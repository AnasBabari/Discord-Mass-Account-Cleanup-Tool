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
