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
