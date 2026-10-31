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
