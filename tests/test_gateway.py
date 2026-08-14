import threading
from unittest.mock import patch
import pytest

from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.gateway.notifications import GatewayNotificationsReader


class TestGatewayNotificationsReader:
    def test_parse_ready_payload_unmuted_unread(self):
        reader = GatewayNotificationsReader()
        ready_payload = {
            "read_state": {
                "entries": [
                    {"id": "channel_101", "last_message_id": "1000", "mention_count": 0},
                    {"id": "channel_102", "last_message_id": "2000", "mention_count": 1},
                ]
            },
            "user_guild_settings": [],
            "guilds": [
                {
                    "id": "guild_1",
                    "name": "General Guild",
                    "channels": [
                        {"id": "channel_101", "last_message_id": "1050"},  # newer -> unread
                        {"id": "channel_102", "last_message_id": "2000"},  # mention_count=1 -> unread
                        {"id": "channel_103", "last_message_id": None},    # no messages -> read
                    ],
                }
            ],
            "private_channels": [
                {"id": "dm_1", "last_message_id": "5000"}  # not in read state -> unread
            ],
        }

        output_map: dict[str, list[str]] = {}
        reader._parse_ready_payload(ready_payload, output_map)

        assert "General Guild" in output_map
        assert "channel_101" in output_map["General Guild"]
        assert "channel_102" in output_map["General Guild"]
        assert "channel_103" not in output_map["General Guild"]

        assert "Direct Messages" in output_map
        assert "dm_1" in output_map["Direct Messages"]

    def test_parse_ready_payload_muted_server(self):
        reader = GatewayNotificationsReader()
        ready_payload = {
            "read_state": {"entries": []},
            "user_guild_settings": [
                {"guild_id": "guild_muted", "muted": True, "channel_overrides": []}
            ],
            "guilds": [
                {
                    "id": "guild_muted",
                    "name": "Muted Server",
                    "channels": [{"id": "chan_muted", "last_message_id": "9999"}],
                }
            ],
        }
        output_map: dict[str, list[str]] = {}
        reader._parse_ready_payload(ready_payload, output_map)
        assert "Muted Server" not in output_map

    def test_fetch_unread_channels_empty_token(self):
        reader = GatewayNotificationsReader()
        assert reader.fetch_unread_channels("") == {}

    def test_fetch_unread_channels_cancellation(self):
        reader = GatewayNotificationsReader(timeout=5.0)
        cancel_event = threading.Event()
        cancel_event.set()

        with patch("websocket.WebSocketApp.run_forever"):
            with pytest.raises(RequestCancelledError, match="cancelled"):
                reader.fetch_unread_channels("test_token", cancel_event=cancel_event)
