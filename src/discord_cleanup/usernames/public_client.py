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


def _date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def parse_rate_limit_metadata(
    response: Any,
    payload: Any,
    now: float,
    *,
    safety_margin: float = 0.25,
    positive_jitter: float = 0.0,
    default_retry: float = 60.0,
) -> RateLimitMetadata:
    if isinstance(response, dict):
        raw_headers: dict[str, Any] = response
        status_code = (
            429
            if (
                "Retry-After" in response
                or "retry-after" in response
                or (isinstance(payload, dict) and "retry_after" in payload)
            )
            else 200
        )
    else:
        raw_headers = getattr(response, "headers", None) or {}
        status_code = getattr(response, "status_code", 200)
    headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
    delays: list[float] = []

    retry = headers.get("retry-after")
    retry_seconds = _seconds(retry)
    if retry_seconds is None and retry:
        retry_date = _date(retry)
        reference_date = _date(headers.get("date"))
        if retry_date is not None:
            retry_seconds = max(0.0, retry_date - (reference_date if reference_date is not None else now))
    if retry_seconds is not None:
        delays.append(retry_seconds)

    body_delay: float | None = None
    if isinstance(payload, dict):
        body_delay = _seconds(payload.get("retry_after"))
        if body_delay is not None and (status_code == 429 or body_delay > 0):
            delays.append(body_delay)

    limit = _int_value(headers.get("x-ratelimit-limit"))
    remaining = _seconds(headers.get("x-ratelimit-remaining"))
    reset_after = _seconds(headers.get("x-ratelimit-reset-after"))
    reset_raw = _seconds(headers.get("x-ratelimit-reset"))
    reset_delay: float | None = reset_after
