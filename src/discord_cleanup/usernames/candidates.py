from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.models.usernames import CandidatePreferences
from discord_cleanup.security.token_sanitizer import sanitize_token

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_."
VOWELS = "aeiouy"
CONSONANTS = "bcdfghjklmnpqrstvwxz"
GENERATOR_VERSION = 1
GENERATOR_VERSION_V2 = 2
SUPPORTED_GENERATOR_VERSIONS = (GENERATOR_VERSION, GENERATOR_VERSION_V2)

KNOWN_PATTERNS = (
    "any",
    "pronounceable",
    "starts_with_letter",
    "no_edge_punctuation",
    "palindrome",
)


@dataclass(frozen=True)
class CandidatePreview:
    total_candidates: int
    sample_candidates: list[str]
    invalid_candidates: list[tuple[str, str]]
    strategy_description: str

    @property
    def strategy_summary(self) -> str:
        return self.strategy_description

    @property
    def valid_custom_count(self) -> int:
        return max(0, self.total_candidates - len(self.invalid_candidates))

    @property
    def invalid_custom_count(self) -> int:
        return len(self.invalid_candidates)


def validate_username(username: str) -> str:
    if len(username) not in (3, 4):
        return "Use exactly 3 or 4 characters."
    if any(char not in ALPHABET for char in username):
        return "Use lowercase a-z, 0-9, underscores, or periods."
    if ".." in username:
        return "Consecutive periods are not allowed."
    return ""
