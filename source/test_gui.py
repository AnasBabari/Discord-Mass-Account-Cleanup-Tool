import sys
import pytest
from unittest.mock import patch, MagicMock
from PyQt5.QtWidgets import QLineEdit
from gui_app import MainWindow
from ui.pages.login import LoginPage


@pytest.fixture
def app(qtbot):
    main_window = MainWindow()
    qtbot.addWidget(main_window)
    return main_window


def test_main_window_initialization(app):
    assert app.windowTitle() == "Discord Mass Account Cleanup Tool"
    assert app.pages.count() == 6


def test_page_switching(app, qtbot):
    assert app.pages.currentIndex() == 0

    app.switch_page("servers")
    assert app.pages.currentIndex() == 1

    app.switch_page("friends")
    assert app.pages.currentIndex() == 2

    app.switch_page("blocked")
    assert app.pages.currentIndex() == 3

    app.switch_page("notifications")
    assert app.pages.currentIndex() == 4

    app.switch_page("logs")
    assert app.pages.currentIndex() == 5


def test_login_page(qtbot):
    page = LoginPage()
    qtbot.addWidget(page)

    assert page.token_entry is not None
    assert page.login_btn is not None

    qtbot.keyClicks(page.token_entry, "fake_token")
    assert page.token_entry.text() == "fake_token"

    # Toggle visibility
    assert page.token_entry.echoMode() == QLineEdit.Password
    page.toggle_token_visibility()
    assert page.token_entry.echoMode() == QLineEdit.Normal
    page.toggle_token_visibility()
    assert page.token_entry.echoMode() == QLineEdit.Password

    # Empty token request
    page.token_entry.clear()
    page.request_login()
    assert page.login_status.text() == "Please enter your token"


def test_main_window_login_flow(app, qtbot):
    with patch("ui.pages.servers.ServersPage.fetch_data"), \
         patch("ui.pages.friends.FriendsPage.fetch_data"), \
         patch("ui.pages.blocked.BlockedPage.fetch_data"), \
         patch("keyring.set_password"):

        # Test successful login result
        app.on_login_result(True, "DiscordUser", "discorduser", "fake_token", b"", True)
        assert app.token == "fake_token"
        assert app.account_name == "DiscordUser"
        assert app.pages.currentIndex() == 1  # Switched to servers page

    # Test login failure
    with patch("keyring.delete_password"):
        app.on_login_result(False, "INVALID TOKEN", "", "", b"", False)
        assert app.login_page.login_status.text() != ""


def test_main_window_cancel_all_workers(app):
    dummy_worker = MagicMock()
    dummy_worker.isRunning.return_value = False
    app.track_worker(dummy_worker)
    app.cancel_all_workers()
    dummy_worker.cancel.assert_called_once()


def test_toast_overlay_flow(app, qtbot):
    app.show()
    app.toast.show_message("Test message", duration=100)
    assert not app.toast.isHidden()
    app.toast.reposition()
    app.toast._fade_out()


def test_logout_does_not_crash(app):
    app.logout()
    assert app.windowTitle() == "Discord Mass Account Cleanup Tool"
    assert app.token == ""


def test_close_restores_streams(app):
    original_stdout = app._original_stdout
    original_stderr = app._original_stderr
    app.close()
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
