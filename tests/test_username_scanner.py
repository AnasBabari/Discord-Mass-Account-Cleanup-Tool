from __future__ import annotations

import csv
import itertools
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import MagicMock

import pytest

from discord_cleanup.api.exceptions import NetworkError
from discord_cleanup.models.domain import User
from discord_cleanup.models.usernames import UsernameCheckResult, UsernameStatus, utc_now
from discord_cleanup.usernames import candidates
from discord_cleanup.usernames.candidates import (
    candidate_count,
    next_generated_candidate,
    parse_custom_names,
    validate_username,
)
from discord_cleanup.usernames.scanner import ScanBusyError, UsernameScanner
from discord_cleanup.usernames.store import UsernameScanStore


@pytest.fixture
def scan_store(tmp_path):
    return UsernameScanStore(tmp_path / "scans.sqlite3")


@pytest.fixture
def scanner(scan_store):
    client = MagicMock(token="secret-token")
    client.verify_token.return_value = User("account-one", "tester")
    client.check_username_availability.side_effect = lambda name, **_: UsernameCheckResult(
        name, UsernameStatus.AVAILABLE, utc_now()
    )
    return UsernameScanner(scan_store, client)


def test_real_search_counts_and_boundaries():
    assert candidate_count((3,)) == 54797
    assert candidate_count((4,)) == 2080880
    assert candidate_count((3, 4)) == 2135677
    assert next_generated_candidate((3, 4), 38**3) == ("aaaa", 38**3 + 1)
    assert next_generated_candidate((3,), 38**3) is None
    assert next_generated_candidate((3, 4), 38**3 + 38**4) is None
    assert not validate_username(".a.")
    assert not validate_username("____")


def test_incremental_generator_matches_exhaustive_reference(monkeypatch):
    monkeypatch.setattr(candidates, "ALPHABET", "ab.")
    expected = [
        "".join(chars)
        for length in (3, 4)
        for chars in itertools.product("ab.", repeat=length)
        if ".." not in "".join(chars)
    ]
