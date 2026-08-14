from __future__ import annotations

import logging
import threading
from typing import Any

import requests

from discord_cleanup.api.exceptions import (
    AuthenticationError,
    InvalidPayloadError,
    RateLimitExceededError,
    RequestCancelledError,
    UpstreamBlockedError,
)
from discord_cleanup.api.rate_limiter import GLOBAL_RATE_LIMITER, RequestCoordinator
from discord_cleanup.models.domain import Guild, Relationship, RelationshipType, User
from discord_cleanup.transport.http_transport import DEFAULT_TRANSPORT, Transport

logger = logging.getLogger(__name__)

BASE_URL = "https://discord.com/api/v10"


def get_clean_error(response: requests.Response | Any) -> str:
    """Extract a user-readable error summary without dumping HTML or sensitive headers."""
    if response is None:
        return "No response received"
    text = getattr(response, "text", "")
    if "<html" in text.lower():
        if "1015" in text:
            return "Upstream IP Ban (Error 1015)"
        return "HTML Error Response (Upstream Block)"
    try:
        data = response.json()
        if isinstance(data, dict):
            if "message" in data:
                return str(data["message"])
            if "code" in data:
                return f"API Code {data['code']}"
    except (ValueError, AttributeError):
        pass
    cleaned = text.strip()
    return (cleaned[:120] + "...") if len(cleaned) > 120 else (cleaned or f"HTTP {getattr(response, 'status_code', 'Unknown')}")


class DiscordApiClient:
    """High-level Discord REST API client for account management."""

    def __init__(
        self,
        token: str = "",
        transport: Transport = DEFAULT_TRANSPORT,
        rate_limiter: RequestCoordinator = GLOBAL_RATE_LIMITER,
        base_url: str = BASE_URL,
        max_retries: int = 5,
    ):
        self.token = token
        self.transport = transport
        self.rate_limiter = rate_limiter
        self.base_url = base_url
        self.max_retries = max_retries

    def set_token(self, token: str) -> None:
        self.token = token

    def clear_token(self) -> None:
        self.token = ""

    def request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        token: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> requests.Response:
        """Execute an authenticated Discord API request with coordinated rate-limit and backoff handling."""
        auth_token = token or self.token
        if not auth_token:
            raise AuthenticationError("No Discord authentication token provided.")

        url = f"{self.base_url}{endpoint}"
        headers = {"Authorization": auth_token}
        retries = 0

        while retries < self.max_retries:
            if not self.rate_limiter.wait(cancel_event):
                raise RequestCancelledError("Operation cancelled while awaiting rate-limit clearance.")

            response = self.transport.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json_data=json_data,
                cancel_event=cancel_event,
            )

            # Handle 429 Rate Limiting
            if response.status_code == 429:
                wait_seconds = 5.0
                if "<html" in response.text.lower():
                    if "1015" in response.text:
                        raise UpstreamBlockedError(
                            "Cloudflare/Upstream IP Ban (Error 1015) - Requests blocked temporarily."
                        )
                else:
                    try:
                        payload = response.json()
                        if isinstance(payload, dict) and payload.get("retry_after") is not None:
                            wait_seconds = float(payload["retry_after"])
                    except (ValueError, TypeError):
                        pass
                    if wait_seconds == 5.0:
                        try:
                            wait_seconds = float(response.headers.get("Retry-After", 5.0))
                        except (TypeError, ValueError):
                            wait_seconds = 5.0

                wait_seconds = max(0.0, wait_seconds)
                logger.info("Rate-limited on %s %s - backing off for %.2fs", method, endpoint, wait_seconds)
                self.rate_limiter.backoff(wait_seconds)
                if not self.rate_limiter.delay(wait_seconds, cancel_event):
                    raise RequestCancelledError("Operation cancelled during rate-limit backoff delay.")

                retries += 1
                continue

            return response

        raise RateLimitExceededError(f"Max retries ({self.max_retries}) exceeded for {method} {endpoint}")

    def verify_token(self, token: str | None = None, cancel_event: threading.Event | None = None) -> User:
        """Verify authentication token against /users/@me and return authenticated User domain model."""
        response = self.request("GET", "/users/@me", token=token, cancel_event=cancel_event)
        if response.status_code == 401:
            raise AuthenticationError("Invalid Discord authentication token.")
        if response.status_code != 200:
            raise InvalidPayloadError(f"Failed to fetch user profile: {get_clean_error(response)}")

        try:
            user_data = response.json()
            return User.from_dict(user_data)
        except Exception as exc:
            raise InvalidPayloadError(f"Invalid JSON in user profile response: {exc}") from exc

    def get_guilds(self, cancel_event: threading.Event | None = None) -> list[Guild]:
        """Fetch all guilds/servers the authenticated user is currently in (paginated)."""
        guilds: list[Guild] = []
        after: str | None = None

        while True:
            params: dict[str, Any] = {"limit": 200}
            if after:
                params["after"] = after

            response = self.request("GET", "/users/@me/guilds", params=params, cancel_event=cancel_event)
            if response.status_code == 401:
                raise AuthenticationError("Invalid token when fetching guilds.")
            if response.status_code != 200:
                raise InvalidPayloadError(f"Failed to fetch guild list: {get_clean_error(response)}")

            try:
                page = response.json()
            except Exception as exc:
                raise InvalidPayloadError(f"Malformed guild list JSON: {exc}") from exc

            if not isinstance(page, list):
                break

            for item in page:
                if isinstance(item, dict):
                    guilds.append(Guild.from_dict(item))

            if len(page) < 200:
                break
            after = page[-1].get("id")
            if not after:
                break

        return guilds

    def leave_guild(self, guild_id: str, cancel_event: threading.Event | None = None) -> tuple[int, str]:
        """Leave a server by guild ID. Returns (status_code, error_message)."""
        response = self.request("DELETE", f"/users/@me/guilds/{guild_id}", cancel_event=cancel_event)
        err = "" if response.status_code in (200, 204) else get_clean_error(response)
        return response.status_code, err

    def get_relationships(self, cancel_event: threading.Event | None = None) -> list[Relationship]:
        """Fetch all account relationships (friends, incoming/outgoing, blocked)."""
        response = self.request("GET", "/users/@me/relationships", cancel_event=cancel_event)
        if response.status_code == 401:
            raise AuthenticationError("Invalid token when fetching relationships.")
        if response.status_code != 200:
            raise InvalidPayloadError(f"Failed to fetch relationships: {get_clean_error(response)}")

        try:
            data = response.json()
        except Exception as exc:
            raise InvalidPayloadError(f"Malformed relationships JSON: {exc}") from exc

        if not isinstance(data, list):
            return []

        relationships: list[Relationship] = []
        for rel in data:
            if isinstance(rel, dict):
                relationships.append(Relationship.from_dict(rel))
        return relationships

    def get_friends(self, cancel_event: threading.Event | None = None) -> list[Relationship]:
        """Fetch only confirmed friends (RelationshipType == FRIEND)."""
        all_rel = self.get_relationships(cancel_event=cancel_event)
        return [r for r in all_rel if r.rel_type == RelationshipType.FRIEND]

    def get_blocked_users(self, cancel_event: threading.Event | None = None) -> list[Relationship]:
        """Fetch only blocked users (RelationshipType == BLOCKED)."""
        all_rel = self.get_relationships(cancel_event=cancel_event)
        return [r for r in all_rel if r.rel_type == RelationshipType.BLOCKED]

    def remove_friend(self, user_id: str, cancel_event: threading.Event | None = None) -> tuple[int, str]:
        """Remove a friend relationship by user ID. Returns (status_code, error_message)."""
        response = self.request("DELETE", f"/users/@me/relationships/{user_id}", cancel_event=cancel_event)
        err = "" if response.status_code in (200, 204) else get_clean_error(response)
        return response.status_code, err

    def block_user(self, user_id: str, cancel_event: threading.Event | None = None) -> tuple[int, str]:
        """Block a user by ID. Returns (status_code, error_message)."""
        response = self.request(
            "PUT", f"/users/@me/relationships/{user_id}", json_data={"type": 2}, cancel_event=cancel_event
        )
        err = "" if response.status_code in (200, 204) else get_clean_error(response)
        return response.status_code, err

    def unblock_user(self, user_id: str, cancel_event: threading.Event | None = None) -> tuple[int, str]:
        """Unblock a user by ID. Returns (status_code, error_message)."""
        response = self.request("DELETE", f"/users/@me/relationships/{user_id}", cancel_event=cancel_event)
        err = "" if response.status_code in (200, 204) else get_clean_error(response)
        return response.status_code, err

    def ack_read_states_chunk(
        self, chunk: list[dict[str, Any]], cancel_event: threading.Event | None = None
    ) -> tuple[int, str]:
        """Acknowledge a batch of read states. Returns (status_code, error_message)."""
        response = self.request("POST", "/read-states/ack-bulk", json_data={"read_states": chunk}, cancel_event=cancel_event)
        err = "" if response.status_code in (200, 204) else get_clean_error(response)
        return response.status_code, err


DEFAULT_API_CLIENT = DiscordApiClient()
