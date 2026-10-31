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
        elif spec in ("D", "#"):
            if not ("0" <= char <= "9"):
                return False
        elif spec == "V":
            if char not in VOWELS:
                return False
        elif spec == "C":
            if char not in CONSONANTS:
                return False
        else:
            if char != spec.lower():
                return False
    return True


def matches_pattern(name: str, pattern: str = "any") -> bool:
    if ".." in name:
        return False
    cleaned = (pattern or "any").strip()
    if cleaned in ("", "any"):
        return True
    if cleaned == "pronounceable":
        return _is_pronounceable(name)
    if cleaned == "starts_with_letter":
        return "a" <= name[0] <= "z"
    if cleaned == "no_edge_punctuation":
        return name[0] not in "_." and name[-1] not in "_."
    if cleaned == "palindrome":
        return name == name[::-1] or len(set(name)) <= 2
    templates = [t.strip() for t in cleaned.split(",") if t.strip()]
    if not templates:
        return True
    return any(_matches_template(name, t) for t in templates)


def preference_score(name: str) -> tuple[int, int, int, int]:
    """Preference heuristic score (lower tuple sorts earlier).

    Ranks valid names first, shorter 3-character names before 4-character names,
    clean letter-only and pronounceable handles before numeric/punctuation handles.
    This is strictly a user-utility ordering heuristic, not an availability prediction.
    """
    invalid = 1 if validate_username(name) else 0
    length_rank = len(name)
    has_punct = 1 if ("_" in name or "." in name) else 0
    has_digit = 1 if any("0" <= c <= "9" for c in name) else 0
    readability_rank = 0 if _is_pronounceable(name) else (1 if not has_punct and not has_digit else 2 + has_punct)
    return (invalid, length_rank, readability_rank, has_punct + has_digit)


def rank_candidates_by_preference(names: list[str]) -> list[str]:
    """Order candidate names by explicit user-utility preference heuristic with stable tie-breaking."""
    indexed = list(enumerate(names))
    indexed.sort(key=lambda item: (preference_score(item[1]), item[0]))
    return [name for _, name in indexed]


def _feistel_round(right: int, round_index: int, seed: int) -> int:
    payload = f"{seed}:{round_index}:{right}".encode("ascii")
    digest = hashlib.blake2s(payload, digest_size=4).digest()
    return int.from_bytes(digest, "big")


def permute_index(index: int, domain_size: int, seed: int) -> int:
    """Bijective cycle-walking Feistel permutation over [0, domain_size) in O(1) memory."""
    if index < 0 or index >= domain_size:
        raise ValueError("Permutation index out of range.")
    if domain_size <= 1:
        return 0
    total_bits = max(2, (domain_size - 1).bit_length())
    if total_bits % 2 == 1:
        total_bits += 1
    half_bits = total_bits // 2
    mask = (1 << half_bits) - 1
    value = index
    while True:
        left = (value >> half_bits) & mask
        right = value & mask
        for round_index in range(4):
            f_val = _feistel_round(right, round_index, seed) & mask
            left, right = right, left ^ f_val
        value = (left << half_bits) | right
        if value < domain_size:
            return value


def _decode_raw_index(lengths: tuple[int, ...], raw_index: int, alphabet: str) -> str:
    base = len(alphabet)
    offset = 0
    for length in lengths:
        size = base**length
        if raw_index < offset + size:
            value = raw_index - offset
            chars = [alphabet[0]] * length
            for position in range(length - 1, -1, -1):
                value, digit = divmod(value, base)
                chars[position] = alphabet[digit]
            return "".join(chars)
        offset += size
    raise ValueError("Raw candidate index out of range.")


def raw_domain_size(lengths: tuple[int, ...], preferences: CandidatePreferences | None = None) -> int:
    validated = validate_lengths(lengths)
    alphabet = resolve_alphabet(preferences)
    base = len(alphabet)
    return sum(base**length for length in validated)


def _uses_default_exhaustive(preferences: CandidatePreferences | None) -> bool:
    if preferences is None:
        return True
    return (
        preferences.strategy in ("exhaustive", "generated")
        and preferences.charset_mode == "all"
        and preferences.pattern in ("", "any")
        and not preferences.priority_names
    )
