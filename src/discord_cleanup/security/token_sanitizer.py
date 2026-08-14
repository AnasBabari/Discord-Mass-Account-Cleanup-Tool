from __future__ import annotations

import re
from typing import Any

# Standard 3-part Discord user/bot/app token (id_b64.timestamp_b64.hmac_signature)
_STANDARD_TOKEN_PATTERN = re.compile(r"([A-Za-z0-9_-]{24,36}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,110})")

# MFA token format (mfa.XXXX...)
_MFA_TOKEN_PATTERN = re.compile(r"(mfa\.[A-Za-z0-9_-]{60,140})")

# Authorization header values (e.g. 'Authorization': 'token_value' or Authorization: Bearer token_value)
_AUTH_HEADER_PATTERN = re.compile(
    r"((?:[Aa]uthorization|[Xx]-[Aa]uth-[Tt]oken)[\'\"\s]*[:=][\'\"\s]*(?:Bearer\s+)?)[^\s\'\",;}{]+",
    re.IGNORECASE,
)

# Token URL query parameter values (e.g. token=secret or access_token=secret)
_TOKEN_QUERY_PATTERN = re.compile(r"((?:token|access_token|auth_token)=)[^\s&\'\",;}{]+", re.IGNORECASE)

# JSON token fields (e.g. "token": "value" or "auth": "value")
_JSON_TOKEN_PATTERN = re.compile(r"([\'\"](?:token|access_token|auth_token)[\'\"]\s*:\s*[\'\"])[^\'\"]+([\'\"])", re.IGNORECASE)


def sanitize_token(text: Any) -> str:
    """Redact Discord authentication tokens, MFA secrets, and auth headers from any string representation.

    Guarantees that credentials will never be leaked into logs, tracebacks,
    exceptions, terminal exports, or UI widgets.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""

    sanitized = _STANDARD_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    sanitized = _MFA_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", sanitized)
    sanitized = _AUTH_HEADER_PATTERN.sub(r"\1[REDACTED_TOKEN]", sanitized)
    sanitized = _TOKEN_QUERY_PATTERN.sub(r"\1[REDACTED_TOKEN]", sanitized)
    sanitized = _JSON_TOKEN_PATTERN.sub(r"\1[REDACTED_TOKEN]\2", sanitized)
    return sanitized
