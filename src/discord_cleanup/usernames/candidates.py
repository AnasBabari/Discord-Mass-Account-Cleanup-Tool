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


def generator_version_for_preferences(preferences: CandidatePreferences | None = None) -> int:
    return GENERATOR_VERSION if _uses_default_exhaustive(preferences) else GENERATOR_VERSION_V2


_CANDIDATE_COUNT_CACHE: dict[tuple[tuple[int, ...], str, str, tuple[str, ...]], int] = {}
_CACHE_LOCK = threading.Lock()


def _normalize_priority_entries(
    lengths: tuple[int, ...],
    preferences: CandidatePreferences | None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (valid_unique_priority_names, invalid_priority_entries_with_reasons)."""
    if preferences is None or not preferences.priority_names:
        return [], []
    valid_seen: dict[str, None] = {}
    invalid_entries: list[tuple[str, str]] = []
    for raw in preferences.priority_names:
        if not raw:
            continue
        cleaned = sanitize_token(str(raw)).strip().lower().removeprefix("@")
        if not cleaned or cleaned in valid_seen:
            continue
        err = validate_username(cleaned)
        if err:
            invalid_entries.append((cleaned, err))
            continue
        if len(cleaned) not in lengths:
            invalid_entries.append((cleaned, f"Length {len(cleaned)} is not in selected lengths {lengths}."))
            continue
        valid_seen[cleaned] = None
    return list(valid_seen), invalid_entries


def _count_position_constrained(
    allowed_per_pos: list[set[str]],
) -> int:
    """Count strings in the Cartesian product of allowed_per_pos that contain no '..'."""
    other, period = 1, 0
    for allowed in allowed_per_pos:
        if not allowed:
            return 0
        has_dot = 1 if "." in allowed else 0
        non_dot = len(allowed) - has_dot
        other, period = (other + period) * non_dot, other * has_dot
    return other + period


def _template_char_matches(char: str, spec: str) -> bool:
    if spec in ("?", "*"):
        return True
    if spec == "L":
        return "a" <= char <= "z"
    if spec in ("D", "#"):
        return "0" <= char <= "9"
    if spec == "V":
        return char in VOWELS
    if spec == "C":
        return char in CONSONANTS
    return char == spec.lower()


def _count_domain_matches_for_length(length: int, alphabet: str, pattern: str) -> int:
    """Compute the exact number of valid strings of `length` over `alphabet` matching `pattern` in O(length * |alphabet|)."""
    alpha_set = set(alphabet)
    if not alpha_set:
        return 0
    cleaned = (pattern or "any").strip()
    if cleaned in ("", "any"):
        return _count_position_constrained([alpha_set] * length)

    if cleaned == "starts_with_letter":
        first = {c for c in alpha_set if "a" <= c <= "z"}
        return _count_position_constrained([first] + [alpha_set] * (length - 1))

    if cleaned == "no_edge_punctuation":
        edge = {c for c in alpha_set if c not in "_."}
        if length <= 1:
            return _count_position_constrained([edge] * length)
        return _count_position_constrained([edge] + [alpha_set] * (length - 2) + [edge])

    if cleaned == "pronounceable":
        v_set = {c for c in alpha_set if c in VOWELS}
        c_set = {c for c in alpha_set if c in CONSONANTS}
        nv, nc = len(v_set), len(c_set)
        if length == 3:
            # Disjoint shapes: CVC and VCV
            return (nc * nv * nc) + (nv * nc * nv)
        if length == 4:
            # Disjoint shapes: CVCV, VCVC, CVCC, CCVC
            return (nc * nv * nc * nv) + (nv * nc * nv * nc) + (nc * nv * nc * nc) + (nc * nc * nv * nc)
        return 0

    if cleaned == "palindrome":
        # For length 3 and 4, `name == name[::-1]` implies `len(set(name)) <= 2`.
        # Thus `name == name[::-1] or len(set(name)) <= 2` is equivalent to `len(set(name)) <= 2` with no '..'.
        has_dot = "." in alpha_set
        non_dot = len(alpha_set) - (1 if has_dot else 0)
        if non_dot <= 0:
            return 0
        # 1 distinct character (cannot be '.' since length >= 3)
        one_char = non_dot
        # 2 distinct non-dot characters: C(non_dot, 2) * (2^length - 2)
        two_non_dot = (non_dot * (non_dot - 1) // 2) * ((1 << length) - 2)
        # 2 distinct characters where one is '.' and one is non-dot:
        # Fib strings of length L over {x, '.'} with no '..' and at least one '.' and one x:
        # For L=3: .x., x.x, .xx, xx. -> 4; for L=4: 8 total Fib - 1 (xxxx) = 7
        dot_patterns = 4 if length == 3 else (7 if length == 4 else 0)
        two_with_dot = (non_dot * dot_patterns) if has_dot else 0
        return one_char + two_non_dot + two_with_dot

    templates = [t.strip() for t in cleaned.split(",") if t.strip()]
    if not templates:
        return _count_position_constrained([alpha_set] * length)

    matching_templates = [t for t in templates if len(t) == length]
    if not matching_templates:
        return 0

    if len(matching_templates) == 1:
        tmpl = matching_templates[0]
        allowed_per_pos = [
            {c for c in alpha_set if _template_char_matches(c, tmpl[pos])}
            for pos in range(length)
        ]
        return _count_position_constrained(allowed_per_pos)

    # Multiple templates of `length`: DP over (bitmask_of_still_matching_templates, prev_was_dot)
    full_mask = (1 << len(matching_templates)) - 1
    dp: dict[tuple[int, bool], int] = {(full_mask, False): 1}
    for pos in range(length):
        # Group characters at `pos` by (template_match_mask, is_dot)
        transitions: dict[tuple[int, bool], int] = {}
        for ch in alpha_set:
            mask = 0
            for idx, tmpl in enumerate(matching_templates):
                if _template_char_matches(ch, tmpl[pos]):
                    mask |= 1 << idx
            is_dot = ch == "."
            transitions[(mask, is_dot)] = transitions.get((mask, is_dot), 0) + 1

        next_dp: dict[tuple[int, bool], int] = {}
        for (cur_mask, prev_dot), ways in dp.items():
            for (ch_mask, is_dot), count in transitions.items():
                if prev_dot and is_dot:
                    continue
                new_mask = cur_mask & ch_mask
                if new_mask == 0:
                    continue
                key = (new_mask, is_dot)
                next_dp[key] = next_dp.get(key, 0) + ways * count
        dp = next_dp
        if not dp:
            return 0

    return sum(ways for (mask, _), ways in dp.items() if mask != 0)


def candidate_count(
    lengths: tuple[int, ...],
    preferences: CandidatePreferences | None = None,
    *,
    cancel_event: threading.Event | None = None,
) -> int:
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelledError("Candidate counting cancelled.")
    validated = validate_lengths(lengths)
    alphabet = resolve_alphabet(preferences)
    pattern = (preferences.pattern if preferences is not None else "any").strip() or "any"
    priority_list, _ = _normalize_priority_entries(validated, preferences)

    cache_key = (validated, alphabet, pattern, tuple(priority_list))
    with _CACHE_LOCK:
        cached = _CANDIDATE_COUNT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    domain_matches = sum(_count_domain_matches_for_length(length, alphabet, pattern) for length in validated)
    alpha_set = set(alphabet)
    extra_priority = 0
    for name in priority_list:
        in_domain = (
            len(name) in validated
            and all(c in alpha_set for c in name)
            and matches_pattern(name, pattern)
        )
        if not in_domain:
            extra_priority += 1

    total = domain_matches + extra_priority
    with _CACHE_LOCK:
        if len(_CANDIDATE_COUNT_CACHE) >= 512:
            _CANDIDATE_COUNT_CACHE.clear()
        _CANDIDATE_COUNT_CACHE[cache_key] = total
    return total


def next_generated_candidate(
    lengths: tuple[int, ...],
    cursor: int,
    preferences: CandidatePreferences | None = None,
    *,
    cancel_event: threading.Event | None = None,
    max_deadline_mono: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, int] | None:
    """Decode a raw cursor directly; never build or replay a full candidate list in memory."""
    if cursor < 0:
        raise ValueError("Invalid scan cursor.")
    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelledError("Candidate generation cancelled.")
    if max_deadline_mono is not None and monotonic() >= max_deadline_mono:
        raise RequestCancelledError("Maximum active duration budget reached.")

    validated = validate_lengths(lengths)

    if _uses_default_exhaustive(preferences):
        offset = 0
        base = len(ALPHABET)
        for length in validated:
            size = base**length
            index = max(0, cursor - offset)
            while index < size:
                value = index
                chars = [ALPHABET[0]] * length
                for position in range(length - 1, -1, -1):
                    value, digit = divmod(value, base)
                    chars[position] = ALPHABET[digit]
                name = "".join(chars)
                index += 1
                if ".." not in name:
                    return name, offset + index
            offset += size
        return None

    assert preferences is not None
    priority_list, _ = _normalize_priority_entries(validated, preferences)
    priority_len = len(priority_list)
    if cursor < priority_len:
        return priority_list[cursor], cursor + 1

    alphabet = resolve_alphabet(preferences)
    pattern = preferences.pattern
    domain_matches_per_len = {
        length: _count_domain_matches_for_length(length, alphabet, pattern)
        for length in validated
    }
    active_lengths = tuple(length for length in validated if domain_matches_per_len[length] > 0)
    if not active_lengths:
        return None

    priority_set = set(priority_list)
    domain = raw_domain_size(active_lengths, preferences)
    gen_cursor = cursor - priority_len
    randomized = preferences.strategy == "randomized"
    seed = preferences.seed

    steps = 0
    while gen_cursor < domain:
        steps += 1
        if (steps & 255) == 0:
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelledError("Candidate generation cancelled.")
            if max_deadline_mono is not None and monotonic() >= max_deadline_mono:
                raise RequestCancelledError("Maximum active duration budget reached.")

        raw_index = permute_index(gen_cursor, domain, seed) if randomized else gen_cursor
        gen_cursor += 1
        name = _decode_raw_index(active_lengths, raw_index, alphabet)
        if name in priority_set:
            continue
        if matches_pattern(name, pattern):
            return name, priority_len + gen_cursor
    return None


def preview_candidates(
    mode: str = "generated",
    lengths: tuple[int, ...] = (3, 4),
    custom_names: list[str] | None = None,
    preferences: CandidatePreferences | None = None,
    limit: int = 20,
    *,
    sample_size: int | None = None,
    cancel_event: threading.Event | None = None,
) -> CandidatePreview:
    """Generate a preview of candidates, local validation rejections, and strategy summary without network calls."""
    if sample_size is not None:
        limit = sample_size
    limit = max(1, min(limit, 100))
    prefs = preferences or CandidatePreferences()
    if mode == "custom":
        raw_names = list(dict.fromkeys(custom_names or []))
        if prefs.strategy == "priority":
            raw_names = rank_candidates_by_preference(raw_names)
        valid_sample: list[str] = []
        invalid_entries: list[tuple[str, str]] = []
        for name in raw_names:
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelledError("Candidate preview cancelled.")
            err = validate_username(name)
            if err:
                invalid_entries.append((name, err))
            elif len(valid_sample) < limit:
                valid_sample.append(name)
        desc = (
            f"Custom list ({len(raw_names):,} unique entries; {len(invalid_entries):,} invalid filtered locally). "
            + (
                "Ordered by preference heuristic (3-char & readable handles first; heuristic only, not availability probability)."
                if prefs.strategy == "priority"
                else "Checked in input order."
            )
        )
        return CandidatePreview(
            total_candidates=len(raw_names),
            sample_candidates=valid_sample,
            invalid_candidates=invalid_entries,
            strategy_description=desc,
        )

    validated = validate_lengths(lengths)
    _, invalid_priority = _normalize_priority_entries(validated, prefs)
    total = candidate_count(validated, prefs, cancel_event=cancel_event)
    sample: list[str] = []
    if total > 0:
        cursor = 0
        while len(sample) < limit:
            nxt = next_generated_candidate(validated, cursor, prefs, cancel_event=cancel_event)
            if nxt is None:
                break
            candidate, cursor = nxt
            if not validate_username(candidate):
                sample.append(candidate)

    strategy_label = (
        f"Randomized without replacement (seed={prefs.seed})"
        if prefs.strategy == "randomized"
        else "Priority list + exhaustive traversal"
        if prefs.priority_names
        else "Exhaustive lexicographical traversal"
    )
    desc = (
        f"{strategy_label} · charset={prefs.charset_mode} · pattern={prefs.pattern or 'any'} · "
        f"{total:,} valid candidates. Preference filters reduce wasted requests but do not predict server availability."
    )
    return CandidatePreview(
        total_candidates=total,
        sample_candidates=sample,
        invalid_candidates=invalid_priority,
        strategy_description=desc,
    )
