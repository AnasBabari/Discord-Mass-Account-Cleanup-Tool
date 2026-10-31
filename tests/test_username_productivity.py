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
