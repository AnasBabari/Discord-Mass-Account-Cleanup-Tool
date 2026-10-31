from __future__ import annotations

import threading
import time
from importlib import import_module
from unittest.mock import MagicMock

import pytest
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QApplication

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.cli import usernames as cli
from discord_cleanup.models.domain import User
from discord_cleanup.models.usernames import PUBLIC_USERNAME_SCOPE, UsernameCheckResult, UsernameStatus, utc_now
from discord_cleanup.ui import app as app_module
from discord_cleanup.ui.pages import usernames as page_module
from discord_cleanup.ui.pages.usernames import UsernamesPage
from discord_cleanup.usernames.public_client import PublicUsernameClient
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.usernames import UsernameScanWorker
from tests.test_api_client import MockResponse


@pytest.fixture
def fake_client():
    client = MagicMock(token="secret-token")
    client.verify_token.return_value = User("account-one", "tester")
    client.check_username_availability.side_effect = lambda name, **_: UsernameCheckResult(
        name, UsernameStatus.AVAILABLE, utc_now()
    )
    return client


def test_worker_completes_and_scrubs(qtbot, tmp_path, fake_client):
    worker = UsernameScanWorker(
        "secret-token", mode="custom", custom_text="ABC, xyz", store_path=tmp_path / "scans.db", client=fake_client
    )
    completed = []
    worker.completed_signal.connect(completed.append)
    with qtbot.waitSignal(worker.finished, timeout=3000):
        worker.start()
    assert completed[0].status == "completed"
    assert completed[0].available == 2
    assert worker.token == ""
    assert worker.custom_text == ""
    fake_client.clear_token.assert_called_once()


def test_worker_rejects_other_accounts_scan(qtbot, tmp_path, fake_client):
    store = UsernameScanStore(tmp_path / "scans.db")
    scan = store.create_scan("account-two", lengths=(3,))
    worker = UsernameScanWorker(
        "secret-token", mode="resume", scan_id=scan.id, store_path=store.path, client=fake_client
    )
    errors = []
    worker.error_signal.connect(errors.append)
    with qtbot.waitSignal(worker.finished, timeout=3000):
        worker.start()
    assert "this account" in errors[0]
    fake_client.check_username_availability.assert_not_called()


def test_page_custom_results_copy_export_and_history(qtbot, monkeypatch, tmp_path, fake_client):
    monkeypatch.setattr(
        page_module,
        "UsernameScanWorker",
        lambda *args, **kwargs: UsernameScanWorker(*args, **kwargs, client=fake_client),
    )
    page = UsernamesPage(store_path=tmp_path / "scans.db")
    qtbot.addWidget(page)
    page.set_token("secret-token")
    assert page.start_btn.isEnabled()
    page.mode_input.setCurrentIndex(1)
    page.custom_input.setPlainText("ABC, xyz a..")
    page.start_scan()
    qtbot.waitUntil(lambda: page.worker is not None and not page.worker.isRunning(), timeout=3000)
    qtbot.waitUntil(lambda: page.start_btn.isEnabled(), timeout=3000)
    assert page.scan.checked == 3
    assert page.scan.available == 2
    assert page.table.rowCount() == 2
    assert not page.resume_btn.isEnabled()
    page.copy_available()
    qtbot.waitUntil(lambda: page.results_worker is not None and not page.results_worker.isRunning(), timeout=3000)
    qtbot.waitUntil(lambda: page.copy_btn.isEnabled(), timeout=3000)
    assert QApplication.clipboard().text().splitlines() == ["abc", "xyz"]
    page.filter_input.setCurrentIndex(1)
    assert page.table.rowCount() == 3
    output = tmp_path / "export.csv"
    monkeypatch.setattr(page_module.QFileDialog, "getSaveFileName", lambda *args: (str(output), ""))
    page.export_results()
    qtbot.waitUntil(output.exists, timeout=3000)
    qtbot.waitUntil(lambda: not page.results_worker.isRunning(), timeout=3000)
    assert "invalid" in output.read_text()
    assert page.history_input.count() == 1
    page.clear()
    assert page.start_btn.isEnabled()  # Public checks remain accessible without a token.
    assert page.table.rowCount() == 0


def test_page_pagination_uses_only_one_page(qtbot, tmp_path):
    page = UsernamesPage(store_path=tmp_path / "scans.db")
    qtbot.addWidget(page)
    page.set_token("secret-token")
    page.account_id = "account-one"
    scan = page.store().create_scan("account-one", lengths=(3,))
    for index in range(205):
        # Arbitrary fixture rows are enough to exercise disk-backed pagination.
        page.store().save_result(
            scan.id, "account-one", UsernameCheckResult(f"n{index:03}", UsernameStatus.AVAILABLE, utc_now()), index + 1
        )
    page.scan = page.store().get_scan(scan.id, "account-one")
    page.show_scan()
    assert page.table.rowCount() == 100
    assert page.next_btn.isEnabled()
    page.change_page(100)
    assert page.table.rowCount() == 100
    page.change_page(100)
    assert page.table.rowCount() == 5
    assert not page.next_btn.isEnabled()


def test_logout_discards_late_signals(qtbot, monkeypatch, tmp_path, fake_client):
    entered, release = threading.Event(), threading.Event()

    def blocked_check(name, **kwargs):
        entered.set()
        release.wait(3)
        return UsernameCheckResult(name, UsernameStatus.AVAILABLE, utc_now())

    fake_client.check_username_availability.side_effect = blocked_check
    monkeypatch.setattr(
        page_module,
        "UsernameScanWorker",
        lambda *args, **kwargs: UsernameScanWorker(*args, **kwargs, client=fake_client),
    )
    page = UsernamesPage(store_path=tmp_path / "scans.db")
    qtbot.addWidget(page)
    page.set_token("secret-token")
    page.mode_input.setCurrentIndex(1)
    page.custom_input.setPlainText("abc xyz")
    page.start_scan()
    worker = page.worker
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        page.clear()
        page.set_token("another-account-token")
        release.set()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=3000)
        qtbot.waitUntil(lambda: not page._retired_workers, timeout=3000)
        assert page.scan is None
        assert page.table.rowCount() == 0
        assert page.account_id == ""
        assert worker.token == ""
    finally:
        release.set()
        worker.wait(4000)


def test_window_close_defers_for_active_scanner(qtbot, monkeypatch, tmp_path, fake_client):
    entered, release = threading.Event(), threading.Event()

    def blocked_check(name, **kwargs):
        entered.set()
        release.wait(3)
        return UsernameCheckResult(name, UsernameStatus.AVAILABLE, utc_now())

    fake_client.check_username_availability.side_effect = blocked_check
    monkeypatch.setattr(
        page_module,
        "UsernameScanWorker",
        lambda *args, **kwargs: UsernameScanWorker(*args, **kwargs, client=fake_client),
    )
    monkeypatch.setattr(app_module.MainWindow, "check_saved_token", lambda self: None)
    monkeypatch.setattr(app_module.MainWindow, "setup_interceptors", lambda self: None)
    window = app_module.MainWindow()
    qtbot.addWidget(window)
    page = window.usernames_page
    page.store_path = tmp_path / "scans.db"
    page.set_token("secret-token")
    page.mode_input.setCurrentIndex(1)
    page.custom_input.setPlainText("abc xyz")
    page.start_scan()
    worker = page.worker
    try:
        qtbot.waitUntil(entered.is_set, timeout=3000)
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        release.set()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=3000)
        qtbot.waitUntil(lambda: not window.active_workers, timeout=3000)
        window.navigate_to_page(5)
        assert window.main_stack.currentWidget() is window.logs_page
    finally:
        release.set()
        worker.wait(4000)


def test_cli_custom_list_results_and_export(monkeypatch, tmp_path, fake_client, capsys):
    output = tmp_path / "export.csv"
    answers = iter(["2", "ABC, xyz a..", "", "4", "1", "5", "1", str(output), "q"])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    monkeypatch.setattr(cli, "DiscordApiClient", lambda *args, **kwargs: fake_client)
    transport = MagicMock()
    monkeypatch.setattr(cli, "RequestsTransport", lambda: transport)
    cli.run_username_checker(DiscordApiClient("secret-token"), tmp_path / "scans.db")
    text = capsys.readouterr().out
    assert "AVAILABLE: abc" in text
    assert "2 available" in text
    assert "abc |" in text
    assert "secret-token" not in text
    assert "invalid" in output.read_text()
    fake_client.clear_token.assert_called_once()
    transport.close.assert_called_once()


def test_cli_interrupt_and_resume(monkeypatch, tmp_path, fake_client, capsys):
    store = UsernameScanStore(tmp_path / "scans.db")
    scan = store.create_scan("account-one", mode="custom", names=["abc"])
    fake_client.check_username_availability.side_effect = KeyboardInterrupt
    answers = iter(["3", "1", "q"])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    monkeypatch.setattr(cli, "DiscordApiClient", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr(cli, "RequestsTransport", MagicMock)
    cli.run_username_checker(DiscordApiClient("secret-token"), store.path)
    assert "Scan paused" in capsys.readouterr().out
    assert store.get_scan(scan.id, "account-one").cursor == 0


def test_public_worker_never_verifies_or_sends_a_token(qtbot, tmp_path, fake_client):
    worker = UsernameScanWorker(
        "",
        mode="custom",
        custom_text="abc",
        store_path=tmp_path / "public.db",
        client=fake_client,
        access="public",
    )
    completed = []
    worker.completed_signal.connect(completed.append)
    with qtbot.waitSignal(worker.finished, timeout=3000):
        worker.start()
    assert completed[0].account_id == PUBLIC_USERNAME_SCOPE
    assert completed[0].checked == 1
    fake_client.verify_token.assert_not_called()
    assert worker.token == ""


def test_public_mode_available_from_login_page(qtbot, monkeypatch):
    monkeypatch.setattr(app_module.MainWindow, "check_saved_token", lambda self: None)
    monkeypatch.setattr(app_module.MainWindow, "setup_interceptors", lambda self: None)
    delete_token = MagicMock()
    monkeypatch.setattr(app_module.DEFAULT_CREDENTIAL_STORE, "delete_token", delete_token)
    window = app_module.MainWindow()
    qtbot.addWidget(window)
    window.login_page.public_usernames_btn.click()
    assert window.public_username_mode
    assert window.current_token == ""
    assert window.main_stack.currentWidget() is window.usernames_page
    assert window.usernames_page.start_btn.isEnabled()
    assert not window.btn_servers.isEnabled()
    window.navigate_to_page(0)
    assert window.main_stack.currentWidget() is window.usernames_page
    window.logout()
    assert window.main_stack.currentWidget() is window.login_page
    delete_token.assert_not_called()


def test_public_queue_pause_and_reload_keeps_cooldown(qtbot, monkeypatch, tmp_path):
    store = UsernameScanStore(tmp_path / "public.db")
    transport = MagicMock()
    client = PublicUsernameClient(store, transport=transport)
    deadline = time.time() + 2009
    store.save_cooldown(client.url, deadline, "Rate limited")
    monkeypatch.setattr(
        page_module,
        "UsernameScanWorker",
        lambda *args, **kwargs: UsernameScanWorker(*args, **kwargs, client=client),
    )
    page = UsernamesPage(store_path=store.path)
    qtbot.addWidget(page)
    page.mode_input.setCurrentIndex(1)
    page.custom_input.setPlainText("abc")
    page.start_scan()
    worker = page.worker
    try:
        qtbot.waitUntil(lambda: "Rate limited" in page.status_label.text(), timeout=3000)
        assert "saved across restarts" in page.cooldown_label.text()
        page.pause_scan()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=3000)
        qtbot.waitUntil(lambda: page.start_btn.isEnabled(), timeout=3000)
        assert page.scan.status == "paused"
        assert page.scan.checked == 0
        assert store.cooldown(client.url)[0] == deadline
        transport.request.assert_not_called()
        page.clear()
        page.load_scans()
        qtbot.waitUntil(lambda: not page.worker.isRunning(), timeout=3000)
        qtbot.waitUntil(lambda: page.history_input.count() == 1, timeout=3000)
        assert page.resume_btn.isEnabled()
        assert "cooldown" in page.cooldown_label.text()
        transport.request.assert_not_called()
    finally:
        worker.cancel()
        worker.wait(4000)
        if page.worker is not None:
            page.worker.cancel()
            page.worker.wait(4000)


def test_cli_public_mode_uses_direct_results_without_login(monkeypatch, tmp_path, capsys):
    transport = MagicMock()
    transport.request.return_value = MockResponse(json_data={"taken": False})
    monkeypatch.setattr(cli, "RequestsTransport", lambda: transport)
    answers = iter(["2", "abc a..", "", "4", "1", "q"])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))
    cli.run_username_checker(store_path=tmp_path / "public.db")
    output = capsys.readouterr().out
    assert "no login required" in output
    assert "AVAILABLE: abc" in output
    assert transport.request.call_count == 1
    assert "headers" not in transport.request.call_args.kwargs


def test_public_cli_entry_does_not_read_credentials(monkeypatch):
    from discord_cleanup import __main__ as module_main

    cli_main = import_module("discord_cleanup.cli.main")

    calls = []
    monkeypatch.setattr("sys.argv", ["discord-cleanup", "--usernames"])
    monkeypatch.setattr(
        cli_main.DEFAULT_CREDENTIAL_STORE, "get_token", lambda: pytest.fail("Public mode read a credential")
    )
    monkeypatch.setattr(cli_main, "run_username_checker", lambda: calls.append("entry"))
    monkeypatch.setattr(cli, "run_username_checker", lambda: calls.append("module"))
    cli_main.main()
    module_main.main()
    assert calls == ["entry", "module"]


def test_public_and_existing_account_scans_stay_separate(qtbot, monkeypatch, tmp_path, fake_client):
    path = tmp_path / "mixed.db"
    store = UsernameScanStore(path)
    private = store.create_scan("account-one", mode="custom", names=["abc"])
