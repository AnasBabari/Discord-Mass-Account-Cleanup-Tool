from __future__ import annotations

import logging

import requests
from PyQt5.QtCore import pyqtSignal

from discord_cleanup.api.client import DEFAULT_API_CLIENT, DiscordApiClient
from discord_cleanup.api.exceptions import AuthenticationError
from discord_cleanup.workers.base import CancellableTokenWorker

logger = logging.getLogger(__name__)


class LoginWorker(CancellableTokenWorker):
    """Verifies token, fetches profile metadata and avatar bytes in a background QThread."""

    # (success, message, username, token, avatar_bytes, save)
    result_signal = pyqtSignal(bool, str, str, str, bytes, bool)

    def __init__(self, token: str, save: bool = True, client: DiscordApiClient | None = None):
        super().__init__(token)
        self.save = save
        self.client = client or DEFAULT_API_CLIENT

    def run(self) -> None:
        token = self.token
        if not token or self.is_cancelled():
            self.result_signal.emit(False, "No token provided", "", "", b"", self.save)
            return

        try:
            user = self.client.verify_token(token, cancel_event=self._cancel_event)
            avatar_bytes = b""
            if user.id:
                if user.avatar:
                    avatar_url = f"https://cdn.discordapp.com/avatars/{user.id}/{user.avatar}.png?size=64"
                else:
                    try:
                        index = (int(user.id) >> 22) % 6
                    except Exception:
                        index = 0
                    avatar_url = f"https://cdn.discordapp.com/embed/avatars/{index}.png"

                try:
                    r = requests.get(avatar_url, timeout=10.0)
                    if r.status_code == 200:
                        avatar_bytes = r.content
                except Exception as exc:
                    logger.debug("Failed to download avatar image: %s", exc)

            if self.is_cancelled():
                return

            self.result_signal.emit(True, user.display_name, user.username, token, avatar_bytes, self.save)
        except AuthenticationError:
            self.result_signal.emit(False, "INVALID TOKEN", "", "", b"", self.save)
        except Exception as exc:
            if not self.is_cancelled():
                self.result_signal.emit(False, f"NETWORK ERROR: {exc}", "", "", b"", self.save)
        finally:
            self.scrub_token()
