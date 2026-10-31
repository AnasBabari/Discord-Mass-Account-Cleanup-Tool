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
