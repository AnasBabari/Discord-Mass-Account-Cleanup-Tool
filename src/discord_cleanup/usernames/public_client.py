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
    if reset_delay is None and reset_raw is not None:
        reference_date = _date(headers.get("date"))
        ref = reference_date if reference_date is not None else now
        # Distinguish epoch timestamps (>= 1e9) from relative delta values
        reset_delay = max(0.0, reset_raw - ref) if reset_raw >= 1_000_000_000 else max(0.0, reset_raw)

    if (status_code == 429 or remaining == 0) and reset_delay is not None:
        delays.append(reset_delay)

    bucket = headers.get("x-ratelimit-bucket", "").strip()
    rl_scope = headers.get("x-ratelimit-scope", "").strip().lower()
    header_global = headers.get("x-ratelimit-global", "").strip().lower() in ("true", "1")
    body_global = bool(isinstance(payload, dict) and payload.get("global") is True)
    is_global = header_global or body_global or rl_scope == "global"

    safe_jitter = max(0.0, float(positive_jitter)) if math.isfinite(positive_jitter) else 0.0
    safe_margin = max(0.0, float(safety_margin)) if math.isfinite(safety_margin) else 0.25

    effective_delay = 0.0
    reason = ""
    base_delay = max(delays, default=default_retry)
    if status_code == 429 or delays or is_global:
        effective_delay = base_delay + safe_margin + safe_jitter
        reason = "Rate limited"

    return RateLimitMetadata(
        retry_after=max(delays) if delays else (retry_seconds if retry_seconds is not None else body_delay),
        limit=limit,
        remaining=remaining,
        reset_after=reset_delay,
        reset_at=(now + reset_delay) if reset_delay is not None else None,
        bucket=bucket,
        scope=rl_scope,
        is_global=is_global,
        effective_delay=effective_delay,
        reason=reason,
    )


class PublicUsernameClient:
    """Discord's public signup check, with durable, process-shared request scheduling.

    The endpoint is undocumented. Only a successful, explicit Boolean `taken` is
    treated as evidence. No profile or server-invite lookup is used as a substitute.
    """

    token = ""
    scope = PUBLIC_USERNAME_SCOPE
