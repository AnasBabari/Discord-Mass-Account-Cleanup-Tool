from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest

from discord_cleanup.api.client import DiscordApiClient, get_clean_error
from discord_cleanup.api.exceptions import (
    AuthenticationError,
    InvalidPayloadError,
    RateLimitExceededError,
    UpstreamBlockedError,
)
from discord_cleanup.models.domain import RelationshipType


class MockResponse:
    def __init__(self, status_code: int = 200, text: str = "", json_data: dict | list | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._text = text or (json.dumps(json_data) if json_data is not None else "")
        self._json_data = json_data
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self._text)


class TestDiscordApiClient:
    @pytest.fixture
    def client(self, mock_rate_limiter):
        mock_transport = MagicMock()
        client = DiscordApiClient(
            token="test_token",
            transport=mock_transport,
            rate_limiter=mock_rate_limiter,
        )
        return client

    def test_verify_token_success(self, client):
        client.transport.request.return_value = MockResponse(
            status_code=200,
            json_data={
                "id": "123456",
                "username": "tester",
                "discriminator": "0",
                "global_name": "Test User",
                "avatar": "avatar_hash",
            },
        )
        user = client.verify_token()
        assert user.id == "123456"
        assert user.username == "tester"
        assert user.display_name == "Test User"

    def test_verify_token_invalid_401(self, client):
        client.transport.request.return_value = MockResponse(status_code=401, json_data={"message": "401: Unauthorized"})
        with pytest.raises(AuthenticationError, match="Invalid Discord authentication token"):
            client.verify_token()

    def test_get_guilds_pagination(self, client):
        page_1 = [{"id": f"g_{i}", "name": f"Guild {i}", "owner": False} for i in range(200)]
        page_2 = [{"id": "g_201", "name": "Guild 201", "owner": True}]

        client.transport.request.side_effect = [
            MockResponse(status_code=200, json_data=page_1),
            MockResponse(status_code=200, json_data=page_2),
        ]

        guilds = client.get_guilds()
        assert len(guilds) == 201
        assert guilds[0].id == "g_0"
        assert guilds[-1].id == "g_201"
        assert guilds[-1].owner is True
        assert client.transport.request.call_count == 2

    def test_leave_guild(self, client):
        client.transport.request.return_value = MockResponse(status_code=204)
        status, err = client.leave_guild("12345")
        assert status == 204
        assert err == ""

    def test_get_relationships_and_filters(self, client):
        raw_relationships = [
            {"id": "1", "type": 1, "user": {"id": "1", "username": "alice"}},
            {"id": "2", "type": 2, "user": {"id": "2", "username": "spammer"}},
            {"id": "3", "type": 1, "user": {"id": "3", "username": "bob"}},
        ]
        client.transport.request.return_value = MockResponse(status_code=200, json_data=raw_relationships)

        all_rels = client.get_relationships()
        assert len(all_rels) == 3

        client.transport.request.return_value = MockResponse(status_code=200, json_data=raw_relationships)
        friends = client.get_friends()
        assert len(friends) == 2
        assert all(f.rel_type == RelationshipType.FRIEND for f in friends)

        client.transport.request.return_value = MockResponse(status_code=200, json_data=raw_relationships)
        blocked = client.get_blocked_users()
        assert len(blocked) == 1
        assert blocked[0].user.username == "spammer"

    def test_remove_friend(self, client):
        client.transport.request.return_value = MockResponse(status_code=204)
        status, err = client.remove_friend("user_1")
        assert status == 204

    def test_block_and_unblock_user(self, client):
        client.transport.request.return_value = MockResponse(status_code=204)
        status_b, _ = client.block_user("user_2")
        assert status_b == 204

        client.transport.request.return_value = MockResponse(status_code=204)
        status_u, _ = client.unblock_user("user_2")
        assert status_u == 204

    def test_ack_read_states_chunk(self, client):
        client.transport.request.return_value = MockResponse(status_code=200, json_data={"read_states": []})
        status, _ = client.ack_read_states_chunk([{"channel_id": "c1", "message_id": "m1", "read_state_type": 0}])
        assert status == 200

    def test_rate_limit_429_backoff_and_retry(self, client):
        client.transport.request.side_effect = [
            MockResponse(status_code=429, json_data={"retry_after": 0.01}),
            MockResponse(status_code=200, json_data={"id": "user_id", "username": "user"}),
        ]
        user = client.verify_token()
        assert user.id == "user_id"
        assert client.transport.request.call_count == 2

    def test_rate_limit_max_retries_exceeded(self, client):
        client.max_retries = 2
        client.transport.request.return_value = MockResponse(status_code=429, json_data={"retry_after": 0.01})
        with pytest.raises(RateLimitExceededError, match="Max retries"):
            client.verify_token()

    def test_cloudflare_ip_ban_1015(self, client):
        html_response = "<html><title>Error 1015</title><body>You are being rate limited (error 1015)</body></html>"
        client.transport.request.return_value = MockResponse(status_code=429, text=html_response)
        with pytest.raises(UpstreamBlockedError, match="Error 1015"):
            client.verify_token()

    def test_invalid_json_payload(self, client):
        client.transport.request.return_value = MockResponse(status_code=200, text="NOT_VALID_JSON{")
        with pytest.raises(InvalidPayloadError, match="Invalid JSON"):
            client.verify_token()


class TestGetCleanError:
    def test_clean_error_message(self):
        resp = MockResponse(status_code=400, json_data={"message": "Unknown Channel"})
        assert get_clean_error(resp) == "Unknown Channel"

    def test_clean_error_code(self):
        resp = MockResponse(status_code=400, json_data={"code": 50001})
        assert get_clean_error(resp) == "API Code 50001"

    def test_clean_error_html_1015(self):
        resp = MockResponse(status_code=429, text="<html>1015 IP Ban</html>")
        assert "1015" in get_clean_error(resp)

    def test_clean_error_generic_html(self):
        resp = MockResponse(status_code=502, text="<html>Bad Gateway</html>")
        assert "HTML Error" in get_clean_error(resp)
