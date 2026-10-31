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
