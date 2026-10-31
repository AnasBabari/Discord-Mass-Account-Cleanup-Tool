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
