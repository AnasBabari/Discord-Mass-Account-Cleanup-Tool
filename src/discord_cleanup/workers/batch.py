from __future__ import annotations

from typing import Any
from PyQt5.QtCore import pyqtSignal

from discord_cleanup.api.client import DEFAULT_API_CLIENT, DiscordApiClient
from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.workers.base import CancellableTokenWorker


class BatchActionWorker(CancellableTokenWorker):
    """Generic runner for batch actions across items (friends, servers, blocks)."""

    progress_signal = pyqtSignal(int, str)  # (current_count, log_message)
    finished_signal = pyqtSignal(int, int)  # (success_count, failed_count)

    def __init__(
        self,
        token: str,
        items: list[dict[str, Any]],
        action_name: str = "PROCESSED",
        client: DiscordApiClient | None = None,
    ):
        super().__init__(token)
        self.items = items
        self.action_name = action_name
        self.client = client or DEFAULT_API_CLIENT

    def get_item_id(self, item: dict[str, Any]) -> str:
        return str(item.get("id", ""))

    def get_item_display(self, item: dict[str, Any]) -> str:
        user = item.get("user")
        if isinstance(user, dict):
            return user.get("global_name") or user.get("username") or "Unknown"
        return str(item.get("name") or item.get("id", "Unknown"))

    def execute_action(self, token: str, item_id: str, cancel_event: Any) -> tuple[int, str]:
        raise NotImplementedError

    def run(self) -> None:
        success = 0
        failed = 0
        for idx, item in enumerate(self.items, 1):
            if self.is_cancelled():
                break
            display = self.get_item_display(item)
            item_id = self.get_item_id(item)
            try:
                status, text = self.execute_action(self.token, item_id, cancel_event=self._cancel_event)
                if status in (200, 204):
                    success += 1
                    self.progress_signal.emit(idx, f"[+] {self.action_name}: {display}")
                elif status == 401:
                    failed += 1
                    self.progress_signal.emit(idx, f"[-] FAILED: {display} (401 Unauthorized - Invalid Token)")
                    break
                else:
                    failed += 1
                    self.progress_signal.emit(idx, f"[-] FAILED: {display} ({text})")
            except RequestCancelledError:
                break
            except Exception as exc:
                failed += 1
                self.progress_signal.emit(idx, f"[-] FAILED: {display} ({exc})")
                if "IP Ban" in str(exc) or "Cloudflare" in str(exc):
                    break

        self.finished_signal.emit(success, failed)
        self.scrub_token()


class RemoveFriendsWorker(BatchActionWorker):
    def __init__(self, token: str, friends_to_remove: list[dict[str, Any]], client: DiscordApiClient | None = None):
        super().__init__(token, friends_to_remove, action_name="REMOVED", client=client)

    def execute_action(self, token: str, item_id: str, cancel_event: Any) -> tuple[int, str]:
        return self.client.remove_friend(item_id, cancel_event=cancel_event)


class BlockUsersWorker(BatchActionWorker):
    def __init__(self, token: str, users_to_block: list[dict[str, Any]], client: DiscordApiClient | None = None):
        super().__init__(token, users_to_block, action_name="BLOCKED", client=client)

    def execute_action(self, token: str, item_id: str, cancel_event: Any) -> tuple[int, str]:
        return self.client.block_user(item_id, cancel_event=cancel_event)


class UnblockUsersWorker(BatchActionWorker):
    def __init__(self, token: str, users_to_unblock: list[dict[str, Any]], client: DiscordApiClient | None = None):
        super().__init__(token, users_to_unblock, action_name="UNBLOCKED", client=client)

    def execute_action(self, token: str, item_id: str, cancel_event: Any) -> tuple[int, str]:
        return self.client.unblock_user(item_id, cancel_event=cancel_event)


class LeaveServersWorker(BatchActionWorker):
    def __init__(self, token: str, servers_to_leave: list[dict[str, Any]], client: DiscordApiClient | None = None):
        super().__init__(token, servers_to_leave, action_name="LEFT", client=client)

    def execute_action(self, token: str, item_id: str, cancel_event: Any) -> tuple[int, str]:
        return self.client.leave_guild(item_id, cancel_event=cancel_event)
