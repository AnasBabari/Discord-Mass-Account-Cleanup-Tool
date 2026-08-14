from __future__ import annotations

import logging
import threading
from typing import Any, Protocol
import requests
from requests.exceptions import RequestException, Timeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 15.0)  # (connect_timeout, read_timeout)
USER_AGENT: str = "DiscordCleanupTool/2.1.0 (Desktop; OpenSource)"


class TransportResponse(Protocol):
    status_code: int
    text: str
    headers: dict[str, str]

    def json(self) -> Any: ...


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        timeout: tuple[float, float] | float = DEFAULT_TIMEOUT,
        cancel_event: threading.Event | None = None,
    ) -> requests.Response: ...

    def close(self) -> None: ...


class RequestsTransport:
    """Standard, clean HTTP transport backed by requests.Session.

    No anti-detection or browser-impersonation tricks are used; all requests
    are transparent and straightforward.
    """

    def __init__(self, session: requests.Session | None = None, default_timeout: tuple[float, float] = DEFAULT_TIMEOUT):
        self.session = session or requests.Session()
        self.default_timeout = default_timeout

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        timeout: tuple[float, float] | float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> requests.Response:
        if cancel_event is not None and cancel_event.is_set():
            from discord_cleanup.api.exceptions import RequestCancelledError
            raise RequestCancelledError("Request cancelled before dispatch")

        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)

        req_timeout = timeout or self.default_timeout

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                json=json_data,
                timeout=req_timeout,
            )
            return response
        except Timeout as exc:
            from discord_cleanup.api.exceptions import RequestTimeoutError
            raise RequestTimeoutError(f"HTTP request timed out: {exc}") from exc
        except RequestException as exc:
            from discord_cleanup.api.exceptions import NetworkError
            raise NetworkError(f"HTTP network error: {exc}") from exc

    def close(self) -> None:
        self.session.close()


DEFAULT_TRANSPORT = RequestsTransport()
