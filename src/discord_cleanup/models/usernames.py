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
