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
        try:
            if self.is_cancelled() or (self.access == "account" and not self.token):
                return
            if self.access not in ("public", "account"):
                raise ValueError("Unknown username-check source.")
            effective_clock = self.clock or time.time
            store = UsernameScanStore(self.store_path, clock=effective_clock)
            if client is None:
                owned_transport = RequestsTransport()
                client = (
                    PublicUsernameClient(store, transport=owned_transport, clock=effective_clock)
                    if self.access == "public"
                    else DiscordApiClient(self.token, transport=owned_transport)
                )
            scanner = UsernameScanner(store, client, clock=self.clock)
            account_id = PUBLIC_USERNAME_SCOPE if self.access == "public" else scanner.identify(self._cancel_event)
            if self.is_cancelled():
                return
            store.recover_interrupted(account_id)
            self.account_ready.emit(account_id, store.list_scans(account_id))
            if self.mode == "list":
                return

            force_recheck = self.force_recheck or self.mode == "recheck"
            use_cache = self.use_cache and not force_recheck

            if self.scan_id is not None and self.mode == "resume":
                scan = store.get_scan(self.scan_id, account_id)
            elif self.mode == "recheck":
                names = self.recheck_names or parse_custom_names(self.custom_text)
                scan = store.create_scan(account_id, "custom", self.lengths, names)
            elif self.mode == "watchlist":
                names = store.watchlist_candidates(
                    account_id,
                    provider="public" if self.access == "public" else "account",
                    stale_only=not force_recheck,
                    now=effective_clock(),
                )
                if not names:
                    names = [str(item["username"]) for item in store.list_watchlist(account_id)]
                scan = store.create_scan(account_id, "custom", self.lengths, names)
            else:
                scan = store.create_scan(
                    account_id,
                    self.mode,
                    self.lengths,
                    parse_custom_names(self.custom_text),
                    preferences=self.preferences,
                )
            self.custom_text = ""
            route = PUBLIC_CHECK_URL if self.access == "public" else "/users/@me/pomelo-attempt"
            health = store.get_provider_health(account_id, route=route, now=effective_clock())
            diag = store.diagnostics_summary(
                account_id,
                remaining_candidates=max(0, scan.total - scan.checked),
                route=route,
                now=effective_clock(),
            )
            self.progress_signal.emit(UsernameProgress(scan, health=health, diagnostics=diag))
        except Exception as exc:
            pass
        finally:
            pass
