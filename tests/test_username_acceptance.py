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
