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
    actual, cursor = [], 0
    while item := next_generated_candidate((3, 4), cursor):
        name, cursor = item
        actual.append(name)
    assert actual == expected
    assert len(actual) == candidate_count((3, 4))
    assert len(set(actual)) == len(actual)


def test_custom_normalization_and_secret_redaction():
    assert parse_custom_names(" ABC, @abc\nxyz\tA_B\n") == ["abc", "xyz", "a_b"]
    token = "M" * 24 + "." + "G" * 6 + "." + "a" * 27
    assert token.lower() not in str(parse_custom_names(token))
    assert "redacted" in str(parse_custom_names(token))


def test_resume_only_checks_remaining_candidates(scanner, scan_store):
    scan = scan_store.create_scan("account-one", lengths=(3,))
    first = scanner.run(scan.id, "account-one", max_checks=2)
    assert (first.checked, first.available, first.cursor, first.status) == (2, 2, 2, "paused")
    reopened = UsernameScanStore(scan_store.path)
    second = UsernameScanner(reopened, scanner.client).run(scan.id, "account-one", max_checks=1)
    assert second.checked == 3
    assert [call.args[0] for call in scanner.client.check_username_availability.call_args_list] == ["aaa", "aab", "aac"]
    assert reopened.results(scan.id, "account-one")[1] == 3


def test_failed_candidate_retried_and_error_row_replaced(scanner, scan_store):
    scan = scan_store.create_scan("account-one", mode="custom", names=["abc", "xyz"])
    scanner.client.check_username_availability.side_effect = NetworkError("secret-token disconnected")
    failed = scanner.run(scan.id, "account-one")
    assert (failed.status, failed.cursor, failed.checked) == ("failed", 0, 0)
    assert "secret-token" not in failed.error
    assert scan_store.results(scan.id, "account-one")[1] == 0
    assert scan_store.results(scan.id, "account-one", False)[0][0].status == UsernameStatus.ERROR
    scanner.client.check_username_availability.side_effect = lambda name, **_: UsernameCheckResult(
        name, UsernameStatus.AVAILABLE, utc_now()
    )
    completed = scanner.run(scan.id, "account-one")
    assert (completed.status, completed.checked, completed.available) == ("completed", 2, 2)
    assert scan_store.results(scan.id, "account-one", False)[1] == 2


def test_invalid_custom_names_never_reach_network(scanner, scan_store):
    scan = scan_store.create_scan("account-one", mode="custom", names=["a..", "toolong", "abc"])
    result = scanner.run(scan.id, "account-one")
    assert (result.checked, result.available, result.status) == (3, 1, "completed")
    assert scanner.client.check_username_availability.call_count == 1
    assert [result.status for result in scan_store.results(scan.id, "account-one", False)[0]].count(
        UsernameStatus.INVALID
    ) == 2


def test_cancel_before_and_after_completed_response(scanner, scan_store):
    scan = scan_store.create_scan("account-one", mode="custom", names=["abc", "xyz"])
    event = threading.Event()
    event.set()
    assert scanner.run(scan.id, "account-one", event).checked == 0
    scanner.client.check_username_availability.assert_not_called()
    event.clear()

    def stop_after_response(name, **_):
        event.set()
        return UsernameCheckResult(name, UsernameStatus.AVAILABLE, utc_now())

    scanner.client.check_username_availability.side_effect = stop_after_response
    paused = scanner.run(scan.id, "account-one", event)
    assert (paused.checked, paused.cursor, paused.status) == (1, 1, "paused")


def test_keyboard_interrupt_checkpoints(scanner, scan_store):
    scan = scan_store.create_scan("account-one", lengths=(3,))
    scanner.client.check_username_availability.side_effect = KeyboardInterrupt
    paused = scanner.run(scan.id, "account-one")
    assert paused.status == "paused"
    assert paused.cursor == 0


def test_process_lock_and_crash_recovery(scanner, scan_store):
    scan = scan_store.create_scan("account-one", lengths=(3,))
    scan_store.set_status(scan.id, "account-one", "running")
    with scan_store.lock(scan.id):
        with pytest.raises(ScanBusyError):
            scanner.run(scan.id, "account-one", max_checks=1)
        scan_store.recover_interrupted("account-one")
        assert scan_store.get_scan(scan.id, "account-one").status == "running"
    scan_store.recover_interrupted("account-one")
    assert scan_store.get_scan(scan.id, "account-one").status == "paused"
    assert scanner.run(scan.id, "account-one", max_checks=1).checked == 1


def test_result_and_cursor_rollback_together(scan_store):
    scan = scan_store.create_scan("account-one", lengths=(3,))
    with sqlite3.connect(scan_store.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_checkpoint BEFORE UPDATE OF cursor ON scans BEGIN SELECT RAISE(ABORT,'simulated crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        scan_store.save_result(
            scan.id, "account-one", UsernameCheckResult("aaa", UsernameStatus.AVAILABLE, utc_now()), 1
        )
    assert scan_store.get_scan(scan.id, "account-one").cursor == 0
    assert scan_store.results(scan.id, "account-one", False)[1] == 0


def test_lock_released_after_process_exit(scanner, scan_store):
    scan = scan_store.create_scan("account-one", lengths=(3,))
    lock_path = str(scan_store.path) + f".{scan.id}.lock"
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from filelock import FileLock; lock=FileLock(sys.argv[1]); lock.acquire(); print('locked', flush=True); sys.stdin.read()",
            lock_path,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout.readline().strip() == "locked"
        with pytest.raises(ScanBusyError):
            scanner.run(scan.id, "account-one", max_checks=1)
    finally:
        helper.terminate()
        helper.communicate(timeout=5)
    assert scanner.run(scan.id, "account-one", max_checks=1).checked == 1


def test_accounts_versions_and_export(scanner, scan_store, tmp_path):
    scan = scan_store.create_scan("account-one", mode="custom", names=["abc", "=x", "xyz"])
    scanner.run(scan.id, "account-one")
    assert scan_store.list_scans("account-two") == []
    with pytest.raises(ValueError, match="this account"):
        scanner.run(scan.id, "account-two")
    with pytest.raises(ValueError):
        scan_store.export_csv(scan.id, "account-two", tmp_path / "other.csv")
    output = tmp_path / "names.csv"
    scan_store.export_csv(scan.id, "account-one", output)
    with output.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 3
    assert rows[0]["username"] == "'=x"
    assert all(row["checked_at_utc"] for row in rows)
    assert "secret-token" not in output.read_text()
    with pytest.raises(ValueError, match="different"):
        scan_store.export_csv(scan.id, "account-one", scan_store.path)
    with sqlite3.connect(scan_store.path) as connection:
        connection.execute("UPDATE scans SET generator_version=999 WHERE id=?", (scan.id,))
    with pytest.raises(ValueError, match="generator version"):
        scanner.run(scan.id, "account-one")


def test_completed_scan_not_checked_again(scanner, scan_store):
    scan = scan_store.create_scan("account-one", mode="custom", names=["abc"])
    assert scanner.run(scan.id, "account-one").status == "completed"
    assert scanner.run(scan.id, "account-one").checked == 1
    assert scanner.client.check_username_availability.call_count == 1


def test_identify_requires_real_account_id(scanner):
    assert scanner.identify() == "account-one"
    scanner.client.verify_token.return_value = User("", "tester")
    with pytest.raises(ValueError, match="account ID"):
        scanner.identify()


def test_progress_failure_does_not_corrupt_results(scanner, scan_store):
    scan = scan_store.create_scan("account-one", mode="custom", names=["abc"])

    def broken_ui(progress):
        raise RuntimeError("The consumer failed")

    result = scanner.run(scan.id, "account-one", on_progress=broken_ui)
    assert (result.status, result.checked, result.available) == ("completed", 1, 1)
    assert scan_store.results(scan.id, "account-one")[0][0].status == UsernameStatus.AVAILABLE
