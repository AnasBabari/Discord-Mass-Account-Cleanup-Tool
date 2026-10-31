from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from filelock import Timeout

from discord_cleanup.api.client import BASE_URL
from discord_cleanup.api.exceptions import (
    InvalidPayloadError,
    NetworkError,
    RequestCancelledError,
    RequestTimeoutError,
    UpstreamBlockedError,
)
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    ObservationOrigin,
    ProviderHealthState,
    RateLimitMetadata,
    UsernameCheckResult,
    UsernameStatus,
    format_utc_timestamp,
    utc_now,
)
from discord_cleanup.transport.http_transport import RequestsTransport, Transport
from discord_cleanup.usernames.candidates import validate_username
from discord_cleanup.usernames.store import UsernameScanStore

PUBLIC_USERNAME_ENDPOINT = "/unique-username/username-attempt-unauthed"
PUBLIC_CHECK_URL = BASE_URL + PUBLIC_USERNAME_ENDPOINT


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if parsed >= 0 else None
