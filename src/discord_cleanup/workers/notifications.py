from __future__ import annotations

import time
from typing import Any
from PyQt5.QtCore import pyqtSignal

from discord_cleanup.api.client import DEFAULT_API_CLIENT, DiscordApiClient
from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.gateway.notifications import DEFAULT_GATEWAY_READER, GatewayNotificationsReader
from discord_cleanup.workers.base import CancellableTokenWorker


class ReadNotifsWorker(CancellableTokenWorker):
    """Retrieves unread channels from Gateway and acknowledges notifications in chunks."""

    progress_signal = pyqtSignal(str)
    chunk_progress_signal = pyqtSignal(int, int)  # (current_chunk, total_chunks)
    finished_signal = pyqtSignal(int, int, str)  # (success_count, fail_count, error_msg)

    def __init__(
        self,
        token: str,
        client: DiscordApiClient | None = None,
        gateway: GatewayNotificationsReader | None = None,
    ):
        super().__init__(token)
        self.client = client or DEFAULT_API_CLIENT
        self.gateway = gateway or DEFAULT_GATEWAY_READER

    def run(self) -> None:
        try:
            grouped = self.gateway.fetch_unread_channels(self.token, cancel_event=self._cancel_event)
            if not grouped:
                self.finished_signal.emit(0, 0, "No unread channels found.")
                return

            total_unread = sum(len(c) for c in grouped.values())
            if total_unread == 0:
                self.finished_signal.emit(0, 0, "")
                return

            self.progress_signal.emit(f"[*] Found {total_unread} unread channels across {len(grouped)} servers/DMs.")

            current_time_ms = int(time.time() * 1000)
            future_ms = current_time_ms + 3600000
            massive_message_id = str((future_ms - 1420070400000) << 22)

            all_chunks: list[tuple[str, list[dict[str, Any]]]] = []
            chunk_size = 100
            for server_name, channel_ids in grouped.items():
                if not channel_ids:
                    continue
                payload = [
                    {"channel_id": c, "message_id": massive_message_id, "read_state_type": 0}
                    for c in channel_ids
                ]
                chunks = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]
                for chunk in chunks:
                    all_chunks.append((server_name, chunk))

            total_chunks = len(all_chunks)
            success_count = 0
            fail_count = 0

            for idx, (server_name, chunk) in enumerate(all_chunks, 1):
                if self.is_cancelled():
                    self.finished_signal.emit(success_count, fail_count, "Cancelled")
                    return

                self.progress_signal.emit(f"[*] Marking {server_name} as read... ({idx}/{total_chunks})")
                self.chunk_progress_signal.emit(idx, total_chunks)

                try:
                    status, err = self.client.ack_read_states_chunk(chunk, cancel_event=self._cancel_event)
                    if status in (200, 204):
                        success_count += len(chunk)
                    else:
                        fail_count += len(chunk)
                        self.progress_signal.emit(f"[-] Chunk failed for {server_name}: {err}")
                except RequestCancelledError:
                    self.finished_signal.emit(success_count, fail_count, "Cancelled")
                    return
                except Exception as exc:
                    fail_count += len(chunk)
                    self.progress_signal.emit(f"[-] Chunk failed for {server_name}: {exc}")
                    if "IP Ban" in str(exc) or "Cloudflare" in str(exc):
                        self.finished_signal.emit(success_count, fail_count, "Aborted due to Upstream IP Ban")
                        return

            self.finished_signal.emit(success_count, fail_count, "")
        except Exception as exc:
            self.finished_signal.emit(0, 0, str(exc))
        finally:
            self.scrub_token()
