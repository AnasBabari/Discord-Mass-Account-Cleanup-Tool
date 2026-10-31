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
