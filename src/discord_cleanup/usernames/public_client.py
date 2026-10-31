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

    def __init__(
        self,
        store: UsernameScanStore,
        transport: Transport | None = None,
        base_url: str = BASE_URL,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.time,
        wait: Callable[[threading.Event, float], bool] | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        safety_margin: float = 0.25,
        jitter: float | Callable[[], float] = 0.0,
        rng: Callable[[], float] | None = None,
        freshness_policy: CacheFreshnessPolicy | None = None,
        max_network_retries: int = 3,
    ):
        if isinstance(min_interval, bool) or not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("Invalid request interval.")
        self.store = store
        self.transport = transport if transport is not None else RequestsTransport(allow_redirects=False)
        self.url = base_url.rstrip("/") + PUBLIC_USERNAME_ENDPOINT
        self.min_interval = min_interval
        self.clock = clock
        self.monotonic = monotonic
        self._uses_custom_clock = clock is not time.time
        self.wait = wait if wait is not None else lambda event, seconds: event.wait(seconds)
        self.safety_margin = max(0.0, float(safety_margin)) if math.isfinite(safety_margin) else 0.25
        self.jitter = jitter
        self.rng = rng
        self.freshness_policy = freshness_policy or CacheFreshnessPolicy()
        self.max_network_retries = max(1, int(max_network_retries))
        self._mono_deadlines: dict[str, tuple[float, float]] = {}
        self._consecutive_failures = 0
        self._consecutive_rate_limits = 0

    def _compute_jitter(self) -> float:
        if callable(self.jitter):
            raw = self.jitter()
        elif self.rng is not None and isinstance(self.jitter, (int, float)) and not isinstance(self.jitter, bool):
            raw = float(self.jitter) * float(self.rng())
        else:
            raw = self.jitter
        if isinstance(raw, bool):
            return 0.0
        try:
            val = float(raw)
        except (ValueError, TypeError, OverflowError):
            return 0.0
        # Jitter must strictly be >= 0 so it can never cause an early retry.
        return val if (math.isfinite(val) and val > 0) else 0.0

    def with_random_jitter(self, max_jitter_seconds: float = 0.35) -> PublicUsernameClient:
        bound = max(0.0, float(max_jitter_seconds))
        self.jitter = (lambda: random.uniform(0.0, bound)) if bound > 0 else 0.0
        return self

    def clear_token(self) -> None:
        # Kept compatible with the existing worker lifecycle; public checks never receive a token.
        self.token = ""

    def close(self) -> None:
        self.transport.close()

    def _cancel_if_needed(self, event: threading.Event) -> None:
        if event.is_set():
            raise RequestCancelledError("Public username check paused. Progress and cooldown are saved.")

    def _record_deadline(
        self,
        delay: float,
        reason: str,
        *,
        bucket: str = "",
        is_global: bool = False,
    ) -> float:
        safe_delay = max(0.0, float(delay)) if math.isfinite(delay) else 60.0
        now_utc = self.clock()
        deadline_utc = now_utc + safe_delay
        self.store.save_cooldown(
            self.url,
            deadline_utc,
            reason,
            scope=self.scope,
            bucket=bucket,
            is_global=is_global,
        )
        if not self._uses_custom_clock:
            now_mono = self.monotonic()
            prev_utc, prev_mono = self._mono_deadlines.get(self.url, (0.0, 0.0))
            target_mono = max(prev_mono, now_mono + safe_delay)
            self._mono_deadlines[self.url] = (max(prev_utc, deadline_utc), target_mono)
        return deadline_utc

    def _remaining_cooldown(self) -> tuple[float, str, float]:
        deadline, reason = self.store.cooldown(self.url, scope=self.scope)
        now_utc = self.clock()
        utc_remaining = deadline - now_utc
        if not self._uses_custom_clock:
            now_mono = self.monotonic()
            if self.url in self._mono_deadlines:
                tracked_utc, tracked_mono = self._mono_deadlines[self.url]
                if deadline > tracked_utc + 0.5:
                    # Another process extended the SQLite deadline; adopt and protect it monotonically
                    new_mono = max(tracked_mono, now_mono + max(0.0, utc_remaining))
                    self._mono_deadlines[self.url] = (deadline, new_mono)
                    return max(utc_remaining, new_mono - now_mono), reason, deadline
                if abs(tracked_utc - deadline) <= 0.5:
                    mono_remaining = tracked_mono - now_mono
                    # Guard against forward wall-clock jumps shortening an active wait
                    return max(utc_remaining, mono_remaining), reason, deadline
            elif utc_remaining > 0:
                self._mono_deadlines[self.url] = (deadline, now_mono + utc_remaining)
        return utc_remaining, reason, deadline

    @staticmethod
    def _resolve_deadline(
        max_wait_deadline_mono: float | Callable[[], float | None] | None,
    ) -> float | None:
        if callable(max_wait_deadline_mono):
            return max_wait_deadline_mono()
        return max_wait_deadline_mono

    def _wait_until_ready(
        self,
        event: threading.Event,
        on_backoff: Callable[[float], None] | None,
        *,
        max_wait_deadline_mono: float | Callable[[], float | None] | None = None,
    ) -> float:
        last_notification = -math.inf
        waited_total = 0.0
        while True:
            self._cancel_if_needed(event)
            remaining, reason, deadline = self._remaining_cooldown()
            if remaining <= 0:
                return waited_total
            resolved_deadline = self._resolve_deadline(max_wait_deadline_mono)
            if resolved_deadline is not None:
                current_clock = self.clock() if self._uses_custom_clock else self.monotonic()
                budget_remaining = resolved_deadline - current_clock
                if budget_remaining <= 0:
                    raise RequestCancelledError("Active duration budget reached during cooldown wait.")
            if reason.startswith("Rate limited") or reason.startswith("Global rate limit"):
                self.store.save_provider_health(
                    self.scope,
                    ProviderHealthState.WAITING_COOLDOWN,
                    reason,
                    consecutive_failures=self._consecutive_failures,
                    consecutive_rate_limits=self._consecutive_rate_limits,
                    retry_at=deadline,
                )
            if (
                on_backoff is not None
                and (reason == "Rate limited" or reason.startswith("Global rate limit"))
                and self.clock() - last_notification >= 1
            ):
                on_backoff(remaining)
                last_notification = self.clock()
            step = min(remaining, 1.0)
            resolved_deadline = self._resolve_deadline(max_wait_deadline_mono)
            if resolved_deadline is not None:
                current_clock = self.clock() if self._uses_custom_clock else self.monotonic()
                step = min(step, max(0.01, resolved_deadline - current_clock))
            before = self.clock()
            if self.wait(event, step):
                self._cancel_if_needed(event)
            after = self.clock()
            waited_total += max(0.0, after - before) if after > before else step

    def _save_response_cooldown(self, response: Any, payload: Any) -> RateLimitMetadata:
        now = self.clock()
        metadata = parse_rate_limit_metadata(
            response,
            payload,
            now,
            safety_margin=self.safety_margin,
            positive_jitter=self._compute_jitter(),
        )
        if metadata.effective_delay > 0:
            self._record_deadline(
                metadata.effective_delay,
                metadata.reason or "Rate limited",
                bucket=metadata.bucket,
                is_global=metadata.is_global,
            )
        return metadata

    def _sync_and_enforce_circuit_ready(self) -> None:
        health = self.store.get_provider_health(self.scope, route=self.url, now=self.clock())
        self._consecutive_failures = max(0, int(health.consecutive_failures))
        self._consecutive_rate_limits = max(0, int(health.consecutive_rate_limits))
        if not health.is_circuit_open:
            return
        reason = health.reason or f"Provider circuit breaker is open ({health.state.label}). Reset health before retrying."
        if health.state == ProviderHealthState.BLOCKED_CHALLENGE:
            raise UpstreamBlockedError(reason)
        if health.state == ProviderHealthState.UNINTERPRETABLE_RESPONSE:
            raise InvalidPayloadError(reason)
        raise NetworkError(reason)

    @staticmethod
    def _is_redirect_response(response: Any, expected_url: str) -> bool:
        status_code = getattr(response, "status_code", 200)
        if isinstance(status_code, int) and 300 <= status_code < 400:
            return True
        if getattr(response, "is_redirect", False) is True or getattr(response, "is_permanent_redirect", False) is True:
            return True
        raw_headers = getattr(response, "headers", None) or {}
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        if headers.get("location"):
            return True
        final_url = getattr(response, "url", expected_url)
        if isinstance(final_url, str) and final_url.rstrip("/") != expected_url.rstrip("/"):
            return True
        return False

    def check_username_availability(
        self,
        username: str,
        cancel_event: threading.Event | None = None,
        on_backoff: Callable[[float], None] | None = None,
        *,
        scan_id: str = "",
        use_cache: bool = False,
        force_recheck: bool = False,
        on_network_request: Callable[[], None] | None = None,
        can_dispatch_network: Callable[[], bool] | None = None,
        max_wait_deadline_mono: float | Callable[[], float | None] | None = None,
    ) -> UsernameCheckResult:
        event = cancel_event if cancel_event is not None else threading.Event()
        self._cancel_if_needed(event)
        now_ts = self.clock()
        now_iso = format_utc_timestamp(now_ts) if self._uses_custom_clock else utc_now()
        validation = validate_username(username)
        if validation:
            invalid_res = UsernameCheckResult(
                username,
                UsernameStatus.INVALID,
                now_iso,
                validation,
                origin=ObservationOrigin.LOCAL.value,
                provider="public",
            )
            self.store.record_diagnostic(
                provider="public",
                scope=self.scope,
                route=PUBLIC_USERNAME_ENDPOINT,
                username=username,
                classification="invalid",
                origin="local",
                scan_id=scan_id,
                started_at=now_iso,
                completed_at=now_iso,
            )
            return invalid_res

        if use_cache and not force_recheck and self.freshness_policy.use_cache:
            cached = self.store.get_cached_observation(
                username, provider="public", scope=self.scope, now=self.clock()
            )
            if cached is not None:
                self.store.record_diagnostic(
                    provider="public",
                    scope=self.scope,
                    route=PUBLIC_USERNAME_ENDPOINT,
                    username=username,
                    classification=cached.status.value,
                    origin="cache",
                    scan_id=scan_id,
                    started_at=now_iso,
                    completed_at=now_iso,
                )
                return cached

        self._sync_and_enforce_circuit_ready()
        if can_dispatch_network is not None and not can_dispatch_network():
            raise RequestCancelledError("Network request budget reached before dispatch.")

        failures = 0
        while True:
            self._sync_and_enforce_circuit_ready()
            waited_s = self._wait_until_ready(
                event, on_backoff, max_wait_deadline_mono=max_wait_deadline_mono
            )
            try:
                with self.store.public_request_lock():
                    self._cancel_if_needed(event)
                    self._sync_and_enforce_circuit_ready()
                    if self.store.cooldown(self.url, scope=self.scope)[0] > self.clock():
                        continue
                    if can_dispatch_network is not None and not can_dispatch_network():
                        raise RequestCancelledError("Network request budget reached before dispatch.")
                    resolved_deadline = self._resolve_deadline(max_wait_deadline_mono)
                    if resolved_deadline is not None:
                        current_clock = self.clock() if self._uses_custom_clock else self.monotonic()
                        if current_clock >= resolved_deadline:
                            raise RequestCancelledError("Active duration budget reached before dispatch.")
                    if on_network_request is not None:
                        on_network_request()
                    self._cancel_if_needed(event)
                    # Reserve the slot before dispatch; another process cannot race this request.
                    self._record_deadline(self.min_interval, "Request pacing")
                    req_start_clock = self.clock()
                    req_start_iso = format_utc_timestamp(req_start_clock) if self._uses_custom_clock else utc_now()
                    try:
                        response = self.transport.request(
                            "POST",
                            self.url,
                            json_data={"username": username},
                            cancel_event=event,
                        )
                    except (NetworkError, RequestTimeoutError) as exc:
                        failures += 1
                        self._consecutive_failures += 1
                        total_consec = max(failures, self._consecutive_failures)
                        backoff_delay = float(2**failures) + self._compute_jitter()
                        retry_deadline = self._record_deadline(backoff_delay, "Network retry")
                        req_end_iso = format_utc_timestamp(self.clock()) if self._uses_custom_clock else utc_now()
                        state = (
                            ProviderHealthState.PAUSED_FAILURES
                            if total_consec >= self.max_network_retries
                            else ProviderHealthState.BACKING_OFF
                        )
                        self.store.save_provider_health(
                            self.scope,
                            state,
                            "Public check failed after three connection attempts. Availability is unverified."
                            if total_consec >= self.max_network_retries
                            else f"Connection failure ({total_consec}/{self.max_network_retries}); backing off.",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=retry_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="network_error",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=0,
                            latency_ms=max(0.0, (self.clock() - req_start_clock) * 1000.0),
                            retry_at=retry_deadline,
                            retry_reason="Network retry",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        if total_consec >= self.max_network_retries:
                            raise type(exc)(
                                "Public check failed after three connection attempts. Availability is unverified."
                            ) from None
                        if can_dispatch_network is not None and not can_dispatch_network():
                            raise RequestCancelledError("Network request budget reached.") from None
                        continue

                    latency_ms = max(0.0, (self.clock() - req_start_clock) * 1000.0)
                    req_end_iso = format_utc_timestamp(self.clock()) if self._uses_custom_clock else utc_now()
                    try:
                        payload = response.json()
                    except (ValueError, TypeError):
                        payload = None

                    rl_meta = self._save_response_cooldown(response, payload)
                    active_deadline, active_reason = self.store.cooldown(self.url, scope=self.scope)

                    if self._is_redirect_response(response, self.url):
                        self._consecutive_failures += 1
                        self.store.save_provider_health(
                            self.scope,
                            ProviderHealthState.UNINTERPRETABLE_RESPONSE,
                            "Discord redirected the public check. Availability is unverified.",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=active_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="uninterpretable",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=response.status_code,
                            latency_ms=latency_ms,
                            retry_at=active_deadline,
                            retry_reason="Redirected response",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        raise InvalidPayloadError("Discord redirected the public check. Availability is unverified.")

                    resp_text_lower = getattr(response, "text", "").lower()
                    if "<html" in resp_text_lower or response.status_code in (401, 403):
                        self._consecutive_failures += 1
                        self.store.save_provider_health(
                            self.scope,
                            ProviderHealthState.BLOCKED_CHALLENGE,
                            "Discord blocked the public check. Availability is unverified.",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=active_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="blocked_challenge",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=response.status_code,
                            latency_ms=latency_ms,
                            ratelimit_limit=rl_meta.limit,
                            ratelimit_remaining=rl_meta.remaining,
                            ratelimit_reset_after=rl_meta.reset_after,
                            ratelimit_bucket=rl_meta.bucket,
                            ratelimit_scope=rl_meta.scope,
                            ratelimit_global=rl_meta.is_global,
                            retry_at=active_deadline,
                            retry_reason=active_reason or "Blocked",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        raise UpstreamBlockedError("Discord blocked the public check. Availability is unverified.")

                    if isinstance(payload, dict) and any(key.startswith("captcha_") for key in payload):
                        self._consecutive_failures += 1
                        self.store.save_provider_health(
                            self.scope,
                            ProviderHealthState.BLOCKED_CHALLENGE,
                            "Discord requires a verification challenge. Availability is unverified.",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=active_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="blocked_challenge",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=response.status_code,
                            latency_ms=latency_ms,
                            retry_at=active_deadline,
                            retry_reason="Verification challenge",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        raise UpstreamBlockedError(
                            "Discord requires a verification challenge. Availability is unverified."
                        )

                    if response.status_code == 429:
                        self._consecutive_rate_limits += 1
                        self.store.save_provider_health(
                            self.scope,
                            ProviderHealthState.WAITING_COOLDOWN,
                            active_reason or "Rate limited",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=active_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="rate_limited",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=429,
                            latency_ms=latency_ms,
                            ratelimit_limit=rl_meta.limit,
                            ratelimit_remaining=rl_meta.remaining,
                            ratelimit_reset_after=rl_meta.reset_after,
                            ratelimit_bucket=rl_meta.bucket,
                            ratelimit_scope=rl_meta.scope,
                            ratelimit_global=rl_meta.is_global,
                            retry_at=active_deadline,
                            retry_reason=active_reason or "Rate limited",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        if can_dispatch_network is not None and not can_dispatch_network():
                            raise RequestCancelledError("Network request budget reached.")
                        continue

                    if response.status_code >= 500:
                        failures += 1
                        self._consecutive_failures += 1
                        total_consec = max(failures, self._consecutive_failures)
                        retry_deadline = self._record_deadline(
                            float(2**failures) + self._compute_jitter(), "Network retry"
                        )
                        state = (
                            ProviderHealthState.PAUSED_FAILURES
                            if total_consec >= self.max_network_retries
                            else ProviderHealthState.BACKING_OFF
                        )
                        self.store.save_provider_health(
                            self.scope,
                            state,
                            f"Server error HTTP {response.status_code}",
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                            retry_at=retry_deadline,
                        )
                        self.store.record_diagnostic(
                            provider="public",
                            scope=self.scope,
                            route=PUBLIC_USERNAME_ENDPOINT,
                            username=username,
                            classification="network_error",
                            origin="network",
                            scan_id=scan_id,
                            started_at=req_start_iso,
                            completed_at=req_end_iso,
                            http_status=response.status_code,
                            latency_ms=latency_ms,
                            retry_at=retry_deadline,
                            retry_reason="Network retry",
                            wait_seconds=waited_s,
                            consecutive_failures=self._consecutive_failures,
                            consecutive_rate_limits=self._consecutive_rate_limits,
                        )
                        if total_consec < self.max_network_retries:
                            if can_dispatch_network is not None and not can_dispatch_network():
                                raise RequestCancelledError("Network request budget reached.")
                            continue

                    if response.status_code == 200 and isinstance(payload, dict) and type(payload.get("taken")) is bool:
                        status = UsernameStatus.UNAVAILABLE if payload["taken"] else UsernameStatus.AVAILABLE
                        self._consecutive_failures = 0
                        self._consecutive_rate_limits = 0
                        next_state = (
                            ProviderHealthState.WAITING_COOLDOWN
                            if (rl_meta.effective_delay > 0)
                            else ProviderHealthState.READY
                        )
                        self.store.save_provider_health(
                            self.scope,
                            next_state,
                            rl_meta.reason if rl_meta.effective_delay > 0 else "",
                            consecutive_failures=0,
                            consecutive_rate_limits=0,
                            retry_at=active_deadline if rl_meta.effective_delay > 0 else 0.0,
                        )
                        obs_clock = self.clock()
                        obs_time = format_utc_timestamp(obs_clock) if self._uses_custom_clock else utc_now()
                        raw_result = UsernameCheckResult(
                            username,
                            status,
                            obs_time,
                            origin=ObservationOrigin.NETWORK.value,
                            provider="public",
                            observed_epoch=obs_clock,
                        )
                        cached_result = self.store.save_cached_observation(
                            raw_result,
                            provider="public",
                            scope=self.scope,
                            policy=self.freshness_policy,
                            now=obs_clock,
                        )
            except Timeout:
                pass
