from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import pyqtSignal

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CandidatePreferences,
    RequestBudget,
    UsernameProgress,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.transport.http_transport import RequestsTransport
from discord_cleanup.usernames.candidates import parse_custom_names
from discord_cleanup.usernames.public_client import PUBLIC_CHECK_URL, PublicUsernameClient
from discord_cleanup.usernames.scanner import UsernameScanner
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.base import CancellableTokenWorker


class UsernameScanWorker(CancellableTokenWorker):
    account_ready = pyqtSignal(str, list)
    progress_signal = pyqtSignal(object)
    completed_signal = pyqtSignal(object)
    error_signal = pyqtSignal(str)

    def __init__(
        self,
        token: str,
        mode: str = "list",
        lengths: tuple[int, ...] = (3, 4),
        custom_text: str = "",
        scan_id: str | None = None,
        store_path: str | Path | None = None,
        client: DiscordApiClient | PublicUsernameClient | None = None,
        access: str = "account",
        *,
        preferences: CandidatePreferences | None = None,
        budget: RequestBudget | None = None,
        use_cache: bool = True,
        force_recheck: bool = False,
        recheck_names: list[str] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        super().__init__(token)
        self.mode = mode
        self.lengths = lengths
        self.custom_text = custom_text
        self.scan_id = scan_id
        self.store_path = store_path
        self.client = client
        self.access = access
        self.preferences = preferences
        self.budget = budget
        self.use_cache = use_cache
        self.force_recheck = force_recheck
        self.recheck_names = list(recheck_names) if recheck_names else None
        self.clock = clock

    def run(self) -> None:
        owned_transport = None
        client = self.client
