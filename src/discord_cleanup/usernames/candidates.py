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


def parse_custom_names(text: str) -> list[str]:
    names: dict[str, None] = {}
    for value in re.split(r"[\s,]+", text.strip()):
        if value:
            # Redact accidental credential pastes before they can reach saved scan data.
            name = sanitize_token(value).lower().removeprefix("@")
            names.setdefault(name, None)
    return list(names)


def validate_lengths(lengths: tuple[int, ...]) -> tuple[int, ...]:
    if not lengths or any(length not in (3, 4) for length in lengths):
        raise ValueError("Choose three-character names, four-character names, or both.")
    return tuple(sorted(set(lengths)))


def resolve_alphabet(preferences: CandidatePreferences | None = None) -> str:
    if preferences is None or preferences.charset_mode == "all":
        return ALPHABET
    mode = preferences.charset_mode
    if mode in ("letters_only", "letters"):
        chars = "".join(c for c in ALPHABET if "a" <= c <= "z")
    elif mode in ("alphanumeric", "no_punctuation"):
        chars = "".join(c for c in ALPHABET if ("a" <= c <= "z") or ("0" <= c <= "9"))
    elif mode in ("no_periods", "letters_digits_underscore"):
        chars = "".join(c for c in ALPHABET if c != ".")
    elif mode == "custom":
        seen: dict[str, None] = {}
        for c in preferences.allowed_chars.lower():
            if c in ALPHABET:
                seen.setdefault(c, None)
        chars = "".join(seen)
    else:
        raise ValueError(f"Unknown character-set preference: {mode}")
    if not chars:
        raise ValueError("Selected character preference leaves no valid characters.")
    return chars


def _is_pronounceable(name: str) -> bool:
    if not all("a" <= c <= "z" for c in name):
        return False
    shape = "".join("V" if c in VOWELS else "C" for c in name)
    if len(name) == 3:
        return shape in ("CVC", "VCV")
    return shape in ("CVCV", "VCVC", "CVCC", "CCVC")


def _matches_template(name: str, template: str) -> bool:
    if len(name) != len(template):
        return False
    for char, spec in zip(name, template, strict=True):
        if spec in ("?", "*"):
            continue
        elif spec == "L":
            if not ("a" <= char <= "z"):
                return False
