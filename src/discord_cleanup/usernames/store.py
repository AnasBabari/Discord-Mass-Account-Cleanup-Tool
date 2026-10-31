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

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        scan_cols = {row[1] for row in connection.execute("PRAGMA table_info(scans)").fetchall()}
        for col_name, col_def in (
            ("strategy", "TEXT NOT NULL DEFAULT 'exhaustive'"),
            ("network_checked", "INTEGER NOT NULL DEFAULT 0"),
            ("cache_hits", "INTEGER NOT NULL DEFAULT 0"),
            ("dedup_hits", "INTEGER NOT NULL DEFAULT 0"),
            ("pause_reason", "TEXT NOT NULL DEFAULT ''"),
            ("config_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col_name not in scan_cols:
                connection.execute(f"ALTER TABLE scans ADD COLUMN {col_name} {col_def}")

        result_cols = {row[1] for row in connection.execute("PRAGMA table_info(results)").fetchall()}
        for col_name, col_def in (
            ("origin", "TEXT NOT NULL DEFAULT 'network'"),
            ("provider", "TEXT NOT NULL DEFAULT ''"),
            ("expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("reused_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in result_cols:
                connection.execute(f"ALTER TABLE results ADD COLUMN {col_name} {col_def}")

        cooldown_cols = {row[1] for row in connection.execute("PRAGMA table_info(username_cooldowns)").fetchall()}
        if "bucket" not in cooldown_cols:
            connection.execute("ALTER TABLE username_cooldowns ADD COLUMN bucket TEXT NOT NULL DEFAULT ''")

        counter_cols = {row[1] for row in connection.execute("PRAGMA table_info(diagnostics_counters)").fetchall()}
        for col_name, col_def in (
            ("last_interval_start", "REAL NOT NULL DEFAULT 0.0"),
            ("last_interval_end", "REAL NOT NULL DEFAULT 0.0"),
        ):
            if col_name not in counter_cols:
                connection.execute(f"ALTER TABLE diagnostics_counters ADD COLUMN {col_name} {col_def}")

    def _run_bounded_backfill(self) -> None:
        policy = CacheFreshnessPolicy()
        while True:
            with self._connect() as connection:
                meta_row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='backfill_v3_last_rowid'"
                ).fetchone()
                last_rowid = int(meta_row[0]) if meta_row is not None and str(meta_row[0]).isdigit() else 0

                rows = connection.execute(
                    "SELECT r.rowid AS r_rowid, r.scan_id, r.username, r.status, r.checked_at, "
                    "r.message, r.origin, r.provider, r.expires_at, s.account_id "
                    "FROM results r JOIN scans s ON r.scan_id = s.id "
                    "WHERE r.rowid > ? AND r.status IN ('available', 'unavailable') "
                    "AND COALESCE(NULLIF(r.origin, ''), 'network') = 'network' "
                    "ORDER BY r.rowid ASC LIMIT ?",
                    (last_rowid, MIGRATION_BATCH_SIZE),
                ).fetchall()

                if not rows:
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('migration_v3_complete', '1')"
                    )
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    break

                max_rowid = last_rowid
                for row in rows:
                    r_rowid = int(row["r_rowid"])
                    max_rowid = max(max_rowid, r_rowid)
                    username = str(row["username"]).lower()
                    account_id = str(row["account_id"])
                    provider = str(row["provider"]) or ("public" if account_id == PUBLIC_USERNAME_SCOPE else "account")
                    status_val = str(row["status"])
                    observed_at = str(row["checked_at"])
                    obs_epoch = parse_utc_timestamp(observed_at)
                    existing_exp_str = str(row["expires_at"] or "")
                    existing_exp_epoch = parse_utc_timestamp(existing_exp_str) if existing_exp_str else None

                    if obs_epoch is None:
                        # Conservative treatment: malformed or unknown timestamps are marked expired/historical, never fresh!
                        obs_epoch = 0.0
                        exp_epoch = 0.0
                        exp_at = format_utc_timestamp(0.0)
                    elif existing_exp_epoch is not None:
                        exp_epoch = existing_exp_epoch
                        exp_at = existing_exp_str
                    else:
                        ttl = (
                            policy.available_ttl_seconds
                            if status_val == UsernameStatus.AVAILABLE.value
                            else policy.unavailable_ttl_seconds
                        )
                        exp_epoch = obs_epoch + ttl
                        exp_at = format_utc_timestamp(exp_epoch)

                    connection.execute(
                        "INSERT INTO observation_cache (username, provider, scope, status, observed_at, observed_epoch, "
                        "expires_at, expires_epoch, message, provenance) VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(username, provider, scope) DO UPDATE SET "
                        "status = excluded.status, "
                        "observed_at = excluded.observed_at, "
                        "observed_epoch = excluded.observed_epoch, "
                        "expires_at = excluded.expires_at, "
                        "expires_epoch = excluded.expires_epoch, "
                        "message = excluded.message, "
                        "provenance = excluded.provenance "
                        "WHERE excluded.observed_epoch > observation_cache.observed_epoch",
                        (
                            username,
                            provider,
                            account_id,
                            status_val,
                            observed_at,
                            obs_epoch,
                            exp_at,
                            exp_epoch,
                            str(row["message"]),
                            "network",
                        ),
                    )
                    if not existing_exp_str or not row["provider"]:
                        connection.execute(
                            "UPDATE results SET provider=?, expires_at=? WHERE scan_id=? AND username=?",
                            (provider, exp_at, str(row["scan_id"]), str(row["username"])),
                        )

                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('backfill_v3_last_rowid', ?)",
                    (str(max_rowid),),
                )
                if len(rows) < MIGRATION_BATCH_SIZE:
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('migration_v3_complete', '1')"
                    )
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    break

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def lock(self, scan_id: str) -> FileLock:
        # Database IDs must never be usable as filesystem paths.
        if str(uuid.UUID(scan_id)) != scan_id:
            raise ValueError("Invalid scan ID.")
        return FileLock(str(self.path) + f".{scan_id}.lock", timeout=0)

    def public_request_lock(self) -> FileLock:
        return FileLock(str(self.path) + ".public-request.lock", timeout=0)

    def candidate_inflight_lock(self, scope: str, username: str) -> FileLock:
        digest = hashlib.blake2s(f"{scope}:{username.lower()}".encode(), digest_size=8).hexdigest()
        return FileLock(str(self.path) + f".inflight.{digest}.lock", timeout=0)

    def cooldown(self, route: str, *, scope: str = "", bucket: str = "") -> tuple[float, str]:
        effective_scope = scope or (PUBLIC_USERNAME_SCOPE if "username-attempt-unauthed" in route else "")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT retry_at, reason, bucket FROM username_cooldowns WHERE route=?", (route,)
            ).fetchone()
            best_retry = float(row["retry_at"]) if row is not None else 0.0
            best_reason = str(row["reason"]) if row is not None else ""
            route_bucket = bucket or (str(row["bucket"]) if row is not None and row["bucket"] else "")
            if effective_scope:
                keys_to_check = [f"global:{effective_scope}"]
                if route_bucket:
                    keys_to_check.append(f"bucket:{effective_scope}:{route_bucket}")
                placeholders = ",".join("?" for _ in keys_to_check)
                extra_rows = connection.execute(
                    f"SELECT retry_at, reason FROM username_cooldowns WHERE route IN ({placeholders})",
                    tuple(keys_to_check),
                ).fetchall()
                for extra in extra_rows:
                    r_at = float(extra["retry_at"])
                    if r_at > best_retry:
                        best_retry = r_at
                        best_reason = str(extra["reason"])
        return best_retry, best_reason

    def save_rate_limit_deadlines(
        self,
        route: str,
        *,
        scope: str = PUBLIC_USERNAME_SCOPE,
        retry_at: float,
        reason: str,
        metadata: RateLimitMetadata | None = None,
    ) -> float:
        bucket = metadata.bucket if metadata is not None else ""
        is_global = metadata.is_global if metadata is not None else False
        self.save_cooldown(
            route,
            retry_at,
            reason,
            scope=scope,
            bucket=bucket,
            is_global=is_global,
        )
        return self.cooldown(route, scope=scope, bucket=bucket)[0]

    def save_cooldown(
        self,
        route: str,
        retry_at: float,
        reason: str,
        *,
        scope: str = "",
        bucket: str = "",
        is_global: bool = False,
    ) -> None:
        if isinstance(retry_at, bool) or not math.isfinite(retry_at) or retry_at < 0:
            raise ValueError("Invalid username cooldown deadline.")
        clean_reason = sanitize_token(reason)
        safe_bucket = sanitize_token(bucket)[:64] if bucket else ""
        effective_scope = scope or (PUBLIC_USERNAME_SCOPE if "username-attempt-unauthed" in route else "")
        with self._connect() as connection:
            self._upsert_cooldown_row(connection, route, retry_at, clean_reason, safe_bucket)
            if safe_bucket and effective_scope:
                self._upsert_cooldown_row(
                    connection, f"bucket:{effective_scope}:{safe_bucket}", retry_at, clean_reason, safe_bucket
                )
            if is_global and effective_scope:
                self._upsert_cooldown_row(
                    connection, f"global:{effective_scope}", retry_at, f"Global rate limit ({clean_reason})", ""
                )

    @staticmethod
    def _upsert_cooldown_row(
        connection: sqlite3.Connection, route: str, retry_at: float, reason: str, bucket: str = ""
    ) -> None:
        connection.execute(
            "INSERT INTO username_cooldowns (route, retry_at, reason, bucket) VALUES (?,?,?,?) "
            "ON CONFLICT(route) DO UPDATE SET "
            "reason=CASE WHEN excluded.retry_at>retry_at THEN excluded.reason ELSE reason END, "
            "retry_at=MAX(retry_at,excluded.retry_at), "
            "bucket=CASE WHEN excluded.bucket!='' THEN excluded.bucket ELSE username_cooldowns.bucket END",
            (route, retry_at, reason, bucket),
        )

    def get_provider_health(
        self, provider: str = PUBLIC_USERNAME_SCOPE, route: str = "", now: float | None = None
    ) -> ProviderHealth:
        current_now = float(self.clock()) if now is None else float(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT provider, state, reason, consecutive_failures, consecutive_rate_limits, retry_at, updated_at "
                "FROM provider_health WHERE provider=?",
                (provider,),
            ).fetchone()
        cooldown_at, cooldown_reason = self.cooldown(route, scope=provider) if route else (0.0, "")
        if row is None:
            if cooldown_at > current_now and cooldown_reason == "Rate limited":
                return ProviderHealth(
                    provider=provider,
                    state=ProviderHealthState.WAITING_COOLDOWN,
                    reason=cooldown_reason,
                    retry_at=cooldown_at,
                )
            return ProviderHealth(provider=provider, state=ProviderHealthState.READY)

        state = ProviderHealthState(row["state"])
        retry_at = max(float(row["retry_at"]), cooldown_at)
        reason = str(row["reason"])
        # WAITING_COOLDOWN and BACKING_OFF recover automatically once retry_at expires;
        # PAUSED_FAILURES, BLOCKED_CHALLENGE, and UNINTERPRETABLE_RESPONSE require explicit reset.
        if state in (ProviderHealthState.WAITING_COOLDOWN, ProviderHealthState.BACKING_OFF) and retry_at <= current_now:
            state = ProviderHealthState.READY
            reason = ""
        elif state == ProviderHealthState.READY and cooldown_at > current_now and cooldown_reason == "Rate limited":
            state = ProviderHealthState.WAITING_COOLDOWN
            reason = cooldown_reason
        return ProviderHealth(
            provider=str(row["provider"]),
            state=state,
            reason=reason,
            consecutive_failures=int(row["consecutive_failures"]),
            consecutive_rate_limits=int(row["consecutive_rate_limits"]),
            retry_at=retry_at,
            updated_at=str(row["updated_at"]),
        )

    def save_provider_health(
        self,
        provider: str,
        state: ProviderHealthState,
        reason: str = "",
        *,
        consecutive_failures: int = 0,
        consecutive_rate_limits: int = 0,
        retry_at: float = 0.0,
        now: float | None = None,
    ) -> ProviderHealth:
        clean_reason = sanitize_token(reason)
        now_str = utc_now()
        safe_retry = retry_at if (not isinstance(retry_at, bool) and math.isfinite(retry_at) and retry_at >= 0) else 0.0
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO provider_health (provider, state, reason, consecutive_failures, consecutive_rate_limits, retry_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "state=excluded.state, reason=excluded.reason, "
                "consecutive_failures=excluded.consecutive_failures, "
                "consecutive_rate_limits=excluded.consecutive_rate_limits, "
                "retry_at=MAX(provider_health.retry_at, excluded.retry_at), "
                "updated_at=excluded.updated_at",
                (
                    provider,
                    state.value,
                    clean_reason,
                    max(0, int(consecutive_failures)),
                    max(0, int(consecutive_rate_limits)),
                    safe_retry,
                    now_str,
                ),
            )
        return self.get_provider_health(provider, now=now)

    def reset_provider_health(
        self,
        provider: str = PUBLIC_USERNAME_SCOPE,
        route: str = "",
        now: float | None = None,
    ) -> ProviderHealth:
        """Deliberate user retry action: resets circuit breaker without shortening any active server cooldown."""
        current_now = float(self.clock()) if now is None else float(now)
        cooldown_at, cooldown_reason = self.cooldown(route, scope=provider) if route else (0.0, "")
        if cooldown_at > current_now and cooldown_reason == "Rate limited":
            next_state = ProviderHealthState.WAITING_COOLDOWN
            next_reason = cooldown_reason
        else:
            next_state = ProviderHealthState.READY
            next_reason = ""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO provider_health (provider, state, reason, consecutive_failures, consecutive_rate_limits, retry_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "state=excluded.state, reason=excluded.reason, consecutive_failures=0, "
                "retry_at=?, updated_at=excluded.updated_at",
                (provider, next_state.value, next_reason, 0, 0, cooldown_at, utc_now(), cooldown_at),
            )
        return self.get_provider_health(provider, route=route, now=current_now)

    def get_cached_observation(
        self,
        username: str,
        provider: str = "public",
        scope: str = PUBLIC_USERNAME_SCOPE,
        *,
        now: float | None = None,
        include_stale: bool = False,
    ) -> UsernameCheckResult | None:
        normalized = username.strip().lower()
        current_now = float(self.clock()) if now is None else float(now)
        bound_clock: Callable[[], float] = (lambda: current_now) if now is not None else self.clock
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, provider, scope, status, observed_at, observed_epoch, expires_at, expires_epoch, message "
                "FROM observation_cache WHERE username=? AND provider=? AND scope=?",
                (normalized, provider, scope),
            ).fetchone()
        if row is None:
            return None
        status = UsernameStatus(row["status"])
        if status == UsernameStatus.ERROR:
            return None
        obs_epoch = float(row["observed_epoch"])
        expires_epoch = float(row["expires_epoch"])
        is_fresh = (obs_epoch > 0.0) and (current_now < expires_epoch)
        if not is_fresh and not include_stale:
            return None
        origin = ObservationOrigin.CACHE.value if is_fresh else ObservationOrigin.STALE_CACHE.value
        return UsernameCheckResult(
            username=str(row["username"]),
            status=status,
            checked_at=str(row["observed_at"]),
            message=str(row["message"]),
            origin=origin,
            provider=str(row["provider"]),
            expires_at=str(row["expires_at"]),
            reused_at=format_utc_timestamp(current_now),
            observed_epoch=obs_epoch,
            expires_epoch=expires_epoch,
            _clock=bound_clock,
        )

    def save_cached_observation(
        self,
        result: UsernameCheckResult,
        provider: str = "public",
        scope: str = PUBLIC_USERNAME_SCOPE,
        *,
        policy: CacheFreshnessPolicy | None = None,
        now: float | None = None,
    ) -> UsernameCheckResult:
        """Persist a verified observation to the shared cache. Operational errors and cached rows are never re-cached."""
        bound_clock: Callable[[], float] = (lambda: float(now)) if now is not None else self.clock
        if result.status == UsernameStatus.ERROR:
            return result.with_clock(bound_clock)
        if result.origin in (ObservationOrigin.CACHE.value, ObservationOrigin.STALE_CACHE.value):
            return result.with_clock(bound_clock)
        freshness = policy or CacheFreshnessPolicy()
        ttl = freshness.ttl_for_status(result.status)
        if ttl <= 0:
            return result.with_clock(bound_clock)

        if result.observed_epoch is not None and math.isfinite(result.observed_epoch) and result.observed_epoch > 0.0:
            obs_epoch = float(result.observed_epoch)
            observed_at = result.checked_at or format_utc_timestamp(obs_epoch)
        elif result.checked_at:
            parsed_obs = parse_utc_timestamp(result.checked_at)
            if parsed_obs is None:
                # Malformed observation timestamp: treat conservatively as expired/historical, never fresh!
                obs_epoch = 0.0
                observed_at = result.checked_at
            else:
                if now is not None and math.isfinite(now) and int(float(now)) == int(parsed_obs):
                    obs_epoch = float(now)
                else:
                    obs_epoch = parsed_obs
                observed_at = result.checked_at
        else:
            obs_epoch = float(self.clock()) if now is None else float(now)
            observed_at = format_utc_timestamp(obs_epoch)

        if obs_epoch <= 0.0:
            exp_epoch = 0.0
            exp_str = format_utc_timestamp(0.0)
        elif result.expires_epoch is not None and math.isfinite(result.expires_epoch) and result.expires_epoch > 0.0:
            exp_epoch = float(result.expires_epoch)
            exp_str = result.expires_at or format_utc_timestamp(exp_epoch)
        elif result.expires_at:
            parsed_exp = parse_utc_timestamp(result.expires_at)
            if parsed_exp is not None:
                exp_epoch = parsed_exp
                exp_str = result.expires_at
            else:
                exp_epoch = 0.0
                exp_str = format_utc_timestamp(0.0)
        else:
            exp_epoch = obs_epoch + ttl
            exp_str = format_utc_timestamp(exp_epoch)

        clean_msg = sanitize_token(result.message)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO observation_cache (username, provider, scope, status, observed_at, observed_epoch, "
                "expires_at, expires_epoch, message, provenance) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(username, provider, scope) DO UPDATE SET "
                "status=excluded.status, observed_at=excluded.observed_at, observed_epoch=excluded.observed_epoch, "
                "expires_at=excluded.expires_at, expires_epoch=excluded.expires_epoch, message=excluded.message, "
                "provenance=excluded.provenance "
                "WHERE excluded.observed_epoch >= observation_cache.observed_epoch",
                (
                    result.username.lower(),
                    provider,
                    scope,
                    result.status.value,
                    observed_at,
                    obs_epoch,
                    exp_str,
                    exp_epoch,
                    clean_msg,
                    result.origin or ObservationOrigin.NETWORK.value,
                ),
            )
        return UsernameCheckResult(
            username=result.username,
            status=result.status,
            checked_at=observed_at,
            message=clean_msg,
            origin=result.origin or ObservationOrigin.NETWORK.value,
            provider=provider,
            expires_at=exp_str,
            reused_at=result.reused_at,
            observed_epoch=obs_epoch,
            expires_epoch=exp_epoch,
            _clock=bound_clock,
        )

    def record_diagnostic(
        self,
        *,
        provider: str,
        scope: str,
        route: str,
        username: str,
        classification: str,
        origin: str,
        scan_id: str = "",
        started_at: str | float = "",
        completed_at: str | float = "",
        http_status: int = 0,
        latency_ms: float = 0.0,
        ratelimit_limit: int | None = None,
        ratelimit_remaining: float | None = None,
        ratelimit_reset_after: float | None = None,
        ratelimit_bucket: str = "",
        ratelimit_scope: str = "",
        ratelimit_global: bool = False,
        retry_at: float = 0.0,
        retry_reason: str = "",
        wait_seconds: float = 0.0,
        consecutive_failures: int = 0,
        consecutive_rate_limits: int = 0,
    ) -> None:
        now_str = format_utc_timestamp(float(self.clock()))
        start_raw = (
            format_utc_timestamp(float(started_at))
            if isinstance(started_at, (int, float)) and not isinstance(started_at, bool)
            else str(started_at or now_str)
        )
        end_raw = (
            format_utc_timestamp(float(completed_at))
            if isinstance(completed_at, (int, float)) and not isinstance(completed_at, bool)
            else str(completed_at or now_str)
        )
        start_ts = sanitize_token(start_raw)
        end_ts = sanitize_token(end_raw)
        safe_username = sanitize_token(username)[:64]
        safe_route = sanitize_token(route)[:120]
        safe_bucket = sanitize_token(ratelimit_bucket)[:64]
        safe_rl_scope = sanitize_token(ratelimit_scope)[:32]
        safe_reason = sanitize_token(retry_reason)[:120]
        safe_wait = max(0.0, float(wait_seconds)) if math.isfinite(wait_seconds) else 0.0
        safe_latency = max(0.0, float(latency_ms)) if math.isfinite(latency_ms) else 0.0
        safe_retry_at = (
            float(retry_at)
            if (not isinstance(retry_at, bool) and math.isfinite(retry_at) and retry_at >= 0)
            else 0.0
        )

        is_definitive_net = int(
            origin == "network" and classification in ("available", "unavailable", "invalid", "syntax_rejected")
        )
        is_cached = int(origin in ("cache", "stale_cache"))
        is_dedup = int(origin == "dedup" or classification == "deduplicated")
        is_local_invalid = int(origin == "local" and classification == "invalid")
        is_net_req = int(origin == "network")
        is_rl = int(http_status == 429 or classification == "rate_limited")
        is_err = int(classification in ("network_error", "blocked_challenge", "uninterpretable"))
        req_span = (safe_latency / 1000.0) + safe_wait

        end_ep = parse_utc_timestamp(end_ts)
        start_ep = parse_utc_timestamp(start_ts)
        if end_ep is None:
            end_ep = float(self.clock())
        if start_ep is None:
            start_ep = end_ep - req_span
        else:
            start_ep = min(start_ep, end_ep - req_span)
        end_ep = max(end_ep, start_ep + req_span)

        with self._connect() as connection:
            connection.execute(
                "INSERT INTO request_diagnostics ("
                "scan_id, provider, scope, route, username, started_at, completed_at, http_status, latency_ms, "
                "classification, origin, ratelimit_limit, ratelimit_remaining, ratelimit_reset_after, "
                "ratelimit_bucket, ratelimit_scope, ratelimit_global, retry_at, retry_reason, wait_seconds, "
                "consecutive_failures, consecutive_rate_limits"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    provider,
                    scope,
                    safe_route,
                    safe_username,
                    start_ts,
                    end_ts,
                    int(http_status),
                    safe_latency,
                    classification,
                    origin,
                    ratelimit_limit,
                    ratelimit_remaining,
                    ratelimit_reset_after,
                    safe_bucket,
                    safe_rl_scope,
                    int(bool(ratelimit_global)),
                    safe_retry_at,
                    safe_reason,
                    safe_wait,
                    max(0, int(consecutive_failures)),
                    max(0, int(consecutive_rate_limits)),
                ),
            )
            connection.execute(
                "DELETE FROM request_diagnostics WHERE id <= ("
                "SELECT id FROM request_diagnostics ORDER BY id DESC LIMIT 1 OFFSET ?"
                ")",
                (self.max_diagnostics_rows,),
            )

            existing = connection.execute(
                "SELECT active_duration_seconds, last_interval_start, last_interval_end "
                "FROM diagnostics_counters WHERE scope=?",
                (scope,),
            ).fetchone()
            if existing is None or float(existing["last_interval_end"]) <= 0.0:
                active_delta = max(0.0, end_ep - start_ep)
                new_int_start = start_ep
                new_int_end = end_ep
            else:
                last_start = float(existing["last_interval_start"])
                last_end = float(existing["last_interval_end"])
                if start_ep > last_end:
                    gap = start_ep - last_end
                    if gap <= 5.0:
                        active_delta = max(0.0, end_ep - last_end)
                        new_int_start = last_start
                        new_int_end = end_ep
                    else:
                        active_delta = max(0.0, end_ep - start_ep)
                        new_int_start = start_ep
                        new_int_end = end_ep
                else:
                    # Overlapping concurrent interval: only add non-overlapping tail beyond last_end
                    active_delta = max(0.0, end_ep - last_end)
                    new_int_start = min(last_start, start_ep)
                    new_int_end = max(last_end, end_ep)

            connection.execute(
                "INSERT INTO diagnostics_counters ("
                "scope, definitive_network_checks, cached_reused, dedup_avoided, local_invalid_avoided, "
                "wait_seconds_total, network_requests_total, rate_limit_responses_total, error_responses_total, "
                "active_duration_seconds, first_started_at, last_completed_at, last_interval_start, last_interval_end"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                "definitive_network_checks = definitive_network_checks + excluded.definitive_network_checks, "
                "cached_reused = cached_reused + excluded.cached_reused, "
                "dedup_avoided = dedup_avoided + excluded.dedup_avoided, "
                "local_invalid_avoided = local_invalid_avoided + excluded.local_invalid_avoided, "
                "wait_seconds_total = wait_seconds_total + excluded.wait_seconds_total, "
                "network_requests_total = network_requests_total + excluded.network_requests_total, "
                "rate_limit_responses_total = rate_limit_responses_total + excluded.rate_limit_responses_total, "
                "error_responses_total = error_responses_total + excluded.error_responses_total, "
                "active_duration_seconds = active_duration_seconds + excluded.active_duration_seconds, "
                "last_completed_at = excluded.last_completed_at, "
                "last_interval_start = excluded.last_interval_start, "
                "last_interval_end = excluded.last_interval_end",
                (
                    scope,
                    is_definitive_net,
                    is_cached,
                    is_dedup,
                    is_local_invalid,
                    safe_wait,
                    is_net_req,
                    is_rl,
                    is_err,
                    active_delta,
                    start_ts,
                    end_ts,
                    new_int_start,
                    new_int_end,
                ),
            )

    def recent_diagnostics(self, scope: str = PUBLIC_USERNAME_SCOPE, limit: int = 50) -> list[RequestDiagnosticEntry]:
        safe_limit = max(1, min(int(limit), self.max_diagnostics_rows))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM request_diagnostics WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, safe_limit),
            ).fetchall()
        return [
            RequestDiagnosticEntry(
                id=int(row["id"]),
                scan_id=str(row["scan_id"]),
                provider=str(row["provider"]),
                scope=str(row["scope"]),
                route=str(row["route"]),
                username=str(row["username"]),
                started_at=str(row["started_at"]),
                completed_at=str(row["completed_at"]),
                http_status=int(row["http_status"]),
                latency_ms=float(row["latency_ms"]),
                classification=str(row["classification"]),
                origin=str(row["origin"]),
                ratelimit_limit=int(row["ratelimit_limit"]) if row["ratelimit_limit"] is not None else None,
                ratelimit_remaining=float(row["ratelimit_remaining"])
                if row["ratelimit_remaining"] is not None
                else None,
                ratelimit_reset_after=float(row["ratelimit_reset_after"])
                if row["ratelimit_reset_after"] is not None
                else None,
                ratelimit_bucket=str(row["ratelimit_bucket"]),
                ratelimit_scope=str(row["ratelimit_scope"]),
                ratelimit_global=bool(row["ratelimit_global"]),
                retry_at=float(row["retry_at"]),
                retry_reason=str(row["retry_reason"]),
                wait_seconds=float(row["wait_seconds"]),
                consecutive_failures=int(row["consecutive_failures"]),
                consecutive_rate_limits=int(row["consecutive_rate_limits"]),
            )
            for row in rows
        ]

    def diagnostics_summary(
        self,
        scope: str = PUBLIC_USERNAME_SCOPE,
        *,
        remaining_candidates: int = 0,
        route: str = "",
        now: float | None = None,
    ) -> DiagnosticsSummary:
        current_now = float(self.clock()) if now is None else float(now)
        with self._connect() as connection:
            counter = connection.execute(
                "SELECT * FROM diagnostics_counters WHERE scope=?", (scope,)
            ).fetchone()
            recent_rows = connection.execute(
                "SELECT classification, origin, http_status, consecutive_failures, consecutive_rate_limits "
                "FROM request_diagnostics WHERE scope=? ORDER BY id DESC LIMIT 50",
                (scope,),
            ).fetchall()

        health = self.get_provider_health(scope, route=route, now=current_now)
        if counter is None:
            return DiagnosticsSummary(
                consecutive_failures=health.consecutive_failures,
                consecutive_rate_limits=health.consecutive_rate_limits,
                provider_health=health,
            )

        definitive_net = int(counter["definitive_network_checks"])
        cached_reused = int(counter["cached_reused"])
        dedup_avoided = int(counter["dedup_avoided"])
        local_invalid = int(counter["local_invalid_avoided"])
        wait_total = float(counter["wait_seconds_total"])
        net_total = int(counter["network_requests_total"])
        active_duration = max(float(counter["active_duration_seconds"]), wait_total)

        recent_success = sum(
            1
            for r in recent_rows
            if r["origin"] == "network" and r["classification"] in ("available", "unavailable", "invalid")
        )
        recent_rl = sum(1 for r in recent_rows if r["http_status"] == 429 or r["classification"] == "rate_limited")
        recent_fail = sum(
            1 for r in recent_rows if r["classification"] in ("network_error", "blocked_challenge", "uninterpretable")
        )
        consec_fail = health.consecutive_failures
        consec_rl = health.consecutive_rate_limits

        cooldown_deadline, _ = self.cooldown(route, scope=scope) if route else (0.0, "")
        pending_cooldown = max(0.0, cooldown_deadline - current_now)
        effective_window = active_duration + pending_cooldown

        # Require at least 2 definitive network checks OR 1 check with a measured cooldown/window >= 5s
        # before estimating throughput; never extrapolate local pacing into a completion promise.
        rate_per_min: float | None = None
        eta_seconds: float | None = None
        if definitive_net >= 2 and effective_window >= 1.0:
            rate_per_min = (definitive_net / effective_window) * 60.0
        elif definitive_net >= 1 and (wait_total >= 5.0 or pending_cooldown >= 5.0):
            rate_per_min = (definitive_net / max(effective_window, 5.0)) * 60.0

        if rate_per_min is not None and rate_per_min > 0 and remaining_candidates > 0:
            eta_seconds = pending_cooldown + (remaining_candidates / (rate_per_min / 60.0))

        return DiagnosticsSummary(
            definitive_network_checks=definitive_net,
            cached_observations_reused=cached_reused,
            requests_avoided_deduplication=dedup_avoided,
            requests_avoided_local_validation=local_invalid,
            total_wait_seconds=wait_total,
            network_requests_total=net_total,
            recent_success_count=recent_success,
            recent_rate_limit_count=recent_rl,
            recent_failure_count=recent_fail,
            consecutive_failures=consec_fail,
            consecutive_rate_limits=consec_rl,
            effective_definitive_checks_per_minute=rate_per_min,
            estimated_remaining_seconds=eta_seconds,
            provider_health=health,
        )

    @staticmethod
    def _scan(row: sqlite3.Row) -> UsernameScan:
        keys = row.keys()
        return UsernameScan(
            id=row["id"],
            account_id=row["account_id"],
            mode=row["mode"],
            lengths=tuple(json.loads(row["lengths"])),
            cursor=row["cursor"],
            total=row["total"],
            checked=row["checked"],
            available=row["available"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            generator_version=row["generator_version"],
            strategy=str(row["strategy"]) if "strategy" in keys else "exhaustive",
            network_checked=int(row["network_checked"]) if "network_checked" in keys else 0,
            cache_hits=int(row["cache_hits"]) if "cache_hits" in keys else 0,
            dedup_hits=int(row["dedup_hits"]) if "dedup_hits" in keys else 0,
            pause_reason=str(row["pause_reason"]) if "pause_reason" in keys else "",
            config_json=str(row["config_json"]) if "config_json" in keys else "{}",
        )

    def create_scan(
        self,
        account_id: str,
        mode: str = "generated",
        lengths: tuple[int, ...] = (3, 4),
        names: list[str] | None = None,
        *,
        preferences: CandidatePreferences | None = None,
    ) -> UsernameScan:
        if not account_id:
            raise ValueError("A verified account is required.")
        if mode not in ("generated", "custom", "priority", "watchlist"):
            raise ValueError("Unknown search mode.")
        lengths = validate_lengths(lengths)
        prefs = preferences or CandidatePreferences()
        raw_names = list(dict.fromkeys(sanitize_token(name).lower().removeprefix("@") for name in (names or [])))
        if mode == "custom" and prefs.strategy == "priority":
            raw_names = rank_candidates_by_preference(raw_names)
        elif mode == "watchlist" and not raw_names:
            raw_names = [entry["username"] for entry in self.list_watchlist(account_id)]
        if mode in ("custom", "watchlist"):
            total = len(raw_names)
            gen_version = 1
            strategy = prefs.strategy if mode == "custom" else "watchlist"
        else:
            if mode == "priority" and raw_names and not prefs.priority_names:
                prefs = CandidatePreferences(
                    strategy="priority",
                    charset_mode=prefs.charset_mode,
                    allowed_chars=prefs.allowed_chars,
                    pattern=prefs.pattern,
                    seed=prefs.seed,
                    priority_names=tuple(rank_candidates_by_preference(raw_names)),
                )
            total = candidate_count(lengths, prefs)
            gen_version = generator_version_for_preferences(prefs)
            strategy = prefs.strategy

        if total == 0:
            raise ValueError("Enter at least one username to check.")
        scan_id, now = str(uuid.uuid4()), utc_now()
        config_str = json.dumps(prefs.to_dict())
        stored_mode = "custom" if mode == "watchlist" else ("generated" if mode == "priority" else mode)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO scans (id,account_id,mode,lengths,names,total,created_at,updated_at,"
                "generator_version,strategy,config_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_id,
                    account_id,
                    stored_mode,
                    json.dumps(lengths),
                    json.dumps(raw_names),
                    total,
                    now,
                    now,
                    gen_version,
                    strategy,
                    config_str,
                ),
            )
        return self.get_scan(scan_id, account_id)

    def scan_preferences(self, scan_id: str, account_id: str) -> CandidatePreferences:
        scan = self.get_scan(scan_id, account_id)
        try:
            data = json.loads(scan.config_json or "{}")
        except (ValueError, TypeError):
            data = {}
        return CandidatePreferences.from_dict(data)

    def preview_scan_candidates(
        self,
        mode: str = "generated",
        lengths: tuple[int, ...] = (3, 4),
        names: list[str] | None = None,
        preferences: CandidatePreferences | None = None,
        limit: int = 20,
    ) -> CandidatePreview:
        return preview_candidates(
            mode=mode,
            lengths=lengths,
            custom_names=names,
            preferences=preferences,
            limit=limit,
        )

    def add_watchlist(self, account_id: str, names: list[str], notes: str = "") -> list[str]:
        if not account_id:
            raise ValueError("Account scope is required.")
        added: list[str] = []
        now = utc_now()
        clean_notes = sanitize_token(notes)[:120]
        with self._connect() as connection:
            for raw in names:
                name = sanitize_token(raw).strip().lower().removeprefix("@")
                if not name or validate_username(name):
                    continue
                connection.execute(
                    "INSERT INTO watchlist (account_id, username, added_at, notes) VALUES (?,?,?,?) "
                    "ON CONFLICT(account_id, username) DO UPDATE SET notes=excluded.notes",
                    (account_id, name, now, clean_notes),
                )
                added.append(name)
        return added

    def watchlist_candidates(
        self,
        account_id: str,
        *,
        provider: str = "public",
        stale_only: bool = True,
        now: float | None = None,
    ) -> list[str]:
        """Return watchlist usernames ordered with unobserved or stale observations first."""
        entries = self.list_watchlist(account_id, now=now)
        unobserved_or_stale: list[str] = []
        fresh: list[str] = []
        for item in entries:
            uname = str(item["username"])
            res: UsernameCheckResult | None = item.get("result")
            if res is None or res.is_stale:
                unobserved_or_stale.append(uname)
            else:
                fresh.append(uname)
        if stale_only:
            return unobserved_or_stale
        return unobserved_or_stale + fresh

    def remove_watchlist(self, account_id: str, username: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM watchlist WHERE account_id=? AND username=?",
                (account_id, username.strip().lower()),
            )

    def list_watchlist(self, account_id: str, now: float | None = None) -> list[dict[str, Any]]:
        current_now = float(self.clock()) if now is None else float(now)
        provider = "public" if account_id == PUBLIC_USERNAME_SCOPE else "account"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, added_at, notes FROM watchlist WHERE account_id=? ORDER BY added_at, username",
                (account_id,),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            uname = str(row["username"])
            cached = self.get_cached_observation(
                uname, provider=provider, scope=account_id, now=current_now, include_stale=True
            )
            entries.append(
                {
                    "username": uname,
                    "added_at": str(row["added_at"]),
                    "notes": str(row["notes"]),
                    "result": cached,
                }
            )
        return entries

    def get_scan(self, scan_id: str, account_id: str) -> UsernameScan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id,account_id,mode,lengths,cursor,total,checked,available,status,error,"
                "created_at,updated_at,generator_version,strategy,network_checked,cache_hits,"
                "dedup_hits,pause_reason,config_json FROM scans WHERE id=? AND account_id=?",
                (scan_id, account_id),
            ).fetchone()
        if row is None:
            raise ValueError("Saved scan not found for this account.")
        return self._scan(row)

    def list_scans(self, account_id: str) -> list[UsernameScan]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,account_id,mode,lengths,cursor,total,checked,available,status,error,"
                "created_at,updated_at,generator_version,strategy,network_checked,cache_hits,"
                "dedup_hits,pause_reason,config_json FROM scans WHERE account_id=? ORDER BY updated_at DESC, rowid DESC",
                (account_id,),
            ).fetchall()
        return [self._scan(row) for row in rows]

    def recover_interrupted(self, account_id: str) -> None:
        """Mark orphaned running scans paused, without disturbing a live writer."""
        for scan in self.list_scans(account_id):
            if scan.status == "running":
                try:
                    with self.lock(scan.id):
                        self.set_status(
                            scan.id,
                            account_id,
                            "paused",
                            pause_reason="Recovered after unexpected interruption",
                        )
                except LockTimeout:
                    pass

    def custom_names(self, scan_id: str, account_id: str) -> list[str]:
        self.get_scan(scan_id, account_id)
        with self._connect() as connection:
            row = connection.execute("SELECT names FROM scans WHERE id=?", (scan_id,)).fetchone()
        return list(json.loads(row[0]))

    def set_status(
        self,
        scan_id: str,
        account_id: str,
        status: str,
        error: str = "",
        *,
        pause_reason: str | None = None,
    ) -> UsernameScan:
        current = self.get_scan(scan_id, account_id)
        effective_pause_reason = (
            sanitize_token(pause_reason)
            if pause_reason is not None
            else (current.pause_reason if status == "paused" else "")
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE scans SET status=?,error=?,pause_reason=?,updated_at=? WHERE id=? AND account_id=?",
                (status, sanitize_token(error), effective_pause_reason, utc_now(), scan_id, account_id),
            )
        return self.get_scan(scan_id, account_id)

    def increment_scan_network_checks(self, scan_id: str, count: int = 1) -> None:
        if not scan_id or count <= 0:
            return
        with self._connect() as connection:
            connection.execute(
                "UPDATE scans SET network_checked=network_checked+?, updated_at=? WHERE id=?",
                (int(count), utc_now(), scan_id),
            )

    def save_result(
        self,
        scan_id: str,
        account_id: str,
        result: UsernameCheckResult,
        next_cursor: int | None,
        *,
        deduplicated: bool = False,
        network_attempts_already_recorded: bool = False,
    ) -> UsernameScan:
        self.get_scan(scan_id, account_id)
        provider = result.provider or ("public" if account_id == PUBLIC_USERNAME_SCOPE else "account")
        origin = result.origin or ObservationOrigin.NETWORK.value
        is_definitive_network = (
            origin == ObservationOrigin.NETWORK.value
            and result.status in (UsernameStatus.AVAILABLE, UsernameStatus.UNAVAILABLE, UsernameStatus.INVALID)
            and not deduplicated
        )
        is_net = 0 if network_attempts_already_recorded else int(is_definitive_network)
        is_cache = int(
            origin in (ObservationOrigin.CACHE.value, ObservationOrigin.STALE_CACHE.value) and not deduplicated
        )
        is_dedup = int(deduplicated)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO results (scan_id,username,status,checked_at,message,origin,provider,expires_at,reused_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(scan_id,username) DO UPDATE SET "
                "status=excluded.status,checked_at=excluded.checked_at,message=excluded.message,"
                "origin=excluded.origin,provider=excluded.provider,expires_at=excluded.expires_at,reused_at=excluded.reused_at",
                (
                    scan_id,
                    result.username,
                    result.status.value,
                    result.checked_at,
                    result.message,
                    origin,
                    provider,
                    result.expires_at,
                    result.reused_at,
                ),
            )
            if next_cursor is not None:
                connection.execute(
                    "UPDATE scans SET cursor=?,checked=checked+1,available=available+?,"
                    "network_checked=network_checked+?,cache_hits=cache_hits+?,dedup_hits=dedup_hits+?,"
                    "updated_at=? WHERE id=?",
                    (
                        next_cursor,
                        int(result.status == UsernameStatus.AVAILABLE),
                        is_net,
                        is_cache,
                        is_dedup,
                        utc_now(),
                        scan_id,
                    ),
                )
        return self.get_scan(scan_id, account_id)

    def _row_to_result(self, row: sqlite3.Row, now: float | None = None) -> UsernameCheckResult:
        keys = row.keys()
        origin = str(row["origin"]) if "origin" in keys and row["origin"] else ObservationOrigin.NETWORK.value
        expires_at = str(row["expires_at"]) if "expires_at" in keys and row["expires_at"] else ""
        bound_clock: Callable[[], float] = (lambda: float(now)) if now is not None else self.clock
        return UsernameCheckResult(
            username=row["username"],
            status=UsernameStatus(row["status"]),
            checked_at=row["checked_at"],
            message=row["message"],
            origin=origin,
            provider=str(row["provider"]) if "provider" in keys else "",
            expires_at=expires_at,
            reused_at=str(row["reused_at"]) if "reused_at" in keys else "",
            _clock=bound_clock,
        )

    def availability_counts_and_next_expiry(
        self,
        scan_id: str,
        account_id: str,
        *,
        now: float | None = None,
    ) -> tuple[int, int, float | None]:
        """Return (total_available, fresh_available, next_expiry_epoch) for `scan_id`."""
        self.get_scan(scan_id, account_id)
        eval_now = float(self.clock()) if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM results WHERE scan_id=? AND status='available'",
                (scan_id,),
            ).fetchall()
        total_avail = len(rows)
        fresh_avail = 0
        next_expiry: float | None = None
        for row in rows:
            res = self._row_to_result(row, now=eval_now)
            if res.is_fresh(eval_now):
                fresh_avail += 1
                exp = res.effective_expires_epoch()
                if exp is not None and exp > eval_now:
                    if next_expiry is None or exp < next_expiry:
                        next_expiry = exp
        return total_avail, fresh_avail, next_expiry

    def availability_counts(
        self,
        scan_id: str,
        account_id: str,
        *,
        now: float | None = None,
    ) -> tuple[int, int]:
        """Return (total_available, fresh_available) for `scan_id` without mutating stored timestamps."""
        total_avail, fresh_avail, _ = self.availability_counts_and_next_expiry(scan_id, account_id, now=now)
        return total_avail, fresh_avail

    def results(
        self,
        scan_id: str,
        account_id: str,
        available_only: bool = True,
        offset: int = 0,
        limit: int = 100,
        *,
        now: float | None = None,
        fresh_only: bool = False,
    ) -> tuple[list[UsernameCheckResult], int]:
        self.get_scan(scan_id, account_id)
        eval_now = float(self.clock()) if now is None else float(now)
        where = "scan_id=?" + (" AND status='available'" if available_only else "")
        safe_limit = max(1, min(limit, 1000))
        safe_offset = max(0, offset)
        if not fresh_only:
            with self._connect() as connection:
                count = connection.execute(f"SELECT COUNT(*) FROM results WHERE {where}", (scan_id,)).fetchone()[0]
                rows = connection.execute(
                    f"SELECT * FROM results WHERE {where} ORDER BY username LIMIT ? OFFSET ?",
                    (scan_id, safe_limit, safe_offset),
                ).fetchall()
            return [self._row_to_result(row, now=eval_now) for row in rows], count

        with self._connect() as connection:
            all_rows = connection.execute(
                f"SELECT * FROM results WHERE {where} ORDER BY username",
                (scan_id,),
            ).fetchall()
        fresh_results = [
            res for row in all_rows if (res := self._row_to_result(row, now=eval_now)).is_fresh(eval_now)
        ]
        return fresh_results[safe_offset : safe_offset + safe_limit], len(fresh_results)

    def iter_results(
        self,
        scan_id: str,
        account_id: str,
        available_only: bool = True,
        *,
        now: float | None = None,
        fresh_only: bool = False,
    ) -> Iterator[UsernameCheckResult]:
        self.get_scan(scan_id, account_id)
        eval_now = float(self.clock()) if now is None else float(now)
        where = "scan_id=?" + (" AND status='available'" if available_only else "")
        with self._connect() as connection:
            cursor = connection.execute(f"SELECT * FROM results WHERE {where} ORDER BY username", (scan_id,))
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    res = self._row_to_result(row, now=eval_now)
                    if fresh_only and not res.is_fresh(eval_now):
                        continue
                    yield res

    def export_csv(
        self,
        scan_id: str,
        account_id: str,
        path: str | Path,
        available_only: bool = False,
        cancel_event: threading.Event | None = None,
        *,
        now: float | None = None,
    ) -> None:
        self.get_scan(scan_id, account_id)
        eval_now = float(self.clock()) if now is None else float(now)
        destination = Path(path).expanduser().resolve()
        database = self.path.resolve()
        protected = {database, Path(str(database) + "-wal"), Path(str(database) + "-shm")}
        is_scan_lock = (
            destination.parent == database.parent
            and destination.name.startswith(database.name + ".")
            and destination.name.endswith(".lock")
        )
        if destination in protected or is_scan_lock:
            raise ValueError("Choose an export file different from the scan database.")
        temporary: Path | None = None
