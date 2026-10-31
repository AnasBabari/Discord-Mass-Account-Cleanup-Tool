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
