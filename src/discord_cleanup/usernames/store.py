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
