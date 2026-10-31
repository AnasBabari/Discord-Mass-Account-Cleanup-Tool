from __future__ import annotations

import csv
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from discord_cleanup.api.exceptions import (
    InvalidPayloadError,
    NetworkError,
    UpstreamBlockedError,
)
from discord_cleanup.cli import usernames as cli
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    CandidatePreferences,
    ProviderHealthState,
    RequestBudget,
    UsernameCheckResult,
    UsernameStatus,
    format_utc_timestamp,
)
from discord_cleanup.ui import pages as _pages  # noqa: F401
from discord_cleanup.ui.pages import usernames as page_module
from discord_cleanup.ui.pages.usernames import UsernamesPage
from discord_cleanup.usernames.candidates import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_V2,
    candidate_count,
    matches_pattern,
    next_generated_candidate,
    permute_index,
    preference_score,
    preview_candidates,
    rank_candidates_by_preference,
)
from discord_cleanup.usernames.public_client import (
    PUBLIC_CHECK_URL,
    PublicUsernameClient,
    parse_rate_limit_metadata,
)
from discord_cleanup.usernames.scanner import UsernameScanner
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.usernames import UsernameScanWorker
from tests.test_api_client import MockResponse


class FakeClock:
    def __init__(self, initial: float = 10000.0):
        self.now = initial
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wait(self, event: threading.Event, seconds: float) -> bool:
        if event.is_set():
            return True
        self.now += seconds
        self.waits.append(seconds)
        return False


# ---------------------------------------------------------------------------
# 1. Rate-limit metadata parsing, multi-constraint scheduling, positive jitter
# ---------------------------------------------------------------------------


def test_parse_rate_limit_metadata_bucket_scope_global_and_contradictory_headers():
    headers = {
        "Retry-After": "12.5",
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset-After": "45.75",
        "X-RateLimit-Reset": "10030.0",
        "X-RateLimit-Bucket": "bucket-abc",
        "X-RateLimit-Scope": "shared",
        "X-RateLimit-Global": "true",
    }
    payload = {"retry_after": 20.0, "global": True}
    meta = parse_rate_limit_metadata(headers, payload, now=10000.0, default_retry=60.0)
    assert meta.bucket == "bucket-abc"
    assert meta.scope == "shared"
    assert meta.is_global is True
    assert meta.limit == 5
    assert meta.remaining == 0.0
    # Longest valid wait wins: 45.75s vs 20.0s vs 12.5s vs 30.0s
    assert meta.retry_after_seconds == pytest.approx(45.75)


def test_bucket_and_global_cooldowns_never_shorten_active_deadline(tmp_path):
    store = UsernameScanStore(tmp_path / "sched.db")
    meta_long = parse_rate_limit_metadata(
        {"X-RateLimit-Bucket": "b1", "X-RateLimit-Scope": "user", "Retry-After": "120"},
        {"retry_after": 120, "global": True},
        now=1000.0,
    )
    deadline1 = store.save_rate_limit_deadlines(
        PUBLIC_CHECK_URL,
        scope=PUBLIC_USERNAME_SCOPE,
        retry_at=1120.25,
        reason="Rate limited",
        metadata=meta_long,
    )
    assert deadline1 == pytest.approx(1120.25)

    # Attempt to save a shorter pacing deadline on the same route/bucket/global scope
    meta_short = parse_rate_limit_metadata(
        {"X-RateLimit-Bucket": "b1", "X-RateLimit-Scope": "user"},
        None,
        now=1005.0,
    )
    deadline2 = store.save_rate_limit_deadlines(
        PUBLIC_CHECK_URL,
        scope=PUBLIC_USERNAME_SCOPE,
        retry_at=1006.0,
        reason="Request pacing",
        metadata=meta_short,
    )
    assert deadline2 == pytest.approx(1120.25)
    active_at, active_reason = store.cooldown(
        PUBLIC_CHECK_URL, scope=PUBLIC_USERNAME_SCOPE, bucket="b1"
    )
    assert active_at == pytest.approx(1120.25)
    assert active_reason == "Rate limited"


def test_positive_only_jitter_never_shortens_wait(tmp_path):
    clock = FakeClock(1000.0)
    store = UsernameScanStore(tmp_path / "jitter.db")
    transport = MagicMock()
    transport.request.side_effect = [
        MockResponse(status_code=429, json_data={"retry_after": 10.0}),
        MockResponse(json_data={"taken": True}),
    ]
    client = PublicUsernameClient(
        store,
        transport=transport,
        min_interval=1.0,
        safety_margin=0.25,
        jitter=0.5,
        clock=clock,
        wait=clock.wait,
        rng=lambda: 0.8,  # deterministic positive jitter = 0.4s
    )
    res = client.check_username_availability("abc")
    assert res.status == UsernameStatus.UNAVAILABLE
    # Base wait = 10.0 + 0.25 safety_margin + 0.4 positive jitter = 10.65s
    assert clock.now >= 1010.25
    assert clock.now == pytest.approx(1010.65)


@pytest.mark.parametrize(
    "status_code,headers,url",
    [
        (302, {"Location": "https://discord.com/login"}, PUBLIC_CHECK_URL),
        (307, {"Location": "https://example.com"}, PUBLIC_CHECK_URL),
        (200, {"Location": "https://discord.com/channels/@me"}, PUBLIC_CHECK_URL),
        (200, {}, "https://discord.com/login"),
    ],
)
def test_redirect_responses_rejected_before_and_after_following(tmp_path, status_code, headers, url):
    clock = FakeClock()
    store = UsernameScanStore(tmp_path / "redir.db")
    transport = MagicMock()
    resp = MockResponse(status_code=status_code, json_data={"taken": False}, headers=headers)
    resp.url = url
    transport.request.return_value = resp
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    with pytest.raises(InvalidPayloadError, match="redirect"):
        client.check_username_availability("abc")
    assert store.get_cached_observation("abc", provider="public", scope=PUBLIC_USERNAME_SCOPE, now=clock.now) is None


# ---------------------------------------------------------------------------
# 2. Provider health state machine & circuit breaker + deliberate reset
# ---------------------------------------------------------------------------


def test_provider_health_transitions_and_deliberate_reset_preserves_cooldown(tmp_path):
    clock = FakeClock(5000.0)
    store = UsernameScanStore(tmp_path / "health.db")
    transport = MagicMock()
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    # Initial state is READY
    h0 = store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h0.state == ProviderHealthState.READY

    # 1. Challenge / block -> BLOCKED_CHALLENGE
    transport.request.return_value = MockResponse(status_code=403, text="Cloudflare challenge")
    scan1 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    res1 = scanner.run(scan1.id, PUBLIC_USERNAME_SCOPE)
    assert res1.status == "failed"
    h_blocked = store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h_blocked.state == ProviderHealthState.BLOCKED_CHALLENGE

    # 2. Save an active server cooldown, then reset health -> transitions to WAITING_COOLDOWN (cooldown preserved!)
    store.save_cooldown(PUBLIC_CHECK_URL, 5500.0, "Rate limited")
    h_reset_during_cd = store.reset_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h_reset_during_cd.state == ProviderHealthState.WAITING_COOLDOWN
    assert store.cooldown(PUBLIC_CHECK_URL)[0] == pytest.approx(5500.0)

    # 3. Advance clock past cooldown, reset health -> READY
    clock.now = 5600.0
    h_ready = store.reset_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h_ready.state == ProviderHealthState.READY

    # 4. Uninterpretable response -> UNINTERPRETABLE_RESPONSE
    transport.request.side_effect = None
    transport.request.return_value = MockResponse(status_code=200, json_data={"unexpected": 123})
    scan2 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["def"])
    res2 = scanner.run(scan2.id, PUBLIC_USERNAME_SCOPE)
    assert res2.status == "failed"
    h_uninterp = store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h_uninterp.state == ProviderHealthState.UNINTERPRETABLE_RESPONSE

    # Reset health before testing PAUSED_FAILURES so open circuit doesn't block scan3 pre-dispatch
    store.reset_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)

    # 5. Network failure after bounded retries -> PAUSED_FAILURES
    transport.request.side_effect = NetworkError("Connection reset")
    scan3 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["ghi"])
    res3 = scanner.run(scan3.id, PUBLIC_USERNAME_SCOPE)
    assert res3.status == "failed"
    h_paused = store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert h_paused.state == ProviderHealthState.PAUSED_FAILURES


@pytest.mark.parametrize(
    "blocked_state",
    [
        ProviderHealthState.BLOCKED_CHALLENGE,
        ProviderHealthState.UNINTERPRETABLE_RESPONSE,
        ProviderHealthState.PAUSED_FAILURES,
    ],
)
def test_open_circuit_enforced_at_dispatch_boundary_across_scans_and_store_instances(tmp_path, blocked_state):
    clock = FakeClock(5000.0)
    store = UsernameScanStore(tmp_path / f"circuit_{blocked_state.value}.db", clock=clock)
    transport = MagicMock()
    transport.request.return_value = MockResponse(json_data={"taken": False})
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    # Pre-cache "cached_ok" while healthy
    store.save_cached_observation(
        UsernameCheckResult("cat", UsernameStatus.AVAILABLE, format_utc_timestamp(clock.now)),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=clock.now,
    )

    # Trip circuit breaker
    store.save_provider_health(
        PUBLIC_USERNAME_SCOPE,
        blocked_state,
        f"Simulated {blocked_state.value}",
        consecutive_failures=3,
        now=clock.now,
    )

    # 1. Local invalid ("bad..") and fresh cache hit ("cat") STILL succeed while circuit is open;
    #    live network check ("dog") stops immediately with 0 transport calls!
    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["bad..", "cat", "dog"])
    res = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert res.status == "failed"
    assert res.checked == 2  # "bad.." and "cat" completed; "dog" remained unconsumed at cursor 2
    assert res.cursor == 2
    assert res.cache_hits == 1
    assert res.network_checks == 0
    assert transport.request.call_count == 0

    # 2. Resuming without resetting health dispatches 0 network calls
    res_resume = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert res_resume.status == "failed"
    assert res_resume.checked == 2
    assert transport.request.call_count == 0

    # 3. Another client/process sharing the same SQLite database also dispatches 0 network calls
    store2 = UsernameScanStore(store.path, clock=clock)
    client2 = PublicUsernameClient(store2, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    with pytest.raises((UpstreamBlockedError, InvalidPayloadError, NetworkError), match="Simulated"):
        client2.check_username_availability("dog")
    assert transport.request.call_count == 0

    # 4. Resetting health while an active server cooldown exists transitions to WAITING_COOLDOWN
    #    and preserves the cooldown; before reset, consecutive_failures (3) is persisted in SQLite.
    assert store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now).consecutive_failures == 3
    store.save_cooldown(PUBLIC_CHECK_URL, 5050.0, "Rate limited")
    reset_h = store.reset_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert reset_h.state == ProviderHealthState.WAITING_COOLDOWN
    assert reset_h.consecutive_failures == 0

    res_after_reset = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert res_after_reset.status == "completed"
    assert res_after_reset.checked == 3
    assert transport.request.call_count == 1
    assert clock.now >= 5050.0
    final_h = store.get_provider_health(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert final_h.state == ProviderHealthState.READY
    assert final_h.consecutive_failures == 0


# ---------------------------------------------------------------------------
# 3. Observation cache: freshness TTLs, original timestamps, scope isolation
# ---------------------------------------------------------------------------


def test_observation_cache_ttls_original_timestamps_and_scope_isolation(tmp_path):
    clock = FakeClock(10000.0)
    store = UsernameScanStore(tmp_path / "cache.db")
    policy = CacheFreshnessPolicy(available_ttl_seconds=300.0, unavailable_ttl_seconds=3600.0)
    transport = MagicMock()
    transport.request.side_effect = [
        MockResponse(json_data={"taken": False}),  # "abc" -> available at t=10000
        MockResponse(json_data={"taken": True}),   # "xyz" -> unavailable at t=10000
    ]
    client = PublicUsernameClient(
        store,
        transport=transport,
        min_interval=0,
        clock=clock,
        wait=clock.wait,
        freshness_policy=policy,
    )
    scanner = UsernameScanner(store, client, freshness_policy=policy, clock=clock, monotonic=clock)

    scan1 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc", "xyz"])
    done1 = scanner.run(scan1.id, PUBLIC_USERNAME_SCOPE)
    assert done1.checked == 2
    assert done1.network_checks == 2
    assert done1.cache_hits == 0
    assert transport.request.call_count == 2

    original_abc_ts = store.results(scan1.id, PUBLIC_USERNAME_SCOPE, False)[0][0].checked_at
    assert original_abc_ts == format_utc_timestamp(10000.0)

    # Advance 120s (t=10120): both "abc" (TTL 300s) and "xyz" (TTL 3600s) are fresh in cache
    clock.now = 10120.0
    scan2 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc", "xyz"])
    done2 = scanner.run(scan2.id, PUBLIC_USERNAME_SCOPE)
    assert done2.checked == 2
    assert done2.network_checks == 0
    assert done2.cache_hits == 2
    assert transport.request.call_count == 2  # 0 new network calls!

    results2, _ = store.results(scan2.id, PUBLIC_USERNAME_SCOPE, False, now=clock.now)
    by_name = {r.username: r for r in results2}
    # Original checked_at timestamp MUST survive cache reuse!
    assert by_name["abc"].checked_at == original_abc_ts
    assert by_name["abc"].is_cached is True
    assert by_name["abc"].is_stale is False
    assert by_name["abc"].display_status == "available (cached)"

    # Advance to t=10400 (400s elapsed): "abc" (available, TTL 300s) is now STALE; "xyz" (unavailable, TTL 3600s) is still FRESH
    clock.now = 10400.0
    results2_stale_view, _ = store.results(scan2.id, PUBLIC_USERNAME_SCOPE, False, now=clock.now)
    by_name_stale = {r.username: r for r in results2_stale_view}
    assert by_name_stale["abc"].is_stale is True
    assert by_name_stale["abc"].display_status == "available (historical)"
    assert by_name_stale["xyz"].is_stale is False
    assert by_name_stale["xyz"].display_status == "unavailable (cached)"

    # Running a new scan at t=10400 must recheck "abc" over the network while still reusing "xyz" from cache!
    transport.request.side_effect = [MockResponse(json_data={"taken": False})]
    scan3 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc", "xyz"])
    done3 = scanner.run(scan3.id, PUBLIC_USERNAME_SCOPE)
    assert done3.network_checks == 1
    assert done3.cache_hits == 1
    assert transport.request.call_count == 3

    # Verify strict scope isolation: account-one does NOT reuse public cache entries
    assert store.get_cached_observation("abc", provider="account", scope="account-one", now=clock.now) is None


def test_operational_errors_are_never_cached(tmp_path):
    clock = FakeClock(2000.0)
    store = UsernameScanStore(tmp_path / "no_err_cache.db")
    err_res = UsernameCheckResult("abc", UsernameStatus.ERROR, format_utc_timestamp(clock.now), "Timeout")
    store.save_cached_observation(err_res, provider="public", scope=PUBLIC_USERNAME_SCOPE, now=clock.now)
    assert store.get_cached_observation("abc", provider="public", scope=PUBLIC_USERNAME_SCOPE, now=clock.now) is None


# ---------------------------------------------------------------------------
# 4. Concurrent in-flight deduplication & multi-consumer cooperative cancel
# ---------------------------------------------------------------------------


def test_concurrent_request_deduplication_and_cooperative_cancellation(tmp_path):
    store = UsernameScanStore(tmp_path / "dedup.db")
    entered = threading.Event()
    release = threading.Event()
    network_calls = 0
    call_lock = threading.Lock()

    def slow_request(*args, **kwargs):
        nonlocal network_calls
        with call_lock:
            network_calls += 1
        entered.set()
        release.wait(timeout=3.0)
        return MockResponse(json_data={"taken": False})

    transport1 = MagicMock()
    transport1.request.side_effect = slow_request
    transport2 = MagicMock()
    transport2.request.side_effect = slow_request

    client1 = PublicUsernameClient(store, transport=transport1, min_interval=0)
    client2 = PublicUsernameClient(store, transport=transport2, min_interval=0)
    scanner1 = UsernameScanner(store, client1)
    scanner2 = UsernameScanner(store, client2)

    scan1 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan2 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    cancel1 = threading.Event()
    cancel2 = threading.Event()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(scanner1.run, scan1.id, PUBLIC_USERNAME_SCOPE, cancel1)
        assert entered.wait(timeout=2.0)
        # Start second consumer while first check for "abc" is in-flight
        fut2 = pool.submit(scanner2.run, scan2.id, PUBLIC_USERNAME_SCOPE, cancel2)
        time.sleep(0.05)
        # Cancel consumer 1 (the leader!) while consumer 2 still wants the result
        cancel1.set()
        res1 = fut1.result(timeout=2.0)
        assert res1.status == "paused"
        assert res1.checked == 0

        # Release the network response so consumer 2 completes using the in-flight request
        release.set()
        res2 = fut2.result(timeout=2.0)
        assert res2.status == "completed"
        assert res2.checked == 1
        assert res2.available == 1
        assert res2.deduplicated_hits == 1

    assert network_calls == 1


# ---------------------------------------------------------------------------
# 5. Request budgets: max_network_requests, max_definitive_checks, duration
# ---------------------------------------------------------------------------


def test_request_budgets_pause_cleanly_and_cache_hits_do_not_spend_network_budget(tmp_path):
    clock = FakeClock(1000.0)
    store = UsernameScanStore(tmp_path / "budget.db")
    transport = MagicMock()
    transport.request.return_value = MockResponse(json_data={"taken": True})
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    # Pre-populate cache with "aaa" and "aab"
    for name in ("aaa", "aab"):
        store.save_cached_observation(
            UsernameCheckResult(name, UsernameStatus.UNAVAILABLE, format_utc_timestamp(clock.now)),
            provider="public",
            scope=PUBLIC_USERNAME_SCOPE,
            now=clock.now,
        )

    # Create scan with ["aaa", "aab", "aac", "aad", "aae"] and max_network_requests=2
    scan = store.create_scan(
        PUBLIC_USERNAME_SCOPE,
        mode="custom",
        names=["aaa", "aab", "aac", "aad", "aae"],
    )
    budget = RequestBudget(max_network_requests=2)
    paused = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, budget=budget)

    # 2 cache hits ("aaa", "aab") + 2 live network checks ("aac", "aad") = 4 checked; pauses before "aae"
    assert paused.status == "paused"
    assert paused.checked == 4
    assert paused.cursor == 4
    assert paused.cache_hits == 2
    assert paused.network_checks == 2
    assert "budget" in paused.pause_reason.lower()
    assert transport.request.call_count == 2


def test_duration_budget_interrupts_long_cooldown_without_losing_candidate(tmp_path):
    clock = FakeClock(1000.0)
    store = UsernameScanStore(tmp_path / "dur_budget.db")
    store.save_cooldown(PUBLIC_CHECK_URL, 3000.0, "Rate limited")
    transport = MagicMock()
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    paused = scanner.run(
        scan.id,
        PUBLIC_USERNAME_SCOPE,
        budget=RequestBudget(max_duration_seconds=5.0),
    )
    assert paused.status == "paused"
    assert paused.checked == 0
    assert paused.cursor == 0
    assert clock.now == pytest.approx(1005.0)
    transport.request.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Candidate strategies: Feistel permutation, resumability, patterns, preview
# ---------------------------------------------------------------------------


def test_seeded_randomized_traversal_is_bijective_and_resumable_from_cursor(tmp_path):
    # Verify permute_index is a full bijection without replacement over an arbitrary domain size
    domain = 250
    permuted = [permute_index(i, domain, seed=1337) for i in range(domain)]
    assert len(permuted) == domain
    assert set(permuted) == set(range(domain))

    # Verify generator v2 with randomized strategy & pattern filter is deterministic and resumable
    prefs = CandidatePreferences(
        strategy="randomized",
        charset_mode="letters",
        pattern="CVC",
        seed=99,
    )
    total = candidate_count((3,), prefs)
    # 20 consonants * 6 vowels * 20 consonants = 2400 CVC 3-char names
    assert total == 20 * 6 * 20

    # Collect first 10 in one pass
    seq_full: list[str] = []
    cur = 0
    for _ in range(10):
        item = next_generated_candidate((3,), cur, prefs)
        assert item is not None
        name, cur = item
        seq_full.append(name)
        assert matches_pattern(name, "CVC")

    # Run via UsernameScanner in two slices (5 checks, pause, reopen store, 5 checks)
    clock = FakeClock(1000.0)
    store = UsernameScanStore(tmp_path / "rand_scan.db")
    checked_order: list[str] = []

    def record_request(method, url, json_data=None, **kwargs):
        checked_order.append(json_data["username"])
        return MockResponse(json_data={"taken": True})

    transport = MagicMock()
    transport.request.side_effect = record_request
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="generated", lengths=(3,), preferences=prefs)
    assert scan.generator_version == GENERATOR_VERSION_V2
    part1 = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, max_checks=5)
    assert part1.status == "paused"
    assert part1.checked == 5

    reopened = UsernameScanStore(store.path)
    client2 = PublicUsernameClient(reopened, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    part2 = UsernameScanner(reopened, client2, clock=clock, monotonic=clock).run(
        scan.id, PUBLIC_USERNAME_SCOPE, max_checks=5
    )
    assert part2.checked == 10
    assert checked_order == seq_full
    assert len(set(checked_order)) == 10


def test_priority_heuristic_and_candidate_preview():
    names = ["z_9.", "abc", "kora", "cat", "a.."]
    ranked = rank_candidates_by_preference(names)
    # 3-char pronounceable "cat" and letter-only "abc" rank ahead of 4-char "kora", punctuation "z_9.", and invalid "a.."
    assert ranked[0] == "cat"
    assert ranked[1] == "abc"
    assert ranked[-1] == "a.."
    assert preference_score("cat") < preference_score("z_9.")

    prefs = CandidatePreferences(strategy="priority", charset_mode="letters", pattern="pronounceable")
    prev = preview_candidates("generated", (3,), [], prefs, sample_size=5)
    assert prev.total_candidates > 0
    assert len(prev.sample_candidates) == 5
    assert "heuristic" in prev.strategy_summary.lower() or "predict" in prev.strategy_summary.lower()


def test_default_exhaustive_preserves_generator_v1(tmp_path):
    store = UsernameScanStore(tmp_path / "v1.db")
    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="generated", lengths=(3, 4))
    assert scan.generator_version == GENERATOR_VERSION


# ---------------------------------------------------------------------------
# 7. Watchlist & freshness-aware recheck
# ---------------------------------------------------------------------------


def test_watchlist_persistence_and_freshness_aware_recheck(tmp_path):
    clock = FakeClock(5000.0)
    store = UsernameScanStore(tmp_path / "watchlist.db")
    added = store.add_watchlist(PUBLIC_USERNAME_SCOPE, ["cat", "dog", "bad..name"])
    assert added == ["cat", "dog"]

    # Cache "cat" as fresh unavailable; leave "dog" unobserved
    store.save_cached_observation(
        UsernameCheckResult("cat", UsernameStatus.UNAVAILABLE, format_utc_timestamp(clock.now)),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=clock.now,
    )
    stale_cands = store.watchlist_candidates(PUBLIC_USERNAME_SCOPE, stale_only=True, now=clock.now)
    assert stale_cands == ["dog"]
    all_cands = store.watchlist_candidates(PUBLIC_USERNAME_SCOPE, stale_only=False, now=clock.now)
    assert all_cands == ["dog", "cat"]


# ---------------------------------------------------------------------------
# 8. Simulated workload: exact requests avoided by cache, dedup, local check
# ---------------------------------------------------------------------------


def test_simulated_workload_quantifies_requests_avoided(tmp_path):
    clock = FakeClock(20000.0)
    store = UsernameScanStore(tmp_path / "workload.db")
    transport = MagicMock()
    transport.request.side_effect = lambda method, url, json_data=None, **kw: MockResponse(
        json_data={"taken": json_data["username"] != "gem"}
    )
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    # Scan 1: 4 valid names ("aaa", "aab", "gem", "xyz") + 2 locally invalid names ("a..", "ab")
    scan1 = store.create_scan(
        PUBLIC_USERNAME_SCOPE,
        mode="custom",
        names=["aaa", "aab", "gem", "xyz", "a..", "ab"],
    )
    res1 = scanner.run(scan1.id, PUBLIC_USERNAME_SCOPE)
    assert res1.status == "completed"
    assert res1.checked == 6
    assert res1.available == 1
    assert res1.network_checks == 4
    assert res1.cache_hits == 0

    # Scan 2 (overlapping custom list): ["aaa", "aab", "gem", "new1", "bad.."]
    clock.now += 30.0
    scan2 = store.create_scan(
        PUBLIC_USERNAME_SCOPE,
        mode="custom",
        names=["aaa", "aab", "gem", "new1", "bad.."],
    )
    res2 = scanner.run(scan2.id, PUBLIC_USERNAME_SCOPE)
    assert res2.status == "completed"
    assert res2.checked == 5
    assert res2.network_checks == 1  # Only "new1" hit the network!
    assert res2.cache_hits == 3      # "aaa", "aab", "gem" served from cache!

    diag = store.diagnostics_summary(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert diag.total_network_checks == 5
    assert diag.total_cache_hits == 3
    assert diag.total_local_invalid == 3
    assert diag.total_requests_avoided == 6
    assert transport.request.call_count == 5


# ---------------------------------------------------------------------------
# 9. Non-interactive CLI & Desktop UI controls + CSV provenance
# ---------------------------------------------------------------------------


def test_non_interactive_cli_preview_check_export_and_diagnostics(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "cli_non_interactive.db"
    csv_path = tmp_path / "cli_out.csv"
    input_file = tmp_path / "candidates.txt"
    input_file.write_text("cat, dog\nbad..\n", encoding="utf-8")

    transport = MagicMock()
    transport.request.side_effect = lambda method, url, json_data=None, **kw: MockResponse(
        json_data={"taken": json_data["username"] != "cat"}
    )
    monkeypatch.setattr(cli, "RequestsTransport", lambda *a, **kw: transport)

    # 1. Preview mode makes zero network calls
    rc_prev = cli.run_username_cli_args(
        ["--usernames", "--lengths", "3", "--strategy", "randomized", "--charset", "letters", "--pattern", "CVC", "--preview"],
        store_path=db_path,
    )
    assert rc_prev == 0
    assert "Candidate preview" in capsys.readouterr().out
    transport.request.assert_not_called()

    # 2. Check from input file + budget + export CSV
    rc_run = cli.run_username_cli_args(
        [
            "--usernames",
            "--input-file",
            str(input_file),
            "--max-network-requests",
            "5",
            "--export",
            str(csv_path),
        ],
        store_path=db_path,
    )
    assert rc_run == 0
    out_run = capsys.readouterr().out
    assert "AVAILABLE: cat" in out_run
    assert transport.request.call_count == 2  # "cat" and "dog" (while "bad.." rejected locally)

    # 3. Second run reuses cache and marks provenance in CSV
    csv_path2 = tmp_path / "cli_out_cached.csv"
    rc_cached = cli.run_username_cli_args(
        ["--usernames", "--check", "cat,dog", "--export", str(csv_path2)],
        store_path=db_path,
    )
    assert rc_cached == 0
    out_cached = capsys.readouterr().out
    assert "(cached)" in out_cached
    assert transport.request.call_count == 2  # No additional network requests!

    with csv_path2.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[0]["username"] == "cat"
    assert rows[0]["provenance"] == "cached"
    assert rows[0]["expires_at_utc"] != ""

    # 4. Diagnostics flag prints summary
    rc_diag = cli.run_username_cli_args(["--usernames", "--diagnostics"], store_path=db_path)
    assert rc_diag == 0
    diag_out = capsys.readouterr().out
    assert "Cache Hits Reused:" in diag_out


def test_desktop_page_minimum_size_strategy_watchlist_and_health_reset(qtbot, monkeypatch, tmp_path):
    store_path = tmp_path / "ui_new.db"
    store = UsernameScanStore(store_path)
    store.save_cooldown(PUBLIC_CHECK_URL, time.time() + 1200, "Rate limited")
    store.save_provider_health(PUBLIC_USERNAME_SCOPE, ProviderHealthState.PAUSED_FAILURES, "Network error")

    transport = MagicMock()
    transport.request.return_value = MockResponse(json_data={"taken": False})
    client = PublicUsernameClient(store, transport=transport, min_interval=0)

    monkeypatch.setattr(
        page_module,
        "UsernameScanWorker",
        lambda *args, **kwargs: UsernameScanWorker(*args, **kwargs, client=client),
    )

    page = UsernamesPage(store_path=store_path)
    page.resize(1000, 680)
    qtbot.addWidget(page)

    # Add to watchlist via custom_input
    page.mode_input.setCurrentIndex(1)  # Custom list
    page.custom_input.setPlainText("fox, owl")
    page.add_selected_to_watchlist()
    assert "Added 2" in page.status_label.text()

    # Reset health preserves active server cooldown
    page.reset_provider_health()
    assert "cooldown remains enforced" in page.status_label.text()
    assert store.cooldown(PUBLIC_CHECK_URL)[0] > time.time()


def test_sqlite_migration_on_copy_of_legacy_schema(tmp_path):
    db_file = tmp_path / "legacy_v2.sqlite3"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 2;
            CREATE TABLE scans (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                lengths TEXT NOT NULL,
                names TEXT NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'paused',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                generator_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE results (
                scan_id TEXT NOT NULL,
                username TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scan_id, username)
            );
            CREATE TABLE username_cooldowns (
                route TEXT PRIMARY KEY,
                retry_at REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            INSERT INTO scans VALUES (
                'scan-1', 'public:discord', 'generated', '[3, 4]', '[]',
                1, 2135677, 1, 0, 'paused', '',
                '2026-10-31T09:12:04+00:00', '2026-10-31T09:13:06+00:00', 1
            );
            INSERT INTO results VALUES (
                'scan-1', 'aaa', 'unavailable', '2026-10-31T09:12:05+00:00', ''
            );
            INSERT INTO username_cooldowns VALUES (
                'https://discord.com/api/v10/unique-username/username-attempt-unauthed',
                1793440080.00228,
                'Rate limited',
                '2026-10-31T09:12:05+00:00'
            );
            """
        )

    migrated = UsernameScanStore(db_file)
    scan = migrated.get_scan("scan-1", PUBLIC_USERNAME_SCOPE)
    assert scan.cursor == 1
    assert scan.checked == 1
    assert scan.total == 2135677
    assert scan.status == "paused"
    assert scan.generator_version == 1

    # Verify cooldown survived intact
    cd_at, cd_reason = migrated.cooldown(PUBLIC_CHECK_URL)
    assert cd_at == pytest.approx(1793440080.00228)
    assert cd_reason == "Rate limited"

    # Verify existing definitive result was backfilled into observation_cache!
    cached = migrated.get_cached_observation(
        "aaa",
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=1793437926.0,  # 1 second after 2026-10-31T09:12:05+00:00
    )
    assert cached is not None
    assert cached.status == UsernameStatus.UNAVAILABLE
    assert cached.checked_at == "2026-10-31T09:12:05+00:00"
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 10. Hardened regression tests: migration, freshness, budgets, concurrency,
#     fast DP candidate counts, scheduler isolation, and diagnostics trimming
# ---------------------------------------------------------------------------


def test_migration_runs_once_preserves_expires_at_and_handles_malformed_timestamps(tmp_path):
    db_file = tmp_path / "migration_edge.sqlite3"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 2;
            CREATE TABLE scans (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                lengths TEXT NOT NULL,
                names TEXT NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL,
                checked INTEGER NOT NULL DEFAULT 0,
                available INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'paused',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                generator_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE results (
                scan_id TEXT NOT NULL,
                username TEXT NOT NULL,
                status TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'network',
                expires_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scan_id, username)
            );
            INSERT INTO scans VALUES (
                's-old', 'public:discord', 'custom', '[3]', '["abc","def","badts","cachedrow"]',
                4, 4, 4, 2, 'completed', '',
                '2026-10-31T08:00:00+00:00', '2026-10-31T08:00:00+00:00', 1
            );
            INSERT INTO scans VALUES (
                's-new', 'public:discord', 'custom', '[3]', '["abc"]',
                1, 1, 1, 0, 'completed', '',
                '2026-10-31T09:00:00+00:00', '2026-10-31T09:00:00+00:00', 1
            );
            -- Older observation says "abc" is available at 08:00
            INSERT INTO results VALUES (
                's-old', 'abc', 'available', '2026-10-31T08:00:00+00:00', '', 'network', '2026-10-31T08:15:00+00:00'
            );
            -- Newer observation says "abc" is unavailable at 09:00
            INSERT INTO results VALUES (
                's-new', 'abc', 'unavailable', '2026-10-31T09:00:00+00:00', '', 'network', '2026-10-31T15:00:00+00:00'
            );
            -- Row with custom established expires_at
            INSERT INTO results VALUES (
                's-old', 'def', 'available', '2026-10-31T08:00:00+00:00', '', 'network', '2026-10-31T08:05:00+00:00'
            );
            -- Row with malformed timestamp
            INSERT INTO results VALUES (
                's-old', 'badts', 'available', 'not-a-valid-timestamp', '', 'network', ''
            );
            -- Row with origin='cache' must NOT be treated as a new primary network observation
            INSERT INTO results VALUES (
                's-old', 'cachedrow', 'available', '2026-10-31T08:00:00+00:00', '', 'cache', '2026-10-31T08:15:00+00:00'
            );
            """
        )

    store = UsernameScanStore(db_file)
    # 1. Newer observation ("unavailable" at 09:00) won over older ("available" at 08:00)
    obs_abc = store.get_cached_observation("abc", scope=PUBLIC_USERNAME_SCOPE, now=1793437210.0)
    assert obs_abc is not None
    assert obs_abc.status == UsernameStatus.UNAVAILABLE
    assert obs_abc.checked_at == "2026-10-31T09:00:00+00:00"

    # 2. Established expires_at on "def" (08:05:00) was preserved, not recalculated to 08:15:00
    obs_def_before = store.get_cached_observation("def", scope=PUBLIC_USERNAME_SCOPE, now=1793433800.0)  # 08:03:20
    assert obs_def_before is not None
    assert obs_def_before.expires_at == "2026-10-31T08:05:00+00:00"
    obs_def_after = store.get_cached_observation("def", scope=PUBLIC_USERNAME_SCOPE, now=1793434200.0)  # 08:10:00
    assert obs_def_after is None

    # 3. Malformed timestamp is treated conservatively as expired/historical, never freshly verified
    assert store.get_cached_observation("badts", scope=PUBLIC_USERNAME_SCOPE, now=1.0) is None
    rows_old, _ = store.results("s-old", PUBLIC_USERNAME_SCOPE, available_only=False, now=1793433601.0)
    by_user = {r.username: r for r in rows_old}
    assert by_user["badts"].is_stale is True
    assert by_user["badts"].display_status == "available (historical)"

    # 4. origin='cache' row was not backfilled as a primary network observation
    assert store.get_cached_observation("cachedrow", scope=PUBLIC_USERNAME_SCOPE, now=1793433601.0) is None

    # 5. Subsequent opens do not re-run migration backfill
    with sqlite3.connect(db_file) as conn:
        conn.execute("DELETE FROM observation_cache")
    reopened = UsernameScanStore(db_file)
    assert reopened.get_cached_observation("abc", scope=PUBLIC_USERNAME_SCOPE, now=1793437210.0) is None


def test_save_cached_observation_preserves_result_timestamp_and_rejects_older_write(tmp_path):
    store = UsernameScanStore(tmp_path / "order.db")
    newer = UsernameCheckResult("abc", UsernameStatus.UNAVAILABLE, format_utc_timestamp(2000.0))
    older = UsernameCheckResult("abc", UsernameStatus.AVAILABLE, format_utc_timestamp(1000.0))

    # Even if now=5000.0 is passed, result.checked_at (2000.0) is used for observed_epoch
    store.save_cached_observation(newer, scope=PUBLIC_USERNAME_SCOPE, now=5000.0)
    # Out-of-order older observation must not overwrite newer observation
    store.save_cached_observation(older, scope=PUBLIC_USERNAME_SCOPE, now=6000.0)

    cached = store.get_cached_observation("abc", scope=PUBLIC_USERNAME_SCOPE, now=2100.0)
    assert cached is not None
    assert cached.status == UsernameStatus.UNAVAILABLE
    assert cached.checked_at == format_utc_timestamp(2000.0)


def test_freshness_accuracy_across_results_filters_csv_and_watchlist(tmp_path):
    clock = FakeClock(10000.0)
    store = UsernameScanStore(tmp_path / "freshness.db", clock=clock)
    policy = CacheFreshnessPolicy(available_ttl_seconds=300.0, unavailable_ttl_seconds=3600.0)
    transport = MagicMock()
    transport.request.side_effect = [
        MockResponse(json_data={"taken": False}),  # "cat" available at t=10000 (expires 10300)
        MockResponse(json_data={"taken": False}),  # "dog" available at t=10200 (expires 10500)
    ]
    client = PublicUsernameClient(
        store, transport=transport, min_interval=0, clock=clock, wait=clock.wait, freshness_policy=policy
    )
    scanner = UsernameScanner(store, client, freshness_policy=policy, clock=clock, monotonic=clock)

    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["cat", "dog"])
    scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, max_checks=1)
    clock.now = 10200.0
    scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, max_checks=1)

    # At t=10400: "cat" (network observation from 10000) is expired/historical; "dog" (from 10200) is fresh!
    clock.now = 10400.0
    total_avail, fresh_avail = store.availability_counts(scan.id, PUBLIC_USERNAME_SCOPE)
    assert total_avail == 2
    assert fresh_avail == 1

    all_avail, total_all = store.results(scan.id, PUBLIC_USERNAME_SCOPE, available_only=True)
    assert total_all == 2
    by_name = {r.username: r for r in all_avail}
    # Original network origin is preserved, while time-based freshness marks "cat" as historical
    assert by_name["cat"].origin == "network"
    assert by_name["cat"].is_stale is True
    assert by_name["cat"].display_status == "available (historical)"
    assert "historical" in by_name["cat"].provenance_label()
    assert by_name["dog"].origin == "network"
    assert by_name["dog"].is_stale is False
    assert by_name["dog"].display_status == "available"

    fresh_rows, fresh_total = store.results(scan.id, PUBLIC_USERNAME_SCOPE, available_only=True, fresh_only=True)
    assert fresh_total == 1
    assert [r.username for r in fresh_rows] == ["dog"]
    assert [r.username for r in store.iter_results(scan.id, PUBLIC_USERNAME_SCOPE, fresh_only=True)] == ["dog"]

    csv_path = tmp_path / "freshness.csv"
    store.export_csv(scan.id, PUBLIC_USERNAME_SCOPE, csv_path)
    with csv_path.open(newline="", encoding="utf-8") as f:
        csv_rows = {r["username"]: r for r in csv.DictReader(f)}
    assert csv_rows["cat"]["provenance"] == "live_network"
    assert csv_rows["cat"]["freshness"] == "historical"
    assert csv_rows["dog"]["provenance"] == "live_network"
    assert csv_rows["dog"]["freshness"] == "fresh"


@pytest.mark.parametrize(
    "first_failure",
    [
        NetworkError("Socket closed"),
        MockResponse(status_code=502, text="Bad Gateway"),
        MockResponse(status_code=429, json_data={"retry_after": 1.0}),
    ],
)
def test_max_network_requests_budget_of_one_stops_after_single_transport_attempt(tmp_path, first_failure):
    clock = FakeClock(1000.0)
    store = UsernameScanStore(tmp_path / "strict_budget.db", clock=clock)
    transport = MagicMock()
    transport.request.side_effect = [
        first_failure,
        MockResponse(json_data={"taken": False}),
    ]
    client = PublicUsernameClient(
        store, transport=transport, min_interval=0, max_network_retries=3, clock=clock, wait=clock.wait
    )
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)
    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc", "def"])

    res = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, budget=RequestBudget(max_network_requests=1))
    # Exactly 1 HTTP attempt was made; retry was NOT dispatched because budget=1 was exhausted!
    assert transport.request.call_count == 1
    assert res.status == "paused"
    assert res.checked == 0
    assert res.cursor == 0
    assert "budget" in res.pause_reason.lower()
    diag = store.diagnostics_summary(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL, now=clock.now)
    assert diag.network_requests_total == 1
    assert diag.definitive_network_checks == 0


def test_budget_and_policy_validation_rejects_invalid_values():
    for bad in (True, False, 0, -1, -5, "1"):
        with pytest.raises((ValueError, TypeError)):
            RequestBudget(max_network_requests=bad)  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            RequestBudget(max_definitive_checks=bad)  # type: ignore[arg-type]
    for bad_dur in (True, False, 0.0, -0.5, float("nan"), float("inf"), float("-inf")):
        with pytest.raises((ValueError, TypeError)):
            RequestBudget(max_duration_seconds=bad_dur)  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            CacheFreshnessPolicy(available_ttl_seconds=bad_dur)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CandidatePreferences(strategy="invalid_strategy")
    with pytest.raises(ValueError):
        CandidatePreferences(charset_mode="invalid_charset")
    with pytest.raises(ValueError):
        CandidatePreferences(seed=True)  # type: ignore[arg-type]


def test_force_recheck_does_not_reuse_pre_existing_cache(tmp_path):
    clock = FakeClock(5000.0)
    store = UsernameScanStore(tmp_path / "force_recheck.db", clock=clock)
    # Pre-existing fresh observation at t=4995 (5 seconds before scan starts at t=5000)
    store.save_cached_observation(
        UsernameCheckResult("abc", UsernameStatus.UNAVAILABLE, format_utc_timestamp(4995.0)),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=4995.0,
    )
    transport = MagicMock()
    transport.request.return_value = MockResponse(json_data={"taken": False})
    client = PublicUsernameClient(store, transport=transport, min_interval=0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    res = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, use_cache=False, force_recheck=True)
    assert res.status == "completed"
    assert res.available == 1
    assert res.network_checks == 1
    assert res.cache_hits == 0
    assert transport.request.call_count == 1
