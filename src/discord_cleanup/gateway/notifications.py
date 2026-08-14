from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
import websocket

from discord_cleanup.api.exceptions import RequestCancelledError

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
WS_READY_TIMEOUT = 20.0


class GatewayNotificationsReader:
    """Connects to Discord Gateway to retrieve unread channels and notification state."""

    def __init__(self, gateway_url: str = GATEWAY_URL, timeout: float = WS_READY_TIMEOUT):
        self.gateway_url = gateway_url
        self.timeout = timeout

    def fetch_unread_channels(
        self, token: str, cancel_event: threading.Event | None = None
    ) -> dict[str, list[str]]:
        """Connect to Gateway, receive READY payload, parse unread states, and return grouped channels.

        WARNING: This is a blocking network call. Run inside a background thread.
        """
        if not token:
            return {}

        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Gateway connection cancelled by user.")

        grouped_channels: dict[str, list[str]] = {}
        has_received_ready = False
        ws_error: Exception | None = None

        def on_message(ws: websocket.WebSocketApp, message: str) -> None:
            nonlocal has_received_ready
            try:
                data = json.loads(message)
            except json.JSONDecodeError as exc:
                logger.warning("Gateway JSON decode error: %s", exc)
                return

            if not isinstance(data, dict):
                return

            op = data.get("op")
            event_type = data.get("t")

            if op == 9:
                logger.warning("Gateway invalid session (op 9)")
                ws.close()
                return

            if event_type == "READY":
                has_received_ready = True
                payload = data.get("d")
                if isinstance(payload, dict):
                    self._parse_ready_payload(payload, grouped_channels)
                ws.close()

        def on_open(ws: websocket.WebSocketApp) -> None:
            identify_payload = {
                "op": 2,
                "d": {
                    "token": token,
                    "capabilities": 16381,
                    "properties": {
                        "os": "Windows",
                        "browser": "Chrome",
                        "device": "",
                    },
                    "presence": {
                        "status": "unknown",
                        "since": 0,
                        "activities": [],
                        "afk": False,
                    },
                    "compress": False,
                    "client_state": {"guild_versions": {}},
                },
            }
            try:
                ws.send(json.dumps(identify_payload))
            except Exception as exc:
                logger.warning("Failed to send Gateway identify: %s", exc)
                ws.close()

        def on_error(ws: websocket.WebSocketApp, error: Any) -> None:
            nonlocal ws_error
            logger.debug("Gateway WebSocket error: %s", error)
            if isinstance(error, Exception):
                ws_error = error
            try:
                ws.close()
            except Exception:
                pass

        ws = websocket.WebSocketApp(
            self.gateway_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
        )

        wst = threading.Thread(target=ws.run_forever, daemon=True)
        wst.start()

        deadline = time.monotonic() + self.timeout
        while wst.is_alive() and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                ws.keep_running = False
                try:
                    sock = getattr(ws, "sock", None)
                    if sock is not None:
                        sock.close()
                except Exception:
                    pass
                ws.close()
                wst.join(timeout=1.0)
                raise RequestCancelledError("Gateway connection cancelled by user.")
            wst.join(timeout=0.1)

        if wst.is_alive():
            logger.warning("Gateway connection timed out waiting for READY event.")
            ws.keep_running = False
            try:
                sock = getattr(ws, "sock", None)
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            ws.close()
            wst.join(timeout=1.0)

        if ws_error and not has_received_ready:
            raise ws_error

        return grouped_channels

    def _parse_ready_payload(self, data: dict[str, Any], output_map: dict[str, list[str]]) -> None:
        read_map: dict[str, dict[str, Any]] = {}
        read_state = data.get("read_state")
        if isinstance(read_state, dict) and "entries" in read_state:
            for entry in read_state["entries"]:
                if isinstance(entry, dict) and entry.get("id"):
                    read_map[str(entry["id"])] = {
                        "last_message_id": entry.get("last_message_id"),
                        "mention_count": entry.get("mention_count", 0),
                    }

        user_guild_settings = data.get("user_guild_settings", [])
        muted_guilds: set[str] = set()
        muted_channels: set[str] = set()
        unmuted_channels: set[str] = set()

        if isinstance(user_guild_settings, list):
            for ugs in user_guild_settings:
                if not isinstance(ugs, dict):
                    continue
                if ugs.get("muted"):
                    g_id = ugs.get("guild_id")
                    if g_id:
                        muted_guilds.add(str(g_id))

                overrides = ugs.get("channel_overrides", [])
                if isinstance(overrides, list):
                    for override in overrides:
                        if not isinstance(override, dict):
                            continue
                        c_id = override.get("channel_id")
                        if not c_id:
                            continue
                        if override.get("muted"):
                            muted_channels.add(str(c_id))
                        elif "muted" in override and not override["muted"]:
                            unmuted_channels.add(str(c_id))

        def is_unread(channel_id: str, channel_last_msg_id: str | None, guild_id: str | None = None) -> bool:
            if not channel_last_msg_id:
                return False

            state = read_map.get(channel_id, {})
            mention_count = state.get("mention_count", 0)

            if channel_id in unmuted_channels:
                pass
            elif channel_id in muted_channels:
                return False
            elif guild_id and guild_id in muted_guilds:
                return False

            read_last_id = state.get("last_message_id")
            if not read_last_id:
                return True
            try:
                return mention_count > 0 or int(channel_last_msg_id) > int(read_last_id)
            except (ValueError, TypeError):
                return str(channel_last_msg_id) != str(read_last_id)

        # 1. Guilds
        guilds = data.get("guilds", [])
        if isinstance(guilds, list):
            for guild in guilds:
                if isinstance(guild, dict):
                    server_name = guild.get("properties", {}).get("name") or guild.get("name") or "Unknown Server"
                    g_id = str(guild.get("id", ""))
                    unread_ids: list[str] = []

                    channels = guild.get("channels", [])
                    if isinstance(channels, list):
                        for channel in channels:
                            if isinstance(channel, dict) and channel.get("id"):
                                c_id = str(channel["id"])
                                c_last_id = channel.get("last_message_id")
                                if is_unread(c_id, c_last_id, g_id):
                                    unread_ids.append(c_id)

                    threads = guild.get("threads", [])
                    if isinstance(threads, list):
                        for thread in threads:
                            if isinstance(thread, dict) and thread.get("id"):
                                t_id = str(thread["id"])
                                t_last_id = thread.get("last_message_id")
                                if is_unread(t_id, t_last_id, g_id):
                                    unread_ids.append(t_id)

                    if unread_ids:
                        output_map[server_name] = unread_ids

        # 2. Private channels (DMs)
        private_channels = data.get("private_channels", [])
        if isinstance(private_channels, list):
            unread_dms: list[str] = []
            for dm in private_channels:
                if isinstance(dm, dict) and dm.get("id"):
                    c_id = str(dm["id"])
                    c_last_id = dm.get("last_message_id")
                    if is_unread(c_id, c_last_id):
                        unread_dms.append(c_id)
            if unread_dms:
                output_map["Direct Messages"] = unread_dms


DEFAULT_GATEWAY_READER = GatewayNotificationsReader()
