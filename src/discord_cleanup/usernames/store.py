from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as LockTimeout
from platformdirs import user_data_path

from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    CandidatePreferences,
    DiagnosticsSummary,
    ObservationOrigin,
    ProviderHealth,
    ProviderHealthState,
    RateLimitMetadata,
    RequestDiagnosticEntry,
    UsernameCheckResult,
    UsernameScan,
    UsernameStatus,
    format_utc_timestamp,
    parse_utc_timestamp,
    utc_now,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.usernames.candidates import (
    CandidatePreview,
    candidate_count,
    generator_version_for_preferences,
    preview_candidates,
    rank_candidates_by_preference,
    validate_lengths,
    validate_username,
)

MAX_DIAGNOSTICS_ROWS = 500
SCHEMA_VERSION = 3
MIGRATION_BATCH_SIZE = 500

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, mode TEXT NOT NULL,
    lengths TEXT NOT NULL, names TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL, checked INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'paused',
    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    generator_version INTEGER NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'exhaustive',
    network_checked INTEGER NOT NULL DEFAULT 0,
    cache_hits INTEGER NOT NULL DEFAULT 0,
    dedup_hits INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS scans_account ON scans(account_id, updated_at);
CREATE TABLE IF NOT EXISTS results (
    scan_id TEXT NOT NULL REFERENCES scans(id), username TEXT NOT NULL,
    status TEXT NOT NULL, checked_at TEXT NOT NULL, message TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'network',
    provider TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    reused_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scan_id, username)
);
CREATE INDEX IF NOT EXISTS results_status ON results(scan_id, status, username);
CREATE TABLE IF NOT EXISTS username_cooldowns (
    route TEXT PRIMARY KEY,
    retry_at REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    bucket TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS observation_cache (
    username TEXT NOT NULL,
    provider TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observed_epoch REAL NOT NULL,
    expires_at TEXT NOT NULL,
    expires_epoch REAL NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT 'network',
    PRIMARY KEY (username, provider, scope)
);
CREATE INDEX IF NOT EXISTS observation_cache_lookup
    ON observation_cache(provider, scope, username, expires_epoch);
CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'ready',
    reason TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_rate_limits INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    scope TEXT NOT NULL,
    route TEXT NOT NULL,
    username TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    http_status INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    classification TEXT NOT NULL,
    origin TEXT NOT NULL,
    ratelimit_limit INTEGER,
    ratelimit_remaining REAL,
    ratelimit_reset_after REAL,
    ratelimit_bucket TEXT NOT NULL DEFAULT '',
    ratelimit_scope TEXT NOT NULL DEFAULT '',
    ratelimit_global INTEGER NOT NULL DEFAULT 0,
    retry_at REAL NOT NULL DEFAULT 0.0,
    retry_reason TEXT NOT NULL DEFAULT '',
    wait_seconds REAL NOT NULL DEFAULT 0.0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    consecutive_rate_limits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS diagnostics_scope_idx ON request_diagnostics(scope, id DESC);
CREATE TABLE IF NOT EXISTS diagnostics_counters (
    scope TEXT PRIMARY KEY,
    definitive_network_checks INTEGER NOT NULL DEFAULT 0,
    cached_reused INTEGER NOT NULL DEFAULT 0,
    dedup_avoided INTEGER NOT NULL DEFAULT 0,
    local_invalid_avoided INTEGER NOT NULL DEFAULT 0,
    wait_seconds_total REAL NOT NULL DEFAULT 0.0,
    network_requests_total INTEGER NOT NULL DEFAULT 0,
    rate_limit_responses_total INTEGER NOT NULL DEFAULT 0,
    error_responses_total INTEGER NOT NULL DEFAULT 0,
    active_duration_seconds REAL NOT NULL DEFAULT 0.0,
    first_started_at TEXT NOT NULL DEFAULT '',
    last_completed_at TEXT NOT NULL DEFAULT '',
    last_interval_start REAL NOT NULL DEFAULT 0.0,
    last_interval_end REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS watchlist (
    account_id TEXT NOT NULL,
    username TEXT NOT NULL,
    added_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account_id, username)
);
"""


class UsernameScanStore:
    """Transactional checkpoints, observation cache, diagnostics, and scheduling store; no credentials stored."""

    def __init__(
        self,
        path: str | Path | None = None,
        max_diagnostics_rows: int = MAX_DIAGNOSTICS_ROWS,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.path = (
            (
                Path(path)
                if path is not None
                else user_data_path("DiscordMassCleanupTool", appauthor=False) / "usernames.sqlite3"
            )
            .expanduser()
            .resolve()
        )
        self.max_diagnostics_rows = max(10, int(max_diagnostics_rows))
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_or_migrate()

    def _initialize_or_migrate(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValueError("This username database was created by a newer application version.")
            connection.execute("PRAGMA journal_mode=WAL")
            if version == SCHEMA_VERSION:
                # Fast-path open: verify core tables exist without scanning or altering tables
                has_tables = (
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                        "AND name IN ('scans', 'results', 'username_cooldowns', 'observation_cache', 'schema_meta')"
                    ).fetchone()[0]
                    == 5
                )
                if has_tables:
                    mig_done = connection.execute(
                        "SELECT value FROM schema_meta WHERE key='migration_v3_complete'"
                    ).fetchone()
                    if mig_done is not None and str(mig_done[0]) == "1":
                        return

            connection.executescript(BASE_SCHEMA)
            self._ensure_columns(connection)
            if version == 0:
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('migration_v3_complete', '1')"
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return

        # Version 1 or 2 (or interrupted v3 backfill): run bounded-batch backfill once
        self._run_bounded_backfill()
