from __future__ import annotations


class DiscordCleanupError(Exception):
    """Base exception for all Discord Cleanup Tool errors."""


class RequestCancelledError(DiscordCleanupError):
    """Raised when an operation is cancelled by the user or shutdown."""


class RequestTimeoutError(DiscordCleanupError):
    """Raised when an HTTP connection or read times out."""


class NetworkError(DiscordCleanupError):
    """Raised on socket/transport communication failures."""


class AuthenticationError(DiscordCleanupError):
    """Raised when an invalid or expired token is provided (HTTP 401)."""


class RateLimitExceededError(DiscordCleanupError):
    """Raised when max rate-limit retries have been exceeded."""


class UpstreamBlockedError(DiscordCleanupError):
    """Raised when Discord or upstream protection blocks the IP/request (e.g. Cloudflare 1015)."""


class InvalidPayloadError(DiscordCleanupError):
    """Raised when an unexpected or malformed payload is returned."""
