from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PUBLIC_USERNAME_SCOPE = "public:discord"

DEFAULT_AVAILABLE_TTL_SECONDS = 900.0  # 15 minutes: available handles can be claimed quickly
DEFAULT_UNAVAILABLE_TTL_SECONDS = 86400.0  # 24 hours: claimed handles rarely become free immediately
DEFAULT_INVALID_TTL_SECONDS = 604800.0  # 7 days: deterministic syntax/reserved rejection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def parse_utc_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


class UsernameStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    ERROR = "error"


class ObservationOrigin(str, Enum):
    NETWORK = "network"
    CACHE = "cache"
    STALE_CACHE = "stale_cache"
    LOCAL = "local"


class ProviderHealthState(str, Enum):
    READY = "ready"
    WAITING_COOLDOWN = "waiting_cooldown"
    BACKING_OFF = "backing_off"
    PAUSED_FAILURES = "paused_failures"
    BLOCKED_CHALLENGE = "blocked_challenge"
    UNINTERPRETABLE_RESPONSE = "uninterpretable_response"

    @property
    def label(self) -> str:
        labels = {
            ProviderHealthState.READY: "Ready",
            ProviderHealthState.WAITING_COOLDOWN: "Waiting for server cooldown",
            ProviderHealthState.BACKING_OFF: "Backing off after connection failure",
            ProviderHealthState.PAUSED_FAILURES: "Paused after repeated failures",
            ProviderHealthState.BLOCKED_CHALLENGE: "Blocked by verification challenge",
            ProviderHealthState.UNINTERPRETABLE_RESPONSE: "Temporarily unable to interpret response",
        }
        return labels.get(self, self.value)


@dataclass(frozen=True)
class CacheFreshnessPolicy:
    available_ttl_seconds: float = DEFAULT_AVAILABLE_TTL_SECONDS
    unavailable_ttl_seconds: float = DEFAULT_UNAVAILABLE_TTL_SECONDS
    invalid_ttl_seconds: float = DEFAULT_INVALID_TTL_SECONDS
    use_cache: bool = True
    force_recheck: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("available_ttl_seconds", self.available_ttl_seconds),
            ("unavailable_ttl_seconds", self.unavailable_ttl_seconds),
            ("invalid_ttl_seconds", self.invalid_ttl_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ValueError(f"Cache TTL '{name}' must be a finite positive number.")
        if not isinstance(self.use_cache, bool) or not isinstance(self.force_recheck, bool):
            raise ValueError("Cache boolean flags 'use_cache' and 'force_recheck' must be booleans.")

    def ttl_for_status(self, status: UsernameStatus) -> float:
        if status == UsernameStatus.AVAILABLE:
            return float(self.available_ttl_seconds)
        if status == UsernameStatus.UNAVAILABLE:
            return float(self.unavailable_ttl_seconds)
        if status == UsernameStatus.INVALID:
            return float(self.invalid_ttl_seconds)
        return 0.0


VALID_STRATEGIES = ("exhaustive", "generated", "randomized", "priority", "watchlist")
VALID_CHARSET_MODES = (
    "all",
    "letters",
    "letters_only",
    "alphanumeric",
    "no_punctuation",
    "no_periods",
    "letters_digits_underscore",
    "custom",
)


@dataclass(frozen=True)
class CandidatePreferences:
    strategy: str = "exhaustive"  # "exhaustive", "randomized", "priority", "watchlist"
    charset_mode: str = "all"  # "all", "letters", "letters_only", "alphanumeric", "no_punctuation", "no_periods", "custom"
    allowed_chars: str = ""
    pattern: str = "any"  # "any", "pronounceable", "starts_with_letter", "no_edge_punctuation", "palindrome", or template
    seed: int = 42
    priority_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.strategy not in VALID_STRATEGIES:
            raise ValueError(f"Unknown candidate strategy: {self.strategy}")
        if self.charset_mode not in VALID_CHARSET_MODES:
            raise ValueError(f"Unknown character-set preference: {self.charset_mode}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("Candidate seed must be an integer.")
        if not isinstance(self.allowed_chars, str) or not isinstance(self.pattern, str):
            raise ValueError("Candidate pattern and allowed_chars must be strings.")

    def is_default_exhaustive(self) -> bool:
        return (
            self.strategy == "exhaustive"
            and self.charset_mode == "all"
            and not self.allowed_chars
            and self.pattern == "any"
            and not self.priority_names
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "charset_mode": self.charset_mode,
            "allowed_chars": self.allowed_chars,
            "pattern": self.pattern,
            "seed": self.seed,
            "priority_names": list(self.priority_names),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CandidatePreferences:
        if not data or not isinstance(data, dict):
            return cls()
        raw_priority = data.get("priority_names") or ()
        raw_seed = data.get("seed", 42)
        if isinstance(raw_seed, bool):
            raise ValueError("Candidate seed must be an integer.")
        return cls(
            strategy=str(data.get("strategy", "exhaustive")),
            charset_mode=str(data.get("charset_mode", "all")),
            allowed_chars=str(data.get("allowed_chars", "")),
            pattern=str(data.get("pattern", "any")),
            seed=int(raw_seed),
            priority_names=tuple(str(item) for item in raw_priority if item),
        )


def _validate_positive_int_budget(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"Maximum {label} budget must be a positive integer.")


def _validate_positive_float_budget(value: Any, label: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise ValueError(f"Maximum {label} budget must be a finite positive number.")


@dataclass(frozen=True)
class RequestBudget:
    max_network_requests: int | None = None
    max_definitive_checks: int | None = None
    max_duration_seconds: float | None = None
    max_consecutive_failures: int | None = 3

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _validate_positive_int_budget(self.max_network_requests, "network requests")
        _validate_positive_int_budget(self.max_definitive_checks, "definitive checks")
        _validate_positive_float_budget(self.max_duration_seconds, "active duration")
        _validate_positive_int_budget(self.max_consecutive_failures, "consecutive failures")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_network_requests": self.max_network_requests,
            "max_definitive_checks": self.max_definitive_checks,
            "max_duration_seconds": self.max_duration_seconds,
            "max_consecutive_failures": self.max_consecutive_failures,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RequestBudget:
        if not data or not isinstance(data, dict):
            return cls()
        for key in ("max_network_requests", "max_definitive_checks", "max_duration_seconds", "max_consecutive_failures"):
            if isinstance(data.get(key), bool):
                raise ValueError(f"Budget field '{key}' cannot be a boolean.")
        return cls(
            max_network_requests=int(data["max_network_requests"])
            if data.get("max_network_requests") is not None
            else None,
            max_definitive_checks=int(data["max_definitive_checks"])
            if data.get("max_definitive_checks") is not None
            else None,
            max_duration_seconds=float(data["max_duration_seconds"])
            if data.get("max_duration_seconds") is not None
            else None,
            max_consecutive_failures=int(data["max_consecutive_failures"])
            if data.get("max_consecutive_failures") is not None
            else 3,
        )


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    state: ProviderHealthState = ProviderHealthState.READY
    reason: str = ""
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0
    retry_at: float = 0.0
    updated_at: str = field(default_factory=utc_now)

    @property
    def is_circuit_open(self) -> bool:
        return self.state in (
            ProviderHealthState.PAUSED_FAILURES,
            ProviderHealthState.BLOCKED_CHALLENGE,
            ProviderHealthState.UNINTERPRETABLE_RESPONSE,
        )


@dataclass(frozen=True)
class RateLimitMetadata:
    retry_after: float | None = None
    limit: int | None = None
    remaining: float | None = None
    reset_after: float | None = None
    reset_at: float | None = None
    bucket: str = ""
    scope: str = ""
    is_global: bool = False
    effective_delay: float = 0.0
    reason: str = ""

    @property
    def retry_after_seconds(self) -> float:
        return self.retry_after if self.retry_after is not None else self.effective_delay


@dataclass(frozen=True)
class RequestDiagnosticEntry:
    id: int
    scan_id: str
    provider: str
    scope: str
    route: str
    username: str
    started_at: str
    completed_at: str
    http_status: int
    latency_ms: float
    classification: str
    origin: str
    ratelimit_limit: int | None = None
    ratelimit_remaining: float | None = None
    ratelimit_reset_after: float | None = None
    ratelimit_bucket: str = ""
    ratelimit_scope: str = ""
    ratelimit_global: bool = False
    retry_at: float = 0.0
    retry_reason: str = ""
    wait_seconds: float = 0.0
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0

    @property
    def detail(self) -> str:
        return self.retry_reason or self.classification


@dataclass(frozen=True)
class DiagnosticsSummary:
    definitive_network_checks: int = 0
    cached_observations_reused: int = 0
    requests_avoided_deduplication: int = 0
    requests_avoided_local_validation: int = 0
    total_wait_seconds: float = 0.0
    network_requests_total: int = 0
    recent_success_count: int = 0
    recent_rate_limit_count: int = 0
    recent_failure_count: int = 0
    consecutive_failures: int = 0
    consecutive_rate_limits: int = 0
    effective_definitive_checks_per_minute: float | None = None
    estimated_remaining_seconds: float | None = None
    provider_health: ProviderHealth = field(default_factory=lambda: ProviderHealth(provider=PUBLIC_USERNAME_SCOPE))

    @property
    def total_network_checks(self) -> int:
        return self.definitive_network_checks

    @property
    def total_cache_hits(self) -> int:
        return self.cached_observations_reused

    @property
    def total_deduplicated(self) -> int:
        return self.requests_avoided_deduplication

    @property
    def total_local_invalid(self) -> int:
        return self.requests_avoided_local_validation

    @property
    def effective_checks_per_minute(self) -> float | None:
        return self.effective_definitive_checks_per_minute

    @property
    def eta_label(self) -> str:
        return self.format_eta()

    @property
    def total_requests_avoided(self) -> int:
        return (
            self.cached_observations_reused
            + self.requests_avoided_deduplication
            + self.requests_avoided_local_validation
        )

    def format_eta(self) -> str:
        if self.estimated_remaining_seconds is None:
            return "insufficient data"
        seconds = max(0, int(round(self.estimated_remaining_seconds)))
        if seconds < 60:
            return f"~{seconds}s"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"~{minutes}m {sec:02d}s"
        hours, mins = divmod(minutes, 60)
        if hours < 48:
            return f"~{hours}h {mins:02d}m"
        days = hours / 24.0
        return f"~{days:,.1f} days"


class _CallableString(str):
    """String subclass that can also be called with 0 arguments for property/method compatibility."""

    def __call__(self) -> str:
        return str(self)


@dataclass(frozen=True)
class UsernameCheckResult:
    username: str
    status: UsernameStatus
    checked_at: str
    message: str = ""
    origin: str = "network"  # "network", "cache", "stale_cache", "local"
    provider: str = ""
    expires_at: str = ""
    reused_at: str = ""
    observed_epoch: float | None = field(default=None, repr=False, compare=False)
    expires_epoch: float | None = field(default=None, repr=False, compare=False)
    _clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)

    def with_clock(self, clock: Callable[[], float] | None) -> UsernameCheckResult:
        return replace(self, _clock=clock)

    def _resolve_now(self, now: float | None = None) -> float:
        if now is not None:
            return float(now)
        if self._clock is not None:
            return float(self._clock())
        return time.time()

    def effective_observed_epoch(self) -> float | None:
        if self.observed_epoch is not None and math.isfinite(self.observed_epoch):
            return float(self.observed_epoch)
        return parse_utc_timestamp(self.checked_at)

    def effective_expires_epoch(self) -> float | None:
        if self.expires_epoch is not None and math.isfinite(self.expires_epoch):
            return float(self.expires_epoch)
        if self.expires_at:
            return parse_utc_timestamp(self.expires_at)
        obs = self.effective_observed_epoch()
        if obs is None:
            return None
        if self.status == UsernameStatus.INVALID:
            return None
        ttl = (
            DEFAULT_AVAILABLE_TTL_SECONDS
            if self.status == UsernameStatus.AVAILABLE
            else DEFAULT_UNAVAILABLE_TTL_SECONDS
        )
        return obs + ttl

    @property
    def is_cached(self) -> bool:
        return self.origin in (ObservationOrigin.CACHE.value, ObservationOrigin.STALE_CACHE.value)

    def is_expired(self, now: float | None = None) -> bool:
        if self.origin == ObservationOrigin.STALE_CACHE.value:
            return True
        if self.status == UsernameStatus.ERROR:
            return False
        if self.status == UsernameStatus.INVALID:
            if not self.expires_at and self.expires_epoch is None:
                return False
            exp_epoch = self.effective_expires_epoch()
            if exp_epoch is None:
                return True
            return self._resolve_now(now) >= exp_epoch

        obs_epoch = self.effective_observed_epoch()
        if obs_epoch is None or obs_epoch <= 0.0:
            return True
        exp_epoch = self.effective_expires_epoch()
        if exp_epoch is None or exp_epoch <= 0.0:
            return True
        return self._resolve_now(now) >= exp_epoch

    def is_fresh(self, now: float | None = None) -> bool:
        if self.status == UsernameStatus.ERROR:
            return False
        return not self.is_expired(now)

    @property
    def is_stale(self) -> bool:
        return self.is_expired()
