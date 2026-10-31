from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.api.exceptions import (
    AuthenticationError,
    InvalidPayloadError,
    RateLimitExceededError,
    RequestCancelledError,
    UpstreamBlockedError,
)
from discord_cleanup.models.usernames import UsernameStatus
from tests.test_api_client import MockResponse


@pytest.fixture
def username_client(mock_rate_limiter):
    return DiscordApiClient("private-token", transport=MagicMock(), rate_limiter=mock_rate_limiter)


@pytest.mark.parametrize("taken,status", [(False, UsernameStatus.AVAILABLE), (True, UsernameStatus.UNAVAILABLE)])
def test_explicit_availability(username_client, taken, status):
    username_client.transport.request.return_value = MockResponse(json_data={"taken": taken})
    result = username_client.check_username_availability("ab_c")
    assert result.status == status
    assert result.username == "ab_c"
    assert result.checked_at.endswith("+00:00")
    call = username_client.transport.request.call_args.kwargs
    assert call["url"].endswith("/api/v10/users/@me/pomelo-attempt")
    assert call["json_data"] == {"username": "ab_c"}
    assert call["headers"] == {"Authorization": "private-token"}


@pytest.mark.parametrize("payload", [{}, {"taken": None}, {"taken": 0}, {"taken": "false"}, [], {"message": "error"}])
def test_ambiguous_response_never_available(username_client, payload):
    username_client.transport.request.return_value = MockResponse(json_data=payload)
    with pytest.raises(InvalidPayloadError):
        username_client.check_username_availability("abc")


@pytest.mark.parametrize("name", ["ab", "abcde", "a..", "ABC", "a-b", "a b", "äbc"])
def test_invalid_input_never_sent(username_client, name):
    assert username_client.check_username_availability(name).status == UsernameStatus.INVALID
    username_client.transport.request.assert_not_called()
