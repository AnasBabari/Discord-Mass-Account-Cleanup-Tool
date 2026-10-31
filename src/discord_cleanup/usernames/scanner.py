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
