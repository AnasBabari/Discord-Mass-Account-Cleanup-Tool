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
        allow_redirects: bool | None = None,
    ) -> requests.Response: ...

    def close(self) -> None: ...


class RequestsTransport:
    """Standard, clean HTTP transport backed by requests.Session.

    No anti-detection or browser-impersonation tricks are used; all requests
    are transparent and straightforward. Callers that require strict status
    verification before redirects (such as username availability endpoints)
    can disable redirects per request or per transport instance.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        default_timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        allow_redirects: bool = True,
    ):
        self.session = session or requests.Session()
        self.default_timeout = default_timeout
        self.allow_redirects = allow_redirects

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        timeout: tuple[float, float] | float | None = None,
        cancel_event: threading.Event | None = None,
        allow_redirects: bool | None = None,
    ) -> requests.Response:
        if cancel_event is not None and cancel_event.is_set():
            from discord_cleanup.api.exceptions import RequestCancelledError

            raise RequestCancelledError("Request cancelled before dispatch")

        req_headers = {"User-Agent": USER_AGENT}
        if headers:
            req_headers.update(headers)

        req_timeout = timeout or self.default_timeout
        follow_redirects = self.allow_redirects if allow_redirects is None else allow_redirects

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                json=json_data,
                timeout=req_timeout,
                allow_redirects=follow_redirects,
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
