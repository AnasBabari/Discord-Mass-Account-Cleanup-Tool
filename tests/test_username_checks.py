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


@pytest.mark.parametrize(
    "status,payload,error",
    [
        (401, {"message": "Unauthorized"}, AuthenticationError),
        (403, {"message": "Forbidden"}, UpstreamBlockedError),
        (404, {"message": "Unknown endpoint"}, InvalidPayloadError),
        (500, {"message": "Failure"}, InvalidPayloadError),
        (400, {"captcha_key": ["captcha-required"]}, UpstreamBlockedError),
        (429, {"captcha_key": ["captcha-required"]}, UpstreamBlockedError),
        (400, {"errors": {"other_field": {"message": "bad"}}}, InvalidPayloadError),
    ],
)
def test_operational_errors_are_not_availability(username_client, status, payload, error):
    username_client.transport.request.return_value = MockResponse(status_code=status, json_data=payload)
    with pytest.raises(error):
        username_client.check_username_availability("abc")
    assert username_client.transport.request.call_count == 1


def test_username_field_rejection(username_client):
    username_client.transport.request.return_value = MockResponse(
        status_code=400,
        json_data={"errors": {"username": {"_errors": [{"message": "Reserved"}]}}},
    )
    assert username_client.check_username_availability("abc").status == UsernameStatus.INVALID


@pytest.mark.parametrize("status", [200, 429])
def test_html_blocks_stop_immediately(username_client, status):
    username_client.transport.request.return_value = MockResponse(status_code=status, text="<html>blocked</html>")
    with pytest.raises(UpstreamBlockedError):
        username_client.check_username_availability("abc")
    assert username_client.transport.request.call_count == 1


def test_backoff_can_be_cancelled(username_client):
    event = threading.Event()
    username_client.transport.request.return_value = MockResponse(status_code=429, json_data={"retry_after": 10})
    waits = []

    def cancel(seconds):
        waits.append(seconds)
        event.set()

    with pytest.raises(RequestCancelledError):
        username_client.check_username_availability("abc", cancel_event=event, on_backoff=cancel)
    assert waits == [10]
    assert username_client.transport.request.call_count == 1


def test_rate_limit_is_bounded(username_client):
    username_client.max_retries = 2
    username_client.transport.request.return_value = MockResponse(status_code=429, json_data={"retry_after": 0})
    with pytest.raises(RateLimitExceededError):
        username_client.check_username_availability("abc")
    assert username_client.transport.request.call_count == 2
