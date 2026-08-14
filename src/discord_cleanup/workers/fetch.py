from __future__ import annotations

from typing import Any
from PyQt5.QtCore import pyqtSignal

from discord_cleanup.api.client import DEFAULT_API_CLIENT, DiscordApiClient
from discord_cleanup.workers.base import CancellableTokenWorker


class FetchServersWorker(CancellableTokenWorker):
    """Fetches user guilds in a background QThread."""

    result_signal = pyqtSignal(list, str)  # (guilds_list, error_str)

    def __init__(self, token: str, client: DiscordApiClient | None = None):
        super().__init__(token)
        self.client = client or DEFAULT_API_CLIENT

    def run(self) -> None:
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            guilds = self.client.get_guilds(cancel_event=self._cancel_event)
            # Support backwards compatibility: convert Guild models to dict representation for tables
            guilds_data = [
                {"id": g.id, "name": g.name, "owner": g.owner, "permissions": g.permissions, "icon": g.icon}
                for g in guilds
            ]
            self.result_signal.emit(guilds_data, "")
        except Exception as exc:
            self.result_signal.emit([], str(exc))
        finally:
            self.scrub_token()


class FetchFriendsWorker(CancellableTokenWorker):
    """Fetches user friends in a background QThread."""

    result_signal = pyqtSignal(list, str)  # (friends_list, error_str)

    def __init__(self, token: str, client: DiscordApiClient | None = None):
        super().__init__(token)
        self.client = client or DEFAULT_API_CLIENT

    def run(self) -> None:
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            friends = self.client.get_friends(cancel_event=self._cancel_event)
            friends_data: list[dict[str, Any]] = [
                {
                    "id": rel.id,
                    "user": {
                        "id": rel.user.id,
                        "username": rel.user.username,
                        "global_name": rel.user.global_name,
                        "discriminator": rel.user.discriminator,
                    },
                    "nickname": rel.nickname,
                    "since": rel.since,
                }
                for rel in friends
            ]
            self.result_signal.emit(friends_data, "")
        except Exception as exc:
            self.result_signal.emit([], str(exc))
        finally:
            self.scrub_token()


class FetchBlockedWorker(CancellableTokenWorker):
    """Fetches blocked users in a background QThread."""

    result_signal = pyqtSignal(list, str)  # (blocked_list, error_str)

    def __init__(self, token: str, client: DiscordApiClient | None = None):
        super().__init__(token)
        self.client = client or DEFAULT_API_CLIENT

    def run(self) -> None:
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            blocked = self.client.get_blocked_users(cancel_event=self._cancel_event)
            blocked_data: list[dict[str, Any]] = [
                {
                    "id": rel.id,
                    "user": {
                        "id": rel.user.id,
                        "username": rel.user.username,
                        "global_name": rel.user.global_name,
                        "discriminator": rel.user.discriminator,
                    },
                }
                for rel in blocked
            ]
            self.result_signal.emit(blocked_data, "")
        except Exception as exc:
            self.result_signal.emit([], str(exc))
        finally:
            self.scrub_token()
