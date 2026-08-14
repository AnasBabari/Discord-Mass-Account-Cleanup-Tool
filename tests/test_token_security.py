from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from discord_cleanup.logging.logger import TokenRedactingFilter, TokenRedactingFormatter
from discord_cleanup.security.credentials import CredentialStore
from discord_cleanup.security.token_sanitizer import sanitize_token

# Build synthetic mock tokens dynamically to prevent false positives in GitHub secret scanners
SYNTHETIC_USER_TOKEN = "M" * 24 + "." + "G" * 6 + "." + "a" * 27
SYNTHETIC_APP_TOKEN = "N" * 24 + "." + "G" * 6 + "." + "b" * 27
SYNTHETIC_MFA_TOKEN = "mfa." + "x" * 65


class TestTokenSanitizer:
    @pytest.mark.parametrize(
        "token",
        [
            SYNTHETIC_USER_TOKEN,
            SYNTHETIC_APP_TOKEN,
            SYNTHETIC_MFA_TOKEN,
        ],
    )
    def test_direct_token_redaction(self, token: str):
        result = sanitize_token(token)
        assert "[REDACTED_TOKEN]" in result
        assert token not in result

    def test_authorization_header_redaction(self):
        token = SYNTHETIC_USER_TOKEN
        raw = f"Headers: {{'Authorization': '{token}', 'User-Agent': 'Test'}}"
        sanitized = sanitize_token(raw)
        assert "[REDACTED_TOKEN]" in sanitized
        assert token not in sanitized

    def test_bearer_authorization_redaction(self):
        mfa_token = SYNTHETIC_MFA_TOKEN
        raw = f"Authorization: Bearer {mfa_token}"
        sanitized = sanitize_token(raw)
        assert "[REDACTED_TOKEN]" in sanitized
        assert mfa_token not in sanitized

    def test_json_payload_redaction(self):
        token = SYNTHETIC_USER_TOKEN
        raw = f'{{"token": "{token}", "status": "ok"}}'
        sanitized = sanitize_token(raw)
        assert "[REDACTED_TOKEN]" in sanitized
        assert token not in sanitized

    def test_url_query_parameter_redaction(self):
        token = SYNTHETIC_USER_TOKEN
        raw = f"https://discord.com/api/v10/users/@me?token={token}&v=10"
        sanitized = sanitize_token(raw)
        assert "[REDACTED_TOKEN]" in sanitized
        assert token not in sanitized

    def test_traceback_exception_redaction(self):
        token = SYNTHETIC_USER_TOKEN
        raw_traceback = f"""Traceback (most recent call last):
  File "api.py", line 45, in request
    raise requests.exceptions.HTTPError("401 Unauthorized for Authorization: {token}")
requests.exceptions.HTTPError: 401 Unauthorized
"""
        sanitized = sanitize_token(raw_traceback)
        assert "[REDACTED_TOKEN]" in sanitized
        assert token not in sanitized

    def test_none_and_empty_inputs(self):
        assert sanitize_token(None) == ""
        assert sanitize_token("") == ""
        assert sanitize_token(12345) == "12345"


class TestLoggingRedaction:
    def test_filter_redacts_record_message(self):
        filt = TokenRedactingFilter()
        token = SYNTHETIC_USER_TOKEN
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg=f"Failed request with token {token}",
            args=(),
            exc_info=None,
        )
        filt.filter(record)
        assert "[REDACTED_TOKEN]" in record.msg
        assert token not in record.msg

    def test_filter_redacts_record_args(self):
        filt = TokenRedactingFilter()
        token = SYNTHETIC_USER_TOKEN
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="User auth header: %s",
            args=(f"Authorization: {token}",),
            exc_info=None,
        )
        filt.filter(record)
        formatter = TokenRedactingFormatter("%(message)s")
        formatted = formatter.format(record)
        assert "[REDACTED_TOKEN]" in formatted
        assert token not in formatted


class TestCredentialStore:
    def test_save_and_get_token(self):
        store = CredentialStore(service_name="TestService", key_username="test_user")
        with patch("keyring.get_password", return_value="saved_secret_token"), \
             patch("keyring.set_password") as mock_set:
            assert store.save_token("saved_secret_token") is True
            mock_set.assert_called_once_with("TestService", "test_user", "saved_secret_token")
            assert store.get_token() == "saved_secret_token"

    def test_delete_token(self):
        store = CredentialStore(service_name="TestService", key_username="test_user")
        with patch("keyring.delete_password") as mock_del:
            assert store.delete_token() is True
            mock_del.assert_called_once_with("TestService", "test_user")

    def test_keyring_error_fallback(self):
        store = CredentialStore(service_name="TestService", key_username="test_user")
        with patch("keyring.get_password", side_effect=Exception("Keyring locked")):
            assert store.get_token() is None
        with patch("keyring.set_password", side_effect=Exception("Keyring error")):
            assert store.save_token("token") is False
        with patch("keyring.delete_password", side_effect=Exception("Keyring error")):
            assert store.delete_token() is False
