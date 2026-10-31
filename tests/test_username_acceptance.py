from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    CandidatePreferences,
    RequestBudget,
    UsernameCheckResult,
    UsernameStatus,
    format_utc_timestamp,
)
from discord_cleanup.ui.pages import usernames as page_module
from discord_cleanup.ui.pages.login import LoginPage
from discord_cleanup.ui.pages.usernames import UsernamesPage
from discord_cleanup.usernames.candidates import candidate_count, next_generated_candidate, preview_candidates
from discord_cleanup.usernames.public_client import PUBLIC_CHECK_URL, PublicUsernameClient
from discord_cleanup.usernames.scanner import UsernameScanner, wait_for_active_shared_requests
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.usernames import UsernameScanWorker


class FakeClock:
    def __init__(self, initial: float = 1_700_000_000.0):
        self.now = float(initial)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> float:
        with self._lock:
            self.now += float(seconds)
            return self.now

    def wait(self, seconds: float, cancel_event: threading.Event | None = None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Cancelled during wait.")
        with self._lock:
            self.now += max(0.0, float(seconds))


class MockResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


# ===========================================================================
# Section 2: Atomic Request Eligibility and Attribution
# ===========================================================================


def test_shared_check_retry_transfers_budget_from_consumer_a_to_consumer_b(tmp_path: Path) -> None:
    """Consumer A has budget=1; Consumer B has budget=5; shared check requires a retry (2 attempts total)."""
    store = UsernameScanStore(tmp_path / "atomic_retry.db")
    both_joined = threading.Event()
    attempt_count = 0
    attempt_lock = threading.Lock()

    def transport_request(method: str, url: str, json_data: Any = None, **kwargs: Any) -> MockResponse:
        nonlocal attempt_count
        both_joined.wait(timeout=2.0)
        with attempt_lock:
            attempt_count += 1
            current = attempt_count
        if current == 1:
            return MockResponse(status_code=502, text="Bad Gateway")
        return MockResponse(status_code=200, json_data={"taken": False})

    transport = MagicMock()
    transport.request.side_effect = transport_request

    client_a = PublicUsernameClient(
        store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0, max_network_retries=2
    )
    client_b = PublicUsernameClient(
        store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0, max_network_retries=2
    )
    scanner_a = UsernameScanner(store, client_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_network_requests=1),
        )
        time.sleep(0.03)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_network_requests=5),
        )
        time.sleep(0.03)
        both_joined.set()
        res_a = fut_a.result(timeout=3.0)
        res_b = fut_b.result(timeout=3.0)

    assert attempt_count == 2
    assert res_a.status == "completed"
    assert res_b.status == "completed"
    assert res_a.available == 1
    assert res_b.available == 1
    # Attempt 1 was reserved & committed on Consumer A (reaching its max_network_requests=1);
    # Attempt 2 was transferred atomically to Consumer B (1/5 used).
    assert res_a.network_checks == 1
    assert res_b.network_checks == 1
    # Consumer B committed the winning HTTP 200 attempt; Consumer A received the deduplicated outcome.
    assert res_b.deduplicated_hits == 0
    assert res_a.deduplicated_hits == 1

    diag = store.diagnostics_summary(PUBLIC_USERNAME_SCOPE, route=PUBLIC_CHECK_URL)
    assert diag.network_requests_total == 2
    assert res_a.network_checks + res_b.network_checks == diag.network_requests_total


def test_consumer_a_cancels_between_reservation_and_dispatch_transfers_to_consumer_b(tmp_path: Path) -> None:
    """If Consumer A cancels after budget reservation before commit, A's reservation rolls back and B is charged."""
    store = UsernameScanStore(tmp_path / "cancel_transfer.db")
    b_joined = threading.Event()
    cancel_a = threading.Event()

    def hook_cancel_a() -> None:
        # Wait until Consumer B has joined the in-flight entry, then cancel Consumer A right before commit!
        b_joined.wait(timeout=2.0)
        cancel_a.set()

    transport = MagicMock()
    transport.request.return_value = MockResponse(status_code=200, json_data={"taken": False})

    client_a = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    client_b = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    scanner_a = UsernameScanner(store, client_a, before_commit_hook=hook_cancel_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            cancel_a,
            budget=RequestBudget(max_network_requests=1),
        )
        time.sleep(0.03)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_network_requests=2),
        )
        time.sleep(0.03)
        b_joined.set()
        res_a = fut_a.result(timeout=3.0)
        res_b = fut_b.result(timeout=3.0)

    assert transport.request.call_count == 1
    assert res_a.status == "paused"
    assert res_a.checked == 0
    assert res_a.cursor == 0
    assert res_a.network_checks == 0

    assert res_b.status == "completed"
    assert res_b.checked == 1
    assert res_b.available == 1
    assert res_b.network_checks == 1
    assert res_b.deduplicated_hits == 0


def test_both_consumers_exhausted_budgets_before_dispatch_dispatches_zero_further_requests(tmp_path: Path) -> None:
    """When both consumers exhaust their budgets before dispatch, 0 further requests are sent."""
    store = UsernameScanStore(tmp_path / "both_exhausted.db")
    both_joined = threading.Event()
    attempt_count = 0

    def transport_request(method: str, url: str, json_data: Any = None, **kwargs: Any) -> MockResponse:
        nonlocal attempt_count
        both_joined.wait(timeout=2.0)
        attempt_count += 1
        return MockResponse(status_code=502, text="Bad Gateway")

    transport = MagicMock()
    transport.request.side_effect = transport_request

    client_a = PublicUsernameClient(
        store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0, max_network_retries=3
    )
    client_b = PublicUsernameClient(
        store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0, max_network_retries=3
    )
    scanner_a = UsernameScanner(store, client_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    # Each consumer has budget=1. Attempt 1 uses A's budget; Attempt 2 uses B's budget; Attempt 3 sees both exhausted!
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_network_requests=1),
        )
        time.sleep(0.03)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_network_requests=1),
        )
        time.sleep(0.03)
        both_joined.set()
        res_a = fut_a.result(timeout=3.0)
        res_b = fut_b.result(timeout=3.0)

    assert attempt_count == 2
    assert res_a.status == "paused"
    assert res_b.status == "paused"
    assert res_a.checked == 0
    assert res_b.checked == 0
    assert res_a.network_checks == 1
    assert res_b.network_checks == 1
    assert "network request budget reached (1/1)" in res_a.pause_reason.lower()
    assert "network request budget reached (1/1)" in res_b.pause_reason.lower()


# ===========================================================================
# Section 3: Separate Shared Work From Original Leader's Deadline
# ===========================================================================


def test_leader_short_deadline_expires_during_cooldown_while_follower_completes(tmp_path: Path) -> None:
    """Consumer A (leader, short duration budget) expires during cooldown; Consumer B (follower) completes."""
    store = UsernameScanStore(tmp_path / "leader_expires_follower_lives.db")
    store.save_cooldown(PUBLIC_CHECK_URL, time.time() + 0.24, "Rate limited")

    transport = MagicMock()
    transport.request.return_value = MockResponse(status_code=200, json_data={"taken": False})

    client_a = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    client_b = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    scanner_a = UsernameScanner(store, client_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_duration_seconds=0.06),
        )
        time.sleep(0.02)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_duration_seconds=2.0, max_network_requests=2),
        )
        res_a = fut_a.result(timeout=1.0)
        elapsed_a = time.monotonic() - t0
        res_b = fut_b.result(timeout=2.0)

    assert elapsed_a < 0.20
    assert res_a.status == "paused"
    assert res_a.checked == 0
    assert res_a.cursor == 0
    assert res_a.network_checks == 0
    assert "duration budget" in res_a.pause_reason.lower()

    assert res_b.status == "completed"
    assert res_b.checked == 1
    assert res_b.available == 1
    assert res_b.network_checks == 1
    assert res_b.deduplicated_hits == 0
    assert transport.request.call_count == 1
    assert wait_for_active_shared_requests(timeout=1.0) is True


def test_leader_long_deadline_completes_while_follower_short_deadline_expires(tmp_path: Path) -> None:
    """Reverse order: Consumer A (leader, long budget) completes after cooldown while Consumer B (follower) expires."""
    store = UsernameScanStore(tmp_path / "follower_expires_leader_lives.db")
    store.save_cooldown(PUBLIC_CHECK_URL, time.time() + 0.24, "Rate limited")

    transport = MagicMock()
    transport.request.return_value = MockResponse(status_code=200, json_data={"taken": False})

    client_a = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    client_b = PublicUsernameClient(store, transport=transport, min_interval=0.0, safety_margin=0.0, jitter=0.0)
    scanner_a = UsernameScanner(store, client_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_duration_seconds=2.0),
        )
        time.sleep(0.02)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_duration_seconds=0.06),
        )
        res_b = fut_b.result(timeout=1.0)
        res_a = fut_a.result(timeout=2.0)

    assert res_b.status == "paused"
    assert res_b.checked == 0
    assert res_b.cursor == 0
    assert res_b.network_checks == 0
    assert "duration budget" in res_b.pause_reason.lower()

    assert res_a.status == "completed"
    assert res_a.checked == 1
    assert res_a.available == 1
    assert res_a.network_checks == 1
    assert transport.request.call_count == 1
    assert wait_for_active_shared_requests(timeout=1.0) is True


def test_all_consumers_expire_or_cancel_during_cooldown_dispatches_zero_requests(tmp_path: Path) -> None:
    """When all consumers on a shared candidate expire or cancel during a cooldown, 0 HTTP requests are made."""
    store = UsernameScanStore(tmp_path / "all_expire_cooldown.db")
    store.save_cooldown(PUBLIC_CHECK_URL, time.time() + 0.60, "Rate limited")

    transport = MagicMock()
    transport.request.return_value = MockResponse(status_code=200, json_data={"taken": False})

    client_a = PublicUsernameClient(store, transport=transport, min_interval=0.0)
    client_b = PublicUsernameClient(store, transport=transport, min_interval=0.0)
    scanner_a = UsernameScanner(store, client_a)
    scanner_b = UsernameScanner(store, client_b)

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    cancel_b = threading.Event()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            scanner_a.run,
            scan_a.id,
            PUBLIC_USERNAME_SCOPE,
            budget=RequestBudget(max_duration_seconds=0.06),
        )
        time.sleep(0.02)
        fut_b = pool.submit(
            scanner_b.run,
            scan_b.id,
            PUBLIC_USERNAME_SCOPE,
            cancel_b,
            budget=RequestBudget(max_duration_seconds=2.0),
        )
        time.sleep(0.05)
        cancel_b.set()
        res_a = fut_a.result(timeout=1.0)
        res_b = fut_b.result(timeout=1.0)

    assert wait_for_active_shared_requests(timeout=1.0) is True
    assert transport.request.call_count == 0
    assert res_a.status == "paused"
    assert res_a.checked == 0
    assert res_b.status == "paused"
    assert res_b.checked == 0


def test_cancellation_while_shared_request_is_in_flight_preserves_candidates(tmp_path: Path) -> None:
    """Cancelling all consumers while a shared HTTP request is in flight pauses cleanly without marking checked."""
    store = UsernameScanStore(tmp_path / "cancel_inflight.db")
    entered = threading.Event()
    release = threading.Event()

    def blocked_request(*args: Any, **kwargs: Any) -> MockResponse:
        entered.set()
        release.wait(timeout=2.0)
        return MockResponse(status_code=200, json_data={"taken": False})

    transport = MagicMock()
    transport.request.side_effect = blocked_request

    scanner_a = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))
    scanner_b = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))
    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan_b = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    cancel_a = threading.Event()
    cancel_b = threading.Event()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(scanner_a.run, scan_a.id, PUBLIC_USERNAME_SCOPE, cancel_a)
        assert entered.wait(timeout=1.5)
        fut_b = pool.submit(scanner_b.run, scan_b.id, PUBLIC_USERNAME_SCOPE, cancel_b)
        time.sleep(0.03)
        cancel_a.set()
        cancel_b.set()
        res_a = fut_a.result(timeout=1.5)
        res_b = fut_b.result(timeout=1.5)
        release.set()

    assert wait_for_active_shared_requests(timeout=1.0) is True
    assert res_a.status == "paused"
    assert res_a.checked == 0
    assert res_a.cursor == 0
    assert res_b.status == "paused"
    assert res_b.checked == 0
    assert res_b.cursor == 0


# ===========================================================================
# Section 4: Fresh-Recheck Semantics
# ===========================================================================


def test_two_concurrent_forced_rechecks_share_single_new_observation(tmp_path: Path) -> None:
    """Two overlapping forced rechecks bypass pre-existing cache and share a single new live network check."""
    store = UsernameScanStore(tmp_path / "two_rechecks.db")
    # Pre-existing cached observation 10s ago saying "abc" is unavailable
    past = time.time() - 10.0
    store.save_cached_observation(
        UsernameCheckResult("abc", UsernameStatus.UNAVAILABLE, format_utc_timestamp(past), observed_epoch=past),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=past,
    )

    entered = threading.Event()
    release = threading.Event()

    def slow_live_check(*args: Any, **kwargs: Any) -> MockResponse:
        entered.set()
        release.wait(timeout=2.0)
        return MockResponse(status_code=200, json_data={"taken": False})

    transport = MagicMock()
    transport.request.side_effect = slow_live_check

    scanner_a = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))
    scanner_b = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(scanner_a.recheck_names, PUBLIC_USERNAME_SCOPE, ["abc"])
        assert entered.wait(timeout=1.5)
        fut_b = pool.submit(scanner_b.recheck_names, PUBLIC_USERNAME_SCOPE, ["abc"])
        time.sleep(0.04)
        release.set()
        res_a = fut_a.result(timeout=2.0)
        res_b = fut_b.result(timeout=2.0)

    assert transport.request.call_count == 1
    assert res_a.status == "completed"
    assert res_b.status == "completed"
    assert res_a.available == 1
    assert res_b.available == 1
    assert res_a.network_checks + res_b.network_checks == 1
    assert res_a.deduplicated_hits + res_b.deduplicated_hits == 1


def test_ordinary_check_joined_by_forced_recheck_enforces_fresh_live_observation(tmp_path: Path) -> None:
    """An ordinary check waiting on candidate lock joined by a forced recheck rejects older cache and dispatches live."""
    store = UsernameScanStore(tmp_path / "ordinary_then_recheck.db")
    older_epoch = time.time() - 5.0
    store.save_cached_observation(
        UsernameCheckResult(
            "abc", UsernameStatus.UNAVAILABLE, format_utc_timestamp(older_epoch), observed_epoch=older_epoch
        ),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=older_epoch,
    )

    transport = MagicMock()
    transport.request.return_value = MockResponse(status_code=200, json_data={"taken": False})
    scanner_a = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))
    scanner_b = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))

    scan_a = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    # Hold the candidate lock briefly so Consumer A (with use_cache=False) and Consumer B (force_recheck=True) coalesce
    cand_lock = store.candidate_inflight_lock(PUBLIC_USERNAME_SCOPE, "abc")
    with ThreadPoolExecutor(max_workers=2) as pool:
        with cand_lock:
            fut_a = pool.submit(scanner_a.run, scan_a.id, PUBLIC_USERNAME_SCOPE, use_cache=False, force_recheck=False)
            time.sleep(0.03)
            fut_b = pool.submit(scanner_b.recheck_names, PUBLIC_USERNAME_SCOPE, ["abc"])
            time.sleep(0.04)
        res_a = fut_a.result(timeout=2.0)
        res_b = fut_b.result(timeout=2.0)

    # Even though `waited_for_process_lock` was True when `cand_lock` released, the cached observation from
    # `older_epoch` predated Consumer B's `request_started_epoch`, so a live network check was forced!
    assert transport.request.call_count == 1
    assert res_a.available == 1
    assert res_b.available == 1


def test_subsecond_same_second_recheck_accepted_and_10ms_older_cache_rejected(tmp_path: Path) -> None:
    """Observations completed in the same integer second (>= start) are accepted; 10ms older cache is rejected."""
    clock = FakeClock(1_700_000_000.25)
    store = UsernameScanStore(tmp_path / "subsecond.db", clock=clock)

    # 1. Create a cached observation at 1700000000.24 (10ms BEFORE 1700000000.25, in the same integer second!)
    obs_10ms_before = 1_700_000_000.24
    store.save_cached_observation(
        UsernameCheckResult(
            "abc",
            UsernameStatus.UNAVAILABLE,
            format_utc_timestamp(obs_10ms_before),
            observed_epoch=obs_10ms_before,
        ),
        provider="public",
        scope=PUBLIC_USERNAME_SCOPE,
        now=obs_10ms_before,
    )

    def live_dispatch(*args: Any, **kwargs: Any) -> MockResponse:
        # Advance clock by 0.50s to 1700000000.75 (still inside integer second 1700000000!)
        clock.advance(0.50)
        return MockResponse(status_code=200, json_data={"taken": False})

    transport = MagicMock()
    transport.request.side_effect = live_dispatch
    client = PublicUsernameClient(store, transport=transport, min_interval=0.0, clock=clock, wait=clock.wait)
    scanner = UsernameScanner(store, client, clock=clock, monotonic=clock)

    # Hold candidate lock once on another thread so `waited_for_process_lock` triggers cross-process cache inspection
    cand_lock = store.candidate_inflight_lock(PUBLIC_USERNAME_SCOPE, "abc")

    def hold_briefly() -> None:
        with cand_lock:
            time.sleep(0.04)

    holder = threading.Thread(target=hold_briefly)
    holder.start()
    time.sleep(0.01)

    scan = scanner.recheck_names(PUBLIC_USERNAME_SCOPE, ["abc"])
    holder.join(timeout=1.0)

    # The 10ms-older cached observation (1700000000.24 < 1700000000.25) was rejected,
    # and the new observation at 1700000000.75 (same integer second!) was accepted!
    assert transport.request.call_count == 1
    assert scan.status == "completed"
    assert scan.available == 1
    rows, _ = store.results(scan.id, PUBLIC_USERNAME_SCOPE, available_only=True, now=clock())
    assert len(rows) == 1
    assert rows[0].effective_observed_epoch() == pytest.approx(1_700_000_000.75)


# ===========================================================================
# Section 5: Live Desktop Freshness Transitions in UsernamesPage
# ===========================================================================


def test_desktop_ui_live_freshness_transitions_idle_paused_waiting_and_page_switch(
    qtbot: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """Visible table rows, filters, counts, copy, and CSV transition from fresh to historical as clock advances."""
    clock = FakeClock(10_000.0)
    store_path = tmp_path / "ui_live_expiry.db"
    store = UsernameScanStore(store_path, clock=clock)
    policy = CacheFreshnessPolicy(available_ttl_seconds=300.0, unavailable_ttl_seconds=3600.0)

    transport = MagicMock()
    transport.request.side_effect = [
        MockResponse(json_data={"taken": False}),  # "cat" available at t=10000 (expires 10300)
        MockResponse(json_data={"taken": True}),   # "dog" unavailable at t=10000 (expires 13600)
    ]
    client = PublicUsernameClient(
        store, transport=transport, min_interval=0.0, clock=clock, wait=clock.wait, freshness_policy=policy
    )
    scanner = UsernameScanner(store, client, freshness_policy=policy, clock=clock, monotonic=clock)
    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["cat", "dog"])
    scanner.run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert transport.request.call_count == 2

    page = UsernamesPage(store_path=store_path, clock=clock)
    page.resize(1000, 680)
    qtbot.addWidget(page)
    page.on_account(PUBLIC_USERNAME_SCOPE, store.list_scans(PUBLIC_USERNAME_SCOPE))

    # 1. At t=10100 (fresh): "cat" is fresh available
    clock.now = 10_100.0
    page.update_cooldown()
    assert "1 available" in page.status_label.text()
    assert "historical" not in page.status_label.text()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "cat"
    assert page.table.item(0, 1).text() == "available"

    # Switch to "All results": both "cat" (available) and "dog" (unavailable) are fresh
    page.filter_input.setCurrentIndex(1)  # All results
    assert page.table.rowCount() == 2
    statuses_fresh = {page.table.item(r, 0).text(): page.table.item(r, 1).text() for r in range(page.table.rowCount())}
    assert statuses_fresh == {"cat": "available", "dog": "unavailable"}

    # Verify idle timer ticks before expiry (t=10200 < 10300) do NOT re-query SQLite results
    with monkeypatch.context() as m:
        results_spy = MagicMock(wraps=page.store().results)
        m.setattr(page.store(), "results", results_spy)
        clock.now = 10_200.0
        for _ in range(5):
            page.update_cooldown()
        assert results_spy.call_count == 0

    # 2. Advance clock past "cat"'s expiry (t=10305 >= 10300) while idle and trigger timer tick
    clock.now = 10_305.0
    page.update_cooldown()
    assert transport.request.call_count == 2  # Zero network requests triggered by UI refresh!
    assert "1 available (0 fresh, 1 historical)" in page.status_label.text()
    statuses_after = {page.table.item(r, 0).text(): page.table.item(r, 1).text() for r in range(page.table.rowCount())}
    details_after = {page.table.item(r, 0).text(): page.table.item(r, 3).text() for r in range(page.table.rowCount())}
    assert statuses_after["cat"] == "available (historical)"
    assert "historical" in details_after["cat"]
    assert statuses_after["dog"] == "unavailable"  # TTL 3600s -> still fresh at t=10305!

    # 3. Switch filter to "Available (fresh only)": expired "cat" is excluded
    page.filter_input.setCurrentIndex(2)  # Available (fresh only)
    assert page.table.rowCount() == 0
    assert page.page_label.text() == "0–0 of 0 results"

    # Copy Available under "Available (fresh only)" copies empty string
    page.copy_available()
    qtbot.waitUntil(lambda: page.results_worker is not None and not page.results_worker.isRunning(), timeout=2000)

    # Switch to "Available names" (includes historical): "cat" is shown as "available (historical)"
    page.filter_input.setCurrentIndex(0)
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "cat"
    assert page.table.item(0, 1).text() == "available (historical)"

    # Export CSV reflects historical freshness without any network traffic
    csv_out = tmp_path / "ui_expired_export.csv"
    page._prepare_results(str(csv_out))
    qtbot.waitUntil(lambda: page.results_worker is not None and not page.results_worker.isRunning(), timeout=2000)
    with csv_out.open(newline="", encoding="utf-8") as f:
        exported = {r["username"]: r for r in csv.DictReader(f)}
    assert exported["cat"]["freshness"] == "historical"
    assert exported["dog"]["freshness"] == "fresh"

    # 4. Page switch (`showEvent`) refresh test: advance past "dog"'s expiry (t=13700) while hidden, then show
    page.filter_input.setCurrentIndex(1)  # All results
    page.hide()
    clock.now = 13_700.0
    page.show()
    statuses_page_switch = {
        page.table.item(r, 0).text(): page.table.item(r, 1).text() for r in range(page.table.rowCount())
    }
    assert statuses_page_switch["dog"] == "unavailable (historical)"
    assert transport.request.call_count == 2


# ===========================================================================
# Section 6: Genuine Multi-Process Integration Tests
# ===========================================================================


class _MockDiscordServer:
    """Thread-safe local HTTP server for multi-process username checker integration tests."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.mode = "available"
        self.hold_first_request = False
        self.first_request_entered = threading.Event()
        self.release_first_request = threading.Event()

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw)
                with parent.lock:
                    req_idx = len(parent.requests)
                    parent.requests.append({"path": self.path, "payload": payload, "time": time.monotonic()})
                    mode = parent.mode
                    should_hold = parent.hold_first_request and req_idx == 0

                if should_hold:
                    parent.first_request_entered.set()
                    parent.release_first_request.wait(timeout=5.0)

                if mode == "captcha":
                    body = json.dumps({"captcha_key": ["captcha-required"], "captcha_sitekey": "site-key"}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                body = json.dumps({"taken": payload.get("username") != "abc"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}/api/v10"
        self.url = f"{self.base_url}/unique-username/username-attempt-unauthed"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.release_first_request.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def test_multiprocess_scan_busy_and_cross_process_candidate_dedup(tmp_path: Path) -> None:
    """Independent Python processes:
    1) Cannot run the same saved scan concurrently (`ScanBusyError`).
    2) Share a single live network request via `candidate_inflight_lock` + `observation_cache` when checking
       the same candidate in separate scans.
    """
    server = _MockDiscordServer()
    server.hold_first_request = True
    db_path = tmp_path / "multiproc_dedup.db"
    store = UsernameScanStore(db_path)
    scan1 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan2 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])

    worker_code = textwrap.dedent(
        """
        import json, pathlib, sys
        from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE
        from discord_cleanup.transport.http_transport import RequestsTransport
        from discord_cleanup.usernames.public_client import PublicUsernameClient
        from discord_cleanup.usernames.scanner import ScanBusyError, UsernameScanner
        from discord_cleanup.usernames.store import UsernameScanStore

        db_path, base_url, scan_id = sys.argv[1], sys.argv[2], sys.argv[3]
        ready_file = sys.argv[4] if len(sys.argv) > 4 else ""
        store = UsernameScanStore(db_path)
        transport = RequestsTransport()
        try:
            client = PublicUsernameClient(store, transport=transport, base_url=base_url, min_interval=0.0, safety_margin=0.0, jitter=0.0)
            scanner = UsernameScanner(store, client)
            if ready_file:
                pathlib.Path(ready_file).write_text("ready", encoding="utf-8")
            res = scanner.run(scan_id, PUBLIC_USERNAME_SCOPE)
            print(json.dumps({
                "ok": True,
                "status": res.status,
                "checked": res.checked,
                "available": res.available,
                "network_checks": res.network_checks,
                "deduplicated_hits": res.deduplicated_hits,
            }))
        except ScanBusyError as exc:
            print(json.dumps({"ok": False, "busy": True, "error": str(exc)}))
        finally:
            transport.close()
        """
    )

    try:
        # Start Process 1 on scan1 (blocks inside server on first request for "abc")
        p1 = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan1.id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert server.first_request_entered.wait(timeout=5.0)

        # Process 2 attempts to run the SAME scan1 while Process 1 holds scan_lock(scan1) -> ScanBusyError!
        p2_same_scan = subprocess.run(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan1.id],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        )
        out_p2 = json.loads(p2_same_scan.stdout.strip().splitlines()[-1])
        assert out_p2["ok"] is False
        assert out_p2["busy"] is True

        # Process 3 runs scan2 (checking the same candidate "abc") while Process 1 holds candidate_inflight_lock
        p3_ready = tmp_path / "p3.ready"
        p3 = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan2.id, str(p3_ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while not p3_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert p3_ready.exists()
        # Give p3 80ms after `p3_ready` to hit `Timeout` on `candidate_inflight_lock`
        time.sleep(0.08)
        server.release_first_request.set()

        stdout1, stderr1 = p1.communicate(timeout=5.0)
        stdout3, stderr3 = p3.communicate(timeout=5.0)
        assert p1.returncode == 0, stderr1
        assert p3.returncode == 0, stderr3

        res1 = json.loads(stdout1.strip().splitlines()[-1])
        res3 = json.loads(stdout3.strip().splitlines()[-1])
        assert res1["ok"] is True and res1["available"] == 1
        assert res3["ok"] is True and res3["available"] == 1
        assert res1["network_checks"] == 1
        assert res3["network_checks"] == 0
        assert res3["deduplicated_hits"] == 1
        # Only 1 HTTP request reached the server across both processes!
        assert len(server.requests) == 1
    finally:
        server.close()


def test_multiprocess_circuit_breaker_blocks_second_process_and_cooldown_extension(tmp_path: Path) -> None:
    """1) Circuit opened by Process 1 blocks Process 2 before any HTTP request is dispatched.
    2) Cooldown extended by Process 2 while Process 1 is waiting is honored by Process 1.
    """
    server = _MockDiscordServer()
    server.mode = "captcha"
    db_path = tmp_path / "multiproc_circuit.db"
    store = UsernameScanStore(db_path)
    scan1 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scan2 = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["def"])

    worker_code = textwrap.dedent(
        """
        import json, pathlib, sys, time
        from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE
        from discord_cleanup.transport.http_transport import RequestsTransport
        from discord_cleanup.usernames.public_client import PublicUsernameClient
        from discord_cleanup.usernames.scanner import UsernameScanner
        from discord_cleanup.usernames.store import UsernameScanStore

        db_path, base_url, scan_id = sys.argv[1], sys.argv[2], sys.argv[3]
        waiting_file = sys.argv[4] if len(sys.argv) > 4 else ""
        store = UsernameScanStore(db_path)
        transport = RequestsTransport()
        try:
            client = PublicUsernameClient(store, transport=transport, base_url=base_url, min_interval=0.0, safety_margin=0.0, jitter=0.0)
            scanner = UsernameScanner(store, client)
            if waiting_file:
                store.save_cooldown(client.url, time.time() + 0.35, "Rate limited")
            t0 = time.monotonic()
            def on_prog(p):
                if waiting_file and "Rate limited" in p.message and not pathlib.Path(waiting_file).exists():
                    pathlib.Path(waiting_file).write_text(str(time.time()), encoding="utf-8")
            res = scanner.run(scan_id, PUBLIC_USERNAME_SCOPE, on_progress=on_prog)
            print(json.dumps({
                "status": res.status,
                "checked": res.checked,
                "cursor": res.cursor,
                "error": res.error,
                "elapsed": time.monotonic() - t0,
            }))
        finally:
            transport.close()
        """
    )

    try:
        # 1. Process 1 hits CAPTCHA -> opens BLOCKED_CHALLENGE circuit in SQLite
        r1 = subprocess.run(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan1.id],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        )
        d1 = json.loads(r1.stdout.strip().splitlines()[-1])
        assert d1["status"] == "failed"
        assert d1["checked"] == 0
        assert d1["cursor"] == 0
        assert len(server.requests) == 1

        # 2. Process 2 starts on scan2; open circuit blocks Process 2 before any HTTP call!
        r2 = subprocess.run(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan2.id],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=True,
        )
        d2 = json.loads(r2.stdout.strip().splitlines()[-1])
        assert d2["status"] == "failed"
        assert d2["checked"] == 0
        assert len(server.requests) == 1  # Still 1: Process 2 made 0 HTTP requests!

        # 3. Reset health, start Process 1 (which sets a 0.35s cooldown right before scanner.run and signals
        #    `waiting_ready` from `on_backoff`), and extend the cooldown from a second connection while Process 1 waits!
        server.mode = "available"
        store.reset_provider_health(PUBLIC_USERNAME_SCOPE, route=server.url)
        waiting_ready = tmp_path / "p_waiter.waiting"

        p_waiter = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(db_path), server.base_url, scan1.id, str(waiting_ready)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while not waiting_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert waiting_ready.exists()
        wait_started_epoch = float(waiting_ready.read_text(encoding="utf-8").strip())
        extended_target = wait_started_epoch + 0.65
        # Extend cooldown in SQLite while p_waiter is already inside `_wait_until_ready`
        UsernameScanStore(db_path).save_cooldown(server.url, extended_target, "Rate limited")

        out_w, err_w = p_waiter.communicate(timeout=5.0)
        assert p_waiter.returncode == 0, err_w
        dw = json.loads(out_w.strip().splitlines()[-1])
        assert dw["status"] == "completed"
        assert dw["checked"] == 1
        assert dw["elapsed"] >= 0.58
    finally:
        server.close()


def test_multiprocess_killed_holder_lock_recovery_and_interrupted_v1_v2_migration(tmp_path: Path) -> None:
    """1) Killing a process holding scan & candidate locks releases locks and recovers without skipping candidate.
    2) Interrupted v1 and v2 migrations resume from `backfill_v3_last_rowid` and complete cleanly.
    """
    db_path = tmp_path / "kill_recovery.db"
    store = UsernameScanStore(db_path)
    scan = store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc", "def"])
    ready_file = tmp_path / "locked.ready"

    holder_code = textwrap.dedent(
        """
        import pathlib, sys, time
        from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE
        from discord_cleanup.usernames.store import UsernameScanStore

        db_path, scan_id, ready_path = sys.argv[1], sys.argv[2], sys.argv[3]
        store = UsernameScanStore(db_path)
        with store.lock(scan_id):
            store.set_status(scan_id, PUBLIC_USERNAME_SCOPE, "running")
            with store.candidate_inflight_lock(PUBLIC_USERNAME_SCOPE, "abc"):
                pathlib.Path(ready_path).write_text("locked", encoding="utf-8")
                time.sleep(30.0)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", holder_code, str(db_path), scan.id, str(ready_file)])
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists()
    finally:
        proc.kill()
        proc.wait(timeout=5.0)

    # Now recover in parent process and run scan to completion without skipping "abc"
    store.recover_interrupted(PUBLIC_USERNAME_SCOPE)
    s_after = store.get_scan(scan.id, PUBLIC_USERNAME_SCOPE)
    assert s_after.status == "paused"
    assert s_after.cursor == 0
    assert s_after.checked == 0

    transport = MagicMock()
    transport.request.side_effect = [
        MockResponse(json_data={"taken": False}),
        MockResponse(json_data={"taken": True}),
    ]
    scanner = UsernameScanner(store, PublicUsernameClient(store, transport=transport, min_interval=0.0))
    done = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert done.status == "completed"
    assert done.checked == 2
    assert done.available == 1

    # Interrupted migration recovery test on v1 and v2 fixtures
    for start_version in (1, 2):
        mig_db = tmp_path / f"interrupted_v{start_version}.sqlite3"
        with sqlite3.connect(mig_db) as conn:
            conn.executescript(
                f"""
                PRAGMA user_version = {start_version};
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
                CREATE TABLE scanner_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO scans VALUES (
                    's1', 'public:discord', 'custom', '[3]', '["aaa","bbb","ccc"]',
                    3, 3, 3, 2, 'completed', '',
                    '2026-10-31T09:00:00+00:00', '2026-10-31T09:00:00+00:00', 1
                );
                INSERT INTO results VALUES ('s1', 'aaa', 'available', '2026-10-31T09:00:00+00:00', '');
                INSERT INTO results VALUES ('s1', 'bbb', 'available', '2026-10-31T09:00:01+00:00', '');
                INSERT INTO results VALUES ('s1', 'ccc', 'unavailable', '2026-10-31T09:00:02+00:00', '');
                -- Simulate an interrupted v3 migration that checkpointed after rowid 1
                INSERT INTO scanner_meta VALUES ('backfill_v3_last_rowid', '1', '2026-10-31T09:00:02+00:00');
                """
            )
        resumed_store = UsernameScanStore(mig_db)
        with sqlite3.connect(mig_db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        # Rowids 2 ("bbb") and 3 ("ccc") must have been backfilled from the resumed checkpoint!
        assert resumed_store.get_cached_observation("bbb", scope=PUBLIC_USERNAME_SCOPE, now=1793437205.0) is not None
        assert resumed_store.get_cached_observation("ccc", scope=PUBLIC_USERNAME_SCOPE, now=1793437205.0) is not None


# ===========================================================================
# Section 7 & 8: Complete Desktop Workflow, UI State Inspection & Benchmarks
# ===========================================================================


def test_complete_desktop_public_workflow_and_ui_states(qtbot: Any, monkeypatch: Any, tmp_path: Path) -> None:
    """End-to-end desktop workflow from LoginPage entry point through preview, budget, pause, resume, and states."""
