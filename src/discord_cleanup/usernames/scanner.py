from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from filelock import Timeout

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.api.exceptions import (
    DiscordCleanupError,
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
    RequestBudget,
    UsernameCheckResult,
    UsernameProgress,
    UsernameScan,
    UsernameStatus,
    format_utc_timestamp,
    utc_now,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.usernames.candidates import (
    SUPPORTED_GENERATOR_VERSIONS,
    next_generated_candidate,
    validate_username,
)
from discord_cleanup.usernames.public_client import (
    PUBLIC_CHECK_URL,
    PUBLIC_USERNAME_ENDPOINT,
    PublicUsernameClient,
)
from discord_cleanup.usernames.store import UsernameScanStore

logger = logging.getLogger(__name__)


class ScanBusyError(DiscordCleanupError):
    """Another process or worker is already running this saved scan."""


class _WaiterSlot:
    def __init__(
        self,
        *,
        scan_id: str,
        cancel_event: threading.Event,
        outer_cancel_event: threading.Event | None = None,
        request_started_epoch: float,
        force_recheck: bool,
        max_wait_deadline_mono: float | None,
        is_deadline_expired_fn: Callable[[], bool],
        can_dispatch_fn: Callable[[], bool],
        try_reserve_fn: Callable[[], bool],
        release_reserved_fn: Callable[[], None],
        commit_reserved_fn: Callable[[], None],
        on_backoff: Callable[[float], None] | None = None,
        before_commit_hook: Callable[[], None] | None = None,
    ):
        self.scan_id = scan_id
        self.cancel_event = cancel_event
        self.outer_cancel_event = outer_cancel_event
        self.request_started_epoch = request_started_epoch
        self.force_recheck = force_recheck
        self.max_wait_deadline_mono = max_wait_deadline_mono
        self.is_deadline_expired_fn = is_deadline_expired_fn
        self.can_dispatch_fn = can_dispatch_fn
        self.try_reserve_fn = try_reserve_fn
        self.release_reserved_fn = release_reserved_fn
        self.commit_reserved_fn = commit_reserved_fn
        self.on_backoff = on_backoff
        self.before_commit_hook = before_commit_hook
        self.attempts_committed = 0
        self.expired = False

    def is_active(self) -> bool:
        if self.outer_cancel_event is not None and self.outer_cancel_event.is_set():
            self.cancel_event.set()
            return False
        if self.cancel_event.is_set() or self.expired:
            return False
        if self.is_deadline_expired_fn():
            self.expired = True
            self.cancel_event.set()
            return False
        return True


class _InFlightEntry:
    def __init__(self, first_slot: _WaiterSlot, started_epoch: float, use_cache: bool):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.shared_cancel = threading.Event()
        self.slots: list[_WaiterSlot] = [first_slot]
        self.started_epoch = started_epoch
        self.allow_cache = use_cache and not first_slot.force_recheck
        self.total_attempts_committed = 0
        self.cross_process_dedup = False
        self.last_committer_slot: _WaiterSlot | None = None
        self.worker_thread: threading.Thread | None = None
        self.result: UsernameCheckResult | None = None
        self.error: BaseException | None = None
        self._sync_shared_cancel_unlocked()

    def add_waiter(self, slot: _WaiterSlot) -> bool:
        with self.lock:
            if self.done.is_set():
                return False
            self.slots.append(slot)
            if slot.force_recheck:
                self.allow_cache = False
            self._sync_shared_cancel_unlocked()
            return True

    def remove_waiter(self, slot: _WaiterSlot) -> None:
        with self.lock:
            slot.cancel_event.set()
            if slot in self.slots:
                self.slots.remove(slot)
            self._sync_shared_cancel_unlocked()

    def sync_cancel(self) -> None:
        with self.lock:
            self._sync_shared_cancel_unlocked()

    def active_scan_id(self, fallback: str = "") -> str:
        with self.lock:
            for slot in self.slots:
                if slot.is_active() and slot.scan_id:
                    return slot.scan_id
            return fallback

    def required_observed_epoch(self) -> float | None:
        with self.lock:
            self._sync_shared_cancel_unlocked()
            epochs = [s.request_started_epoch for s in self.slots if s.is_active() and s.force_recheck]
            return max(epochs) if epochs else None

    def effective_deadline_mono(self) -> float | None:
        with self.lock:
            self._sync_shared_cancel_unlocked()
            active = [s for s in self.slots if s.is_active()]
            if not active:
                self.shared_cancel.set()
                return 0.0
            if any(s.max_wait_deadline_mono is None for s in active):
                return None
            return max(s.max_wait_deadline_mono for s in active if s.max_wait_deadline_mono is not None)

    def notify_backoff(self, seconds: float) -> None:
        with self.lock:
            callbacks = [s.on_backoff for s in self.slots if s.is_active() and s.on_backoff is not None]
        for cb in callbacks:
            try:
                cb(seconds)
            except Exception:
                pass

    def can_any_active_waiter_dispatch(self) -> bool:
        with self.lock:
            self._sync_shared_cancel_unlocked()
            for slot in self.slots:
                if slot.is_active() and slot.can_dispatch_fn():
                    return True
            return False

    def reserve_and_commit_attempt(self) -> None:
        """Atomically select an eligible active consumer, reserve its budget, verify liveness, and commit."""
        with self.lock:
            self._sync_shared_cancel_unlocked()
            for slot in list(self.slots):
                if not slot.is_active():
                    continue
                if not slot.try_reserve_fn():
                    continue
                try:
                    if slot.before_commit_hook is not None:
                        slot.before_commit_hook()
                except BaseException:
                    slot.release_reserved_fn()
                    self._sync_shared_cancel_unlocked()
                    raise
                if not slot.is_active():
                    # Cancelled or expired between reservation and dispatch: roll back reservation and transfer!
                    slot.release_reserved_fn()
                    self._sync_shared_cancel_unlocked()
                    continue
                slot.commit_reserved_fn()
                slot.attempts_committed += 1
                self.total_attempts_committed += 1
                self.last_committer_slot = slot
                return
            self._sync_shared_cancel_unlocked()
            if self.shared_cancel.is_set():
                raise RequestCancelledError("Username check cancelled before dispatch.")
            raise RequestCancelledError("Network request budget reached before dispatch.")

    def _sync_shared_cancel_unlocked(self) -> None:
        active = [s for s in self.slots if s.is_active()]
        if not active:
            self.shared_cancel.set()
        else:
            self.shared_cancel.clear()


_INFLIGHT_MUTEX = threading.Lock()
_INFLIGHT_MAP: dict[tuple[str, str, str, str], _InFlightEntry] = {}
_ACTIVE_SHARED_THREADS: set[threading.Thread] = set()


def wait_for_active_shared_requests(timeout: float = 2.0) -> bool:
    """Wait up to `timeout` seconds for any background shared-request worker threads to finish."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        with _INFLIGHT_MUTEX:
            threads = list(_ACTIVE_SHARED_THREADS)
        if not threads:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        threads[0].join(timeout=min(0.05, remaining))


class UsernameScanner:
    def __init__(
        self,
        store: UsernameScanStore,
        client: DiscordApiClient | PublicUsernameClient,
        *,
        freshness_policy: CacheFreshnessPolicy | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        before_commit_hook: Callable[[], None] | None = None,
    ):
        self.store = store
        self.client = client
        raw_policy = freshness_policy or getattr(client, "freshness_policy", None)
        self.freshness_policy = raw_policy if isinstance(raw_policy, CacheFreshnessPolicy) else CacheFreshnessPolicy()
        raw_clock = clock or getattr(client, "clock", None)
        self.clock = raw_clock if (callable(raw_clock) and not hasattr(raw_clock, "assert_called")) else time.time
        raw_mono = monotonic or getattr(client, "monotonic", None)
        self.monotonic = raw_mono if (callable(raw_mono) and not hasattr(raw_mono, "assert_called")) else time.monotonic
        self._uses_custom_clock = self.clock is not time.time
        self.before_commit_hook = before_commit_hook

    def _now_iso(self) -> str:
        return format_utc_timestamp(self.clock()) if self._uses_custom_clock else utc_now()

    def _provider_name(self, account_id: str) -> str:
        if isinstance(self.client, PublicUsernameClient) or account_id == PUBLIC_USERNAME_SCOPE:
            return "public"
        return "account"

    def _route_url(self, account_id: str) -> str:
        if isinstance(self.client, PublicUsernameClient):
            return self.client.url
        if account_id == PUBLIC_USERNAME_SCOPE:
            return PUBLIC_CHECK_URL
        base = getattr(self.client, "base_url", "https://discord.com/api/v10")
        return f"{base}/users/@me/pomelo-attempt"

    def identify(self, cancel_event: threading.Event | None = None) -> str:
        if isinstance(self.client, PublicUsernameClient):
            return self.client.scope
        user = self.client.verify_token(cancel_event=cancel_event)
        if not user.id or user.id == "None":
            raise ValueError("Discord did not return a valid account ID.")
        return user.id

    def _supports_extended_kwargs(self) -> bool:
        if isinstance(self.client, PublicUsernameClient):
            return True
        fn = getattr(self.client, "check_username_availability", None)
        if fn is None or hasattr(fn, "assert_called"):
            return False
        try:
            sig = inspect.signature(fn)
            return "scan_id" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (ValueError, TypeError):
            return False

    def _enforce_circuit_ready(self, account_id: str, route_url: str) -> None:
        health = self.store.get_provider_health(account_id, route=route_url, now=self.clock())
        if not health.is_circuit_open:
            return
        reason = health.reason or f"Provider circuit breaker is open ({health.state.label}). Reset health before retrying."
        if health.state == ProviderHealthState.BLOCKED_CHALLENGE:
            raise UpstreamBlockedError(reason)
        if health.state == ProviderHealthState.UNINTERPRETABLE_RESPONSE:
            raise InvalidPayloadError(reason)
        raise NetworkError(reason)

    def _dispatch_client_check(
        self,
        candidate: str,
        *,
        account_id: str,
        scan_id: str,
        cancel_event: threading.Event,
        on_backoff: Callable[[float], None],
        on_network_request: Callable[[], None],
        can_dispatch_network: Callable[[], bool],
        max_wait_deadline_mono: float | Callable[[], float | None] | None,
    ) -> UsernameCheckResult:
        provider = self._provider_name(account_id)
        route_url = self._route_url(account_id)
        self._enforce_circuit_ready(account_id, route_url)
        if isinstance(self.client, PublicUsernameClient):
            return self.client.check_username_availability(
                candidate,
                cancel_event=cancel_event,
                on_backoff=on_backoff,
                scan_id=scan_id,
                use_cache=False,
                force_recheck=True,
                on_network_request=on_network_request,
                can_dispatch_network=can_dispatch_network,
                max_wait_deadline_mono=max_wait_deadline_mono,
            )

        if not can_dispatch_network():
            raise RequestCancelledError("Network request budget reached before dispatch.")
        req_start = self.clock()
        req_start_iso = self._now_iso()
        if self._supports_extended_kwargs():
            result = self.client.check_username_availability(
                candidate,
                cancel_event=cancel_event,
                on_backoff=on_backoff,
                can_dispatch_network=can_dispatch_network,
                on_network_request=on_network_request,
            )
        else:
            on_network_request()
            result = self.client.check_username_availability(
                candidate,
                cancel_event=cancel_event,
                on_backoff=on_backoff,
            )
        req_end_clock = self.clock()
        latency_ms = max(0.0, (req_end_clock - req_start) * 1000.0)
        req_end_iso = self._now_iso()
        if result.status in (UsernameStatus.AVAILABLE, UsernameStatus.UNAVAILABLE, UsernameStatus.INVALID):
            result_with_epoch = replace(result, observed_epoch=req_end_clock)
            result = self.store.save_cached_observation(
                result_with_epoch,
                provider=provider,
                scope=account_id,
                policy=self.freshness_policy,
                now=req_end_clock,
            )
            self.store.save_provider_health(
                account_id,
                ProviderHealthState.READY,
                "",
                consecutive_failures=0,
                consecutive_rate_limits=0,
                now=req_end_clock,
            )
            self.store.record_diagnostic(
                provider=provider,
                scope=account_id,
                route="/users/@me/pomelo-attempt",
                username=candidate,
                classification=result.status.value,
                origin="network",
                scan_id=scan_id,
                started_at=req_start_iso,
                completed_at=req_end_iso,
                http_status=200 if result.status != UsernameStatus.INVALID else 400,
                latency_ms=latency_ms,
            )
        return result

    def _is_deadline_expired(self, max_wait_deadline_mono: float | None) -> bool:
        if max_wait_deadline_mono is None:
            return False
        now_m = self.clock() if self._uses_custom_clock else self.monotonic()
        return now_m >= max_wait_deadline_mono

    def _check_with_cache_and_dedup(
        self,
        candidate: str,
        *,
        account_id: str,
        scan_id: str,
        cancel_event: threading.Event,
        on_backoff: Callable[[float], None],
        can_dispatch_network: Callable[[], bool],
        try_reserve_network: Callable[[], bool],
        release_reserved_network: Callable[[], None],
        commit_reserved_network: Callable[[], None],
        max_wait_deadline_mono: float | None,
        use_cache: bool,
        force_recheck: bool,
        before_commit_hook: Callable[[], None] | None = None,
    ) -> tuple[UsernameCheckResult, bool, int]:
        """Return (UsernameCheckResult, deduplicated_flag, attempts_committed_by_slot)."""
        provider = self._provider_name(account_id)
        route = PUBLIC_USERNAME_ENDPOINT if provider == "public" else "/users/@me/pomelo-attempt"
        normalized = candidate.lower()
        request_started_epoch = self.clock()

        if use_cache and not force_recheck:
            cached = self.store.get_cached_observation(
                normalized, provider=provider, scope=account_id, now=self.clock()
            )
            if cached is not None:
                now_iso = self._now_iso()
                self.store.record_diagnostic(
                    provider=provider,
                    scope=account_id,
                    route=route,
                    username=normalized,
                    classification=cached.status.value,
                    origin="cache",
                    scan_id=scan_id,
                    started_at=now_iso,
                    completed_at=now_iso,
                )
                return cached, False, 0

        if not can_dispatch_network():
            if cancel_event.is_set():
                raise RequestCancelledError("Username check cancelled before dispatch.")
            if self._is_deadline_expired(max_wait_deadline_mono):
                raise RequestCancelledError("Active duration budget reached before dispatch.")
            raise RequestCancelledError("Network request budget reached before dispatch.")

        inflight_key = (str(self.store.path), provider, account_id, normalized)

        while True:
            consumer_cancel = threading.Event()
            if cancel_event.is_set():
                consumer_cancel.set()
            slot = _WaiterSlot(
                scan_id=scan_id,
                cancel_event=consumer_cancel,
                outer_cancel_event=cancel_event,
                request_started_epoch=request_started_epoch,
                force_recheck=force_recheck,
                max_wait_deadline_mono=max_wait_deadline_mono,
                is_deadline_expired_fn=lambda: self._is_deadline_expired(max_wait_deadline_mono),
                can_dispatch_fn=can_dispatch_network,
                try_reserve_fn=try_reserve_network,
                release_reserved_fn=release_reserved_network,
                commit_reserved_fn=commit_reserved_network,
                on_backoff=on_backoff,
                before_commit_hook=before_commit_hook,
            )

            with _INFLIGHT_MUTEX:
                existing = _INFLIGHT_MAP.get(inflight_key)
                if existing is not None and not existing.done.is_set() and existing.add_waiter(slot):
                    is_leader = False
                    entry = existing
                else:
                    entry = _InFlightEntry(slot, request_started_epoch, use_cache=use_cache and not force_recheck)
                    _INFLIGHT_MAP[inflight_key] = entry
                    is_leader = True

            if is_leader:

                def _execute_leader_check(shared_entry: _InFlightEntry = entry) -> tuple[UsernameCheckResult, bool]:
                    file_lock = self.store.candidate_inflight_lock(account_id, normalized)
                    waited_for_process_lock = False
                    while True:
                        shared_entry.sync_cancel()
                        if shared_entry.shared_cancel.is_set():
                            raise RequestCancelledError("Username check cancelled.")
                        eff_deadline = shared_entry.effective_deadline_mono()
                        if eff_deadline is not None and self._is_deadline_expired(eff_deadline):
                            raise RequestCancelledError("Active duration budget reached.")
                        try:
                            with file_lock:
                                req_epoch = shared_entry.required_observed_epoch()
                                if waited_for_process_lock or (shared_entry.allow_cache and req_epoch is None):
                                    cross_proc_cached = self.store.get_cached_observation(
                                        normalized, provider=provider, scope=account_id, now=self.clock()
                                    )
                                    if cross_proc_cached is not None:
                                        obs_epoch = cross_proc_cached.effective_observed_epoch()
                                        acceptable = (req_epoch is None) or (
                                            obs_epoch is not None and obs_epoch >= req_epoch
                                        )
                                        if acceptable:
                                            return cross_proc_cached, waited_for_process_lock
                                res = self._dispatch_client_check(
                                    candidate,
                                    account_id=account_id,
                                    scan_id=shared_entry.active_scan_id(fallback=scan_id),
                                    cancel_event=shared_entry.shared_cancel,
                                    on_backoff=shared_entry.notify_backoff,
                                    on_network_request=shared_entry.reserve_and_commit_attempt,
                                    can_dispatch_network=shared_entry.can_any_active_waiter_dispatch,
                                    max_wait_deadline_mono=shared_entry.effective_deadline_mono,
                                )
                                return res, False
                        except Timeout:
                            waited_for_process_lock = True
                            shared_entry.sync_cancel()
                            if shared_entry.shared_cancel.is_set():
                                raise RequestCancelledError("Username check cancelled.") from None
                            eff_deadline = shared_entry.effective_deadline_mono()
                            if eff_deadline is not None and self._is_deadline_expired(eff_deadline):
                                raise RequestCancelledError(
                                    "Active duration budget reached while waiting for candidate lock."
                                ) from None
                            if shared_entry.shared_cancel.wait(timeout=0.05):
                                raise RequestCancelledError("Username check cancelled.") from None

                def _leader_worker(shared_entry: _InFlightEntry = entry) -> None:
                    try:
                        res, cross_dedup = _execute_leader_check(shared_entry)
                        shared_entry.cross_process_dedup = cross_dedup
                        shared_entry.result = res
                    except BaseException as exc:
                        shared_entry.error = exc
                    finally:
                        with _INFLIGHT_MUTEX:
                            if _INFLIGHT_MAP.get(inflight_key) is shared_entry:
                                _INFLIGHT_MAP.pop(inflight_key, None)
                            if shared_entry.worker_thread is not None:
                                _ACTIVE_SHARED_THREADS.discard(shared_entry.worker_thread)
                        shared_entry.done.set()

                worker_thread = threading.Thread(target=_leader_worker, daemon=True)
                entry.worker_thread = worker_thread
                with _INFLIGHT_MUTEX:
                    _ACTIVE_SHARED_THREADS.add(worker_thread)
                worker_thread.start()

            try:
                cancelled_while_waiting = False
                while not entry.done.wait(timeout=0.01):
                    if cancel_event.is_set():
                        consumer_cancel.set()
                        if entry.done.is_set() or entry.done.wait(timeout=0.01):
                            break
                        cancelled_while_waiting = True
                        entry.remove_waiter(slot)
                        if entry.shared_cancel.is_set() and entry.worker_thread is not None:
                            entry.worker_thread.join(timeout=0.5)
                        raise RequestCancelledError("Username check cancelled.")
                    if self._is_deadline_expired(max_wait_deadline_mono):
                        consumer_cancel.set()
                        entry.remove_waiter(slot)
                        if entry.shared_cancel.is_set() and entry.worker_thread is not None:
                            entry.worker_thread.join(timeout=0.5)
                        raise RequestCancelledError("Active duration budget reached.")

                if entry.worker_thread is not None and entry.shared_cancel.is_set():
                    entry.worker_thread.join(timeout=0.5)

                this_slot_dispatched = (
                    slot.attempts_committed > 0
                    or (entry.total_attempts_committed == 0 and is_leader and not entry.cross_process_dedup)
                )
                if cancelled_while_waiting or (cancel_event.is_set() and not this_slot_dispatched):
                    consumer_cancel.set()
                    raise RequestCancelledError("Username check cancelled.")
                if self._is_deadline_expired(max_wait_deadline_mono) and slot.attempts_committed == 0:
                    consumer_cancel.set()
                    raise RequestCancelledError("Active duration budget reached.")
                if entry.error is not None:
                    if (
                        isinstance(entry.error, RequestCancelledError)
                        and self._is_deadline_expired(max_wait_deadline_mono)
                        and not cancel_event.is_set()
                    ):
                        raise RequestCancelledError("Active duration budget reached.") from entry.error
                    raise entry.error
                assert entry.result is not None
                res = entry.result
            finally:
                consumer_cancel.set()
                entry.remove_waiter(slot)

            if force_recheck:
                obs_epoch = res.effective_observed_epoch()
                if obs_epoch is None or obs_epoch < request_started_epoch:
                    # Joined an in-flight entry whose observation predated our recheck request; run a fresh check.
                    if not can_dispatch_network():
                        if cancel_event.is_set():
                            raise RequestCancelledError("Username check cancelled.")
                        if self._is_deadline_expired(max_wait_deadline_mono):
                            raise RequestCancelledError("Active duration budget reached.")
                        raise RequestCancelledError("Network request budget reached before dispatch.")
                    continue

            if entry.cross_process_dedup:
                is_dedup = True
            elif entry.total_attempts_committed > 0:
                is_dedup = entry.last_committer_slot is not slot
            else:
                is_dedup = not is_leader

            if is_dedup:
                now_iso = self._now_iso()
                dedup_res = UsernameCheckResult(
                    username=res.username,
                    status=res.status,
                    checked_at=res.checked_at,
                    message=res.message,
                    origin=ObservationOrigin.CACHE.value,
                    provider=res.provider or provider,
                    expires_at=res.expires_at,
                    reused_at=now_iso,
                    observed_epoch=res.observed_epoch,
                    expires_epoch=res.expires_epoch,
                    _clock=self.clock,
                )
                self.store.record_diagnostic(
                    provider=provider,
                    scope=account_id,
                    route=route,
                    username=normalized,
                    classification="deduplicated",
                    origin="dedup",
                    scan_id=scan_id,
                    started_at=now_iso,
                    completed_at=now_iso,
                )
                return dedup_res, True, slot.attempts_committed

            return res, False, slot.attempts_committed

    def run(
        self,
        scan_id: str,
        account_id: str,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[UsernameProgress], None] | None = None,
        max_checks: int | None = None,
        *,
        budget: RequestBudget | None = None,
        use_cache: bool | None = None,
        force_recheck: bool = False,
        before_commit_hook: Callable[[], None] | None = None,
    ) -> UsernameScan:
        if max_checks is not None and max_checks < 1:
            raise ValueError("The check limit must be positive.")
        if budget is not None:
            budget.validate()
        try:
            with self.store.lock(scan_id):
                return self._run_locked(
                    scan_id,
                    account_id,
                    cancel_event or threading.Event(),
                    on_progress,
                    max_checks,
                    budget=budget,
                    use_cache=use_cache,
                    force_recheck=force_recheck,
                    before_commit_hook=before_commit_hook or self.before_commit_hook,
                )
        except Timeout as exc:
            raise ScanBusyError("This saved scan is already running in another window or process.") from exc

    def _run_locked(
        self,
        scan_id: str,
        account_id: str,
        cancel_event: threading.Event,
        on_progress: Callable[[UsernameProgress], None] | None,
        max_checks: int | None,
        *,
        budget: RequestBudget | None = None,
        use_cache: bool | None = None,
        force_recheck: bool = False,
        before_commit_hook: Callable[[], None] | None = None,
    ) -> UsernameScan:
        scan = self.store.get_scan(scan_id, account_id)
        if scan.generator_version not in SUPPORTED_GENERATOR_VERSIONS:
            raise ValueError("This scan uses a different generator version. Start a new scan.")
        if scan.status == "completed" and not force_recheck:
            return scan

        provider = self._provider_name(account_id)
        route_url = self._route_url(account_id)
        effective_use_cache = (
            self.freshness_policy.use_cache if use_cache is None else bool(use_cache)
        ) and not force_recheck
        preferences = self.store.scan_preferences(scan_id, account_id) if scan.generator_version >= 2 else None
        names = self.store.custom_names(scan_id, account_id) if scan.mode == "custom" else []
        scan = self.store.set_status(scan_id, account_id, "running", pause_reason="")

        processed = 0
        definitive_this_run = 0
        network_requests_this_run = 0
        reserved_network_requests = 0
        run_active = True
        candidate = ""
        pause_reason = "Paused by user"

        start_mono = self.clock() if self._uses_custom_clock else self.monotonic()
        max_wait_deadline_mono = (
            (start_mono + budget.max_duration_seconds)
            if (budget is not None and budget.max_duration_seconds is not None)
            else None
        )
        max_consec_fail = (
            budget.max_consecutive_failures
            if (budget is not None and budget.max_consecutive_failures is not None)
            else 3
        )

        def can_dispatch_network() -> bool:
            if not run_active or cancel_event.is_set():
                return False
            if self._is_deadline_expired(max_wait_deadline_mono):
                return False
            if budget is not None and budget.max_network_requests is not None:
                return (network_requests_this_run + reserved_network_requests) < budget.max_network_requests
            return True

        def try_reserve_network() -> bool:
            nonlocal reserved_network_requests
            if not can_dispatch_network():
                return False
            reserved_network_requests += 1
            return True

        def release_reserved_network() -> None:
            nonlocal reserved_network_requests
            reserved_network_requests = max(0, reserved_network_requests - 1)

        def commit_reserved_network() -> None:
            nonlocal reserved_network_requests, network_requests_this_run
            reserved_network_requests = max(0, reserved_network_requests - 1)
            network_requests_this_run += 1
            self.store.increment_scan_network_checks(scan_id, 1)

        def notify(message: str = "", result: UsernameCheckResult | None = None) -> None:
            if on_progress is not None:
                try:
                    health = self.store.get_provider_health(account_id, route=route_url, now=self.clock())
                    diag = self.store.diagnostics_summary(
                        account_id,
                        remaining_candidates=max(0, scan.total - scan.checked),
                        route=route_url,
                        now=self.clock(),
                    )
                    on_progress(UsernameProgress(scan, candidate, result, message, health=health, diagnostics=diag))
                except Exception:
                    # A presentation failure must not overwrite a committed availability result.
                    logger.warning("Username progress callback failed; saved scan state is intact.")

        try:
            while not cancel_event.is_set():
                if scan.checked >= scan.total:
                    return self.store.set_status(scan_id, account_id, "completed", pause_reason="")
                if max_checks is not None and processed >= max_checks:
                    pause_reason = f"Check limit reached ({processed}/{max_checks})"
                    break
                if (
                    budget is not None
                    and budget.max_definitive_checks is not None
                    and definitive_this_run >= budget.max_definitive_checks
                ):
                    pause_reason = (
                        f"Definitive check budget reached ({definitive_this_run}/{budget.max_definitive_checks})"
                    )
                    break
                if self._is_deadline_expired(max_wait_deadline_mono):
                    pause_reason = f"Active duration budget reached ({budget.max_duration_seconds:g}s)"  # type: ignore[union-attr]
                    break

                if scan.mode == "generated":
                    try:
                        next_item = next_generated_candidate(
                            scan.lengths,
                            scan.cursor,
                            preferences,
                            cancel_event=cancel_event,
                            max_deadline_mono=max_wait_deadline_mono,
                            monotonic=(self.clock if self._uses_custom_clock else self.monotonic),
                        )
                    except RequestCancelledError as gen_cancel:
                        msg = str(gen_cancel)
                        if "budget reached" in msg.lower():
                            pause_reason = msg
                            break
                        raise
                else:
                    next_item = (names[scan.cursor], scan.cursor + 1) if scan.cursor < len(names) else None
                if next_item is None:
                    raise ValueError("Saved scan cursor is inconsistent. Start a new scan.")
                candidate, next_cursor = next_item
                notify("Checking")
                validation = validate_username(candidate)
                deduplicated = False
                attempts_committed = 0
                if validation:
                    now_iso = self._now_iso()
                    result = UsernameCheckResult(
                        candidate,
                        UsernameStatus.INVALID,
                        now_iso,
                        validation,
                        origin=ObservationOrigin.LOCAL.value,
                        provider=provider,
                        _clock=self.clock,
                    )
                    self.store.record_diagnostic(
                        provider=provider,
                        scope=account_id,
                        route=PUBLIC_USERNAME_ENDPOINT if provider == "public" else "/users/@me/pomelo-attempt",
                        username=candidate,
                        classification="invalid",
                        origin="local",
                        scan_id=scan_id,
                        started_at=now_iso,
                        completed_at=now_iso,
                    )
                else:
                    try:
                        result, deduplicated, attempts_committed = self._check_with_cache_and_dedup(
                            candidate,
                            account_id=account_id,
                            scan_id=scan_id,
                            cancel_event=cancel_event,
                            on_backoff=lambda seconds: notify(
                                f"Rate limited; waiting {math.ceil(seconds):,} seconds. Progress is saved."
                            ),
                            can_dispatch_network=can_dispatch_network,
                            try_reserve_network=try_reserve_network,
                            release_reserved_network=release_reserved_network,
                            commit_reserved_network=commit_reserved_network,
                            max_wait_deadline_mono=max_wait_deadline_mono,
                            use_cache=effective_use_cache,
                            force_recheck=force_recheck,
                            before_commit_hook=before_commit_hook,
                        )
                    except RequestCancelledError as cancel_exc:
                        msg = str(cancel_exc)
                        if "budget reached" in msg.lower():
                            if (
                                "network request budget" in msg.lower()
                                and budget is not None
                                and budget.max_network_requests is not None
                            ):
                                pause_reason = (
                                    f"Network request budget reached "
                                    f"({network_requests_this_run}/{budget.max_network_requests})"
                                )
                            elif (
                                "duration budget" in msg.lower()
                                and budget is not None
                                and budget.max_duration_seconds is not None
                            ):
                                pause_reason = (
                                    f"Active duration budget reached ({budget.max_duration_seconds:g}s)"
                                )
                            else:
                                pause_reason = msg
                            break
                        raise

                if result.username != candidate or result.status == UsernameStatus.ERROR:
                    raise ValueError("Username check did not return a definitive result for this candidate.")
                scan = self.store.save_result(
                    scan_id,
                    account_id,
                    result,
                    next_cursor,
                    deduplicated=deduplicated,
                    network_attempts_already_recorded=(attempts_committed > 0 or deduplicated),
                )
                processed += 1
                if result.is_definitive:
                    definitive_this_run += 1
                notify(result=result)
            status = "completed" if scan.checked >= scan.total else "paused"
            return self.store.set_status(
                scan_id,
                account_id,
                status,
                pause_reason="" if status == "completed" else pause_reason,
            )
        except (RequestCancelledError, KeyboardInterrupt):
            return self.store.set_status(scan_id, account_id, "paused", pause_reason=pause_reason)
        except Exception as exc:
            message = sanitize_token(str(exc))
            if self.client.token:
                message = message.replace(self.client.token, "[REDACTED_TOKEN]")
            existing_health = self.store.get_provider_health(account_id, route=route_url, now=self.clock())
            if isinstance(self.client, PublicUsernameClient) or existing_health.is_circuit_open:
                consec_fail = max(1, existing_health.consecutive_failures)
                consec_rl = existing_health.consecutive_rate_limits
            else:
                consec_fail = existing_health.consecutive_failures + 1
                consec_rl = existing_health.consecutive_rate_limits

            if isinstance(exc, UpstreamBlockedError):
                health_state = ProviderHealthState.BLOCKED_CHALLENGE
            elif isinstance(exc, InvalidPayloadError):
                health_state = (
                    ProviderHealthState.PAUSED_FAILURES
                    if consec_fail >= max_consec_fail
                    else ProviderHealthState.UNINTERPRETABLE_RESPONSE
                )
            elif isinstance(exc, (NetworkError, RequestTimeoutError)):
                if isinstance(self.client, PublicUsernameClient) or consec_fail >= max_consec_fail:
                    health_state = ProviderHealthState.PAUSED_FAILURES
                else:
                    health_state = ProviderHealthState.BACKING_OFF
            else:
                health_state = ProviderHealthState.PAUSED_FAILURES
            self.store.save_provider_health(
                account_id,
                health_state,
                message,
                consecutive_failures=consec_fail,
                consecutive_rate_limits=consec_rl,
                retry_at=existing_health.retry_at,
                now=self.clock(),
            )
            if candidate:
                self.store.save_result(
                    scan_id,
                    account_id,
                    UsernameCheckResult(
                        candidate,
                        UsernameStatus.ERROR,
                        self._now_iso(),
                        message,
                        origin=ObservationOrigin.NETWORK.value,
                        provider=provider,
                        _clock=self.clock,
                    ),
                    None,
                    network_attempts_already_recorded=True,
                )
            return self.store.set_status(
                scan_id,
                account_id,
                "failed",
                message,
                pause_reason=f"Circuit breaker ({health_state.label}): {message}",
            )
        finally:
            run_active = False

    def recheck_names(
        self,
        account_id: str,
        names: list[str],
        *,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[UsernameProgress], None] | None = None,
        budget: RequestBudget | None = None,
    ) -> UsernameScan:
        """Create and run an explicit fresh network recheck scan for selected names."""
        scan = self.store.create_scan(account_id, mode="custom", names=names)
        return self.run(
            scan.id,
            account_id,
            cancel_event=cancel_event,
            on_progress=on_progress,
            budget=budget,
            use_cache=False,
            force_recheck=True,
        )
