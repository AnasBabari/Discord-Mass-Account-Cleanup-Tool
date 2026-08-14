from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError

logger = logging.getLogger(__name__)

SERVICE_NAME = "DiscordMassCleanupTool"
KEY_USERNAME = "user_token"


class CredentialStore:
    """Encapsulates safe token storage via the OS Keyring with failure resilience."""

    def __init__(self, service_name: str = SERVICE_NAME, key_username: str = KEY_USERNAME):
        self.service_name = service_name
        self.key_username = key_username

    def get_token(self) -> str | None:
        """Retrieve stored token from OS Keyring. Returns None if not found or on error."""
        try:
            token = keyring.get_password(self.service_name, self.key_username)
            if token and token.strip():
                return token.strip()
        except (KeyringError, Exception) as exc:
            logger.debug("Could not read credential from OS keyring: %s", exc)
        return None

    def save_token(self, token: str) -> bool:
        """Store token securely in OS Keyring. Returns True if successful."""
        if not token or not token.strip():
            return False
        try:
            keyring.set_password(self.service_name, self.key_username, token.strip())
            return True
        except (KeyringError, Exception) as exc:
            logger.warning("Could not persist credential to OS keyring: %s", exc)
            return False

    def delete_token(self) -> bool:
        """Purge stored token from OS Keyring. Returns True if successful."""
        try:
            keyring.delete_password(self.service_name, self.key_username)
            return True
        except (KeyringError, Exception) as exc:
            logger.debug("Could not remove credential from OS keyring: %s", exc)
            return False


DEFAULT_CREDENTIAL_STORE = CredentialStore()
