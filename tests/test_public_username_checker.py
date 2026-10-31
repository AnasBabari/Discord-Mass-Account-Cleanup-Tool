from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from discord_cleanup.api.exceptions import (
    InvalidPayloadError,
    NetworkError,
    RequestCancelledError,
    UpstreamBlockedError,
)
from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE, UsernameCheckResult, UsernameStatus, utc_now
from discord_cleanup.usernames.public_client import PublicUsernameClient
from discord_cleanup.usernames.scanner import UsernameScanner
from discord_cleanup.usernames.store import UsernameScanStore
from tests.test_api_client import MockResponse


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.waits: list[float] = []

    def __call__(self):
        return self.now

    def wait(self, event, seconds):
        if event.is_set():
            return True
        self.now += seconds
        self.waits.append(seconds)
        return False


@pytest.fixture
def public_client(tmp_path):
    clock = FakeClock()
    client = PublicUsernameClient(
        UsernameScanStore(tmp_path / "public.db"),
        transport=MagicMock(),
        min_interval=0,
        clock=clock,
        wait=clock.wait,
    )
    return client, clock


@pytest.mark.parametrize("taken,status", [(False, UsernameStatus.AVAILABLE), (True, UsernameStatus.UNAVAILABLE)])
def test_public_check_needs_no_credentials(public_client, taken, status):
    client, _ = public_client
    client.transport.request.return_value = MockResponse(json_data={"taken": taken})
    assert UsernameScanner(client.store, client).identify() == PUBLIC_USERNAME_SCOPE
    result = client.check_username_availability("abc")
    assert result.status == status
    assert result.checked_at.endswith("+00:00")
    arguments = client.transport.request.call_args
    assert arguments.args == ("POST", client.url)
    assert arguments.kwargs["json_data"] == {"username": "abc"}
    assert "headers" not in arguments.kwargs
    assert client.token == ""


@pytest.mark.parametrize("payload", [{}, [], {"taken": 0}, {"taken": None}, {"taken": "false"}])
def test_public_ambiguous_answer_is_never_available(public_client, payload):
    client, _ = public_client
    client.transport.request.return_value = MockResponse(json_data=payload)
    with pytest.raises(InvalidPayloadError):
        client.check_username_availability("abc")


def test_long_cooldown_waits_then_retries_same_name(public_client):
    client, clock = public_client
    client.transport.request.side_effect = [
        MockResponse(status_code=429, json_data={"retry_after": 2008.934}, headers={"Retry-After": "2009"}),
        MockResponse(json_data={"taken": False}),
    ]
    notices = []
    result = client.check_username_availability("abcd", on_backoff=notices.append)
    assert result.status == UsernameStatus.AVAILABLE
    assert clock.now == pytest.approx(3009.25)
    assert max(notices) == pytest.approx(2009.25)
    assert len(notices) > 30
    assert max(clock.waits) <= 1
    assert [call.kwargs["json_data"] for call in client.transport.request.call_args_list] == [{"username": "abcd"}] * 2


def test_cooldown_survives_pause_restart_and_new_scan(public_client):
    client, clock = public_client
    event = threading.Event()
    client.transport.request.return_value = MockResponse(status_code=429, json_data={"retry_after": 2009})
    scan = client.store.create_scan(PUBLIC_USERNAME_SCOPE, mode="custom", names=["abc"])
    scanner = UsernameScanner(client.store, client)

    def pause(progress):
        if progress.message.startswith("Rate limited"):
            event.set()

    paused = scanner.run(scan.id, PUBLIC_USERNAME_SCOPE, event, pause)
    assert (paused.status, paused.checked, paused.cursor) == ("paused", 0, 0)
    assert client.store.results(scan.id, PUBLIC_USERNAME_SCOPE, False)[1] == 0
    reopened = UsernameScanStore(client.store.path)
    retry_at, _ = reopened.cooldown(client.url)
    assert retry_at == pytest.approx(3009.25)
    replacement = PublicUsernameClient(reopened, transport=MagicMock(), min_interval=0, clock=clock, wait=clock.wait)

    def successful_check(*args, **kwargs):
        assert clock.now >= retry_at
        return MockResponse(json_data={"taken": True})

    replacement.transport.request.side_effect = successful_check
    completed = UsernameScanner(reopened, replacement).run(scan.id, PUBLIC_USERNAME_SCOPE)
    assert completed.status == "completed"
    assert completed.checked == 1
    assert completed.available == 0
    assert replacement.transport.request.call_count == 1


def test_success_headers_schedule_next_request(public_client):
    client, clock = public_client
    client.transport.request.side_effect = [
        MockResponse(
            json_data={"taken": True}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "7.5"}
        ),
        MockResponse(json_data={"taken": False}),
    ]
    client.check_username_availability("abc")
    assert client.store.cooldown(client.url)[0] == pytest.approx(1007.75)
    assert client.check_username_availability("abcd").status == UsernameStatus.AVAILABLE
    assert clock.now == pytest.approx(1007.75)


def test_http_date_and_longest_deadline_are_honored(public_client):
    client, clock = public_client
    client.transport.request.side_effect = [
        MockResponse(
            status_code=429,
            json_data={"retry_after": 2},
            headers={
                "Date": "Fri, 30 Oct 2026 23:00:00 GMT",
                "Retry-After": "Fri, 30 Oct 2026 23:01:00 GMT",
                "X-RateLimit-Reset-After": "30",
            },
        ),
        MockResponse(json_data={"taken": True}),
    ]
    client.check_username_availability("abc")
    assert clock.now == pytest.approx(1060.25)
    client.store.save_cooldown(client.url, 2000, "Rate limited")
    client.store.save_cooldown(client.url, 1100, "Request pacing")
    assert client.store.cooldown(client.url) == (2000, "Rate limited")


@pytest.mark.parametrize("retry", [None, -10, "invalid", float("nan"), float("inf"), True])
def test_bad_retry_values_use_safe_finite_default(public_client, retry):
    client, clock = public_client
    client.transport.request.side_effect = [
        MockResponse(status_code=429, json_data={"retry_after": retry}),
        MockResponse(json_data={"taken": True}),
    ]
    client.check_username_availability("abc")
    assert clock.now == pytest.approx(1060.25)
