import threading
from unittest.mock import MagicMock
import pytest
from requests.exceptions import ConnectionError, ReadTimeout

from discord_cleanup.api.exceptions import (
    NetworkError,
    RequestCancelledError,
    RequestTimeoutError,
)
from discord_cleanup.transport.http_transport import RequestsTransport


class TestRequestsTransport:
    def test_successful_request(self):
        mock_session = MagicMock()
        mock_response = MagicMock(status_code=200, text='{"status": "ok"}')
        mock_session.request.return_value = mock_response

        transport = RequestsTransport(session=mock_session)
        resp = transport.request("GET", "https://discord.com/api/v10/users/@me", headers={"Authorization": "token"})

        assert resp.status_code == 200
        mock_session.request.assert_called_once()
        args, kwargs = mock_session.request.call_args
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == "https://discord.com/api/v10/users/@me"
        assert kwargs["headers"]["Authorization"] == "token"
        assert "DiscordCleanupTool" in kwargs["headers"]["User-Agent"]

    def test_cancellation_before_dispatch(self):
        transport = RequestsTransport()
        cancel_event = threading.Event()
        cancel_event.set()

        with pytest.raises(RequestCancelledError, match="cancelled before dispatch"):
            transport.request("GET", "https://discord.com/api/v10/users/@me", cancel_event=cancel_event)

    def test_timeout_conversion(self):
        mock_session = MagicMock()
        mock_session.request.side_effect = ReadTimeout("Read timed out")

        transport = RequestsTransport(session=mock_session)
        with pytest.raises(RequestTimeoutError, match="timed out"):
            transport.request("GET", "https://discord.com/api/v10/users/@me")

    def test_network_error_conversion(self):
        mock_session = MagicMock()
        mock_session.request.side_effect = ConnectionError("Failed to establish connection")

        transport = RequestsTransport(session=mock_session)
        with pytest.raises(NetworkError, match="network error"):
            transport.request("GET", "https://discord.com/api/v10/users/@me")

    def test_close_session(self):
        mock_session = MagicMock()
        transport = RequestsTransport(session=mock_session)
        transport.close()
        mock_session.close.assert_called_once()
