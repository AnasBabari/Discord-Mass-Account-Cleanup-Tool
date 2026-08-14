import pytest
import os
import tempfile
from unittest.mock import patch
from PyQt5.QtWidgets import QMessageBox
from ui.pages.friends import FriendsPage
from ui.pages.servers import ServersPage
from ui.pages.blocked import BlockedPage
from ui.pages.notifications import NotificationsPage
from ui.pages.logs import LogsPage
from gui_app import MainWindow


@pytest.fixture
def app(qtbot):
    main_window = MainWindow()
    qtbot.addWidget(main_window)
    return main_window


def test_friends_page_remove_selected(qtbot):
    page = FriendsPage()
    qtbot.addWidget(page)
    page.show()

    # Setup dummy data
    page.friends_data = [
        {"id": "1", "user": {"username": "User1"}},
        {"id": "2", "user": {"username": "User2"}},
    ]
    page.populate_table()

    # Select both
    page.friends_table.item(0, 0).setCheckState(2)  # Qt.Checked
    page.friends_table.item(1, 0).setCheckState(2)

    # Mock the worker and QMessageBox
    with patch("ui.pages.friends.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.friends.RemoveFriendsWorker"):
            with qtbot.waitSignal(page.action_finished, timeout=1000):
                # Click the remove button
                qtbot.mouseClick(page.remove_friends_btn, 1)  # Qt.LeftButton

                # Verify UI state locked
                assert not page.remove_friends_btn.isEnabled()
                assert page.friends_progress.isVisible()

                # Simulate worker progress
                page.on_remove_progress(1, "[+] REMOVED: User1")
                assert page.friends_progress.value() == 1

                # Simulate worker finished
                page.on_remove_finished(2, 0)

        # Verify UI unlocked
        assert page.remove_friends_btn.isEnabled()
        assert not page.friends_progress.isVisible()


def test_friends_page_select_all_and_filter(qtbot):
    page = FriendsPage()
    qtbot.addWidget(page)
    page.show()

    page.friends_data = [
        {"id": "1", "user": {"username": "Alice", "global_name": "Alice In Wonderland"}},
        {"id": "2", "user": {"username": "Bob", "global_name": "Bob Builder"}}
    ]
    page.populate_table()
    assert page.friends_table.rowCount() == 2

    # Test select all
    page.select_all_friends()
    assert page.friends_table.item(0, 0).checkState() == 2
    assert page.friends_table.item(1, 0).checkState() == 2

    # Filter
    page.filter_friends("alice")
    assert not page.friends_table.isRowHidden(0)
    assert page.friends_table.isRowHidden(1)


def test_servers_page_leave_selected(qtbot):
    page = ServersPage()
    qtbot.addWidget(page)
    page.show()

    page.servers_data = [
        {"id": "1", "name": "Server1", "owner": False},
        {"id": "2", "name": "Server2", "owner": False}
    ]
    page.populate_table()

    page.servers_table.item(0, 0).setCheckState(2)
    page.servers_table.item(1, 0).setCheckState(2)

    with patch("ui.pages.servers.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.servers.LeaveServersWorker"):
            with qtbot.waitSignal(page.action_finished, timeout=1000):
                qtbot.mouseClick(page.leave_servers_btn, 1)

                assert not page.leave_servers_btn.isEnabled()
                assert page.servers_progress.isVisible()

                page.on_leave_progress(1, "[+] LEFT: Server1")
                assert page.servers_progress.value() == 1

                page.on_leave_finished(1, 1)  # 1 success, 1 fail

        assert page.leave_servers_btn.isEnabled()
        assert not page.servers_progress.isVisible()


def test_servers_page_select_all_and_filter(qtbot):
    page = ServersPage()
    qtbot.addWidget(page)
    page.show()

    page.servers_data = [
        {"id": "1", "name": "Gaming Hub", "owner": False},
        {"id": "2", "name": "Music Lounge", "owner": False}
    ]
    page.populate_table()

    page.select_all_servers()
    assert page.servers_table.item(0, 0).checkState() == 2
    assert page.servers_table.item(1, 0).checkState() == 2

    page.filter_servers("gaming")
    assert not page.servers_table.isRowHidden(0)
    assert page.servers_table.isRowHidden(1)


def test_blocked_page_unblock_selected(qtbot):
    page = BlockedPage()
    qtbot.addWidget(page)
    page.show()

    page.blocked_data = [
        {"id": "1", "user": {"username": "Spammer1", "global_name": "Spam"}},
        {"id": "2", "user": {"username": "Spammer2", "global_name": "Spam2"}}
    ]
    page.populate_table()

    page.blocked_table.item(0, 0).setCheckState(2)
    page.blocked_table.item(1, 0).setCheckState(2)

    with patch("ui.pages.blocked.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.blocked.UnblockUsersWorker"):
            with qtbot.waitSignal(page.action_finished, timeout=1000):
                qtbot.mouseClick(page.unblock_btn, 1)

                assert not page.unblock_btn.isEnabled()
                assert page.blocked_progress.isVisible()

                page.on_unblock_progress(1, "[+] UNBLOCKED: Spammer1")
                assert page.blocked_progress.value() == 1

                page.on_unblock_finished(2, 0)

        assert page.unblock_btn.isEnabled()
        assert not page.blocked_progress.isVisible()


def test_notifications_page_read(qtbot):
    page = NotificationsPage()
    qtbot.addWidget(page)
    page.show()
    page.set_token("fake_token")

    with patch("ui.pages.notifications.ReadNotifsWorker"):
        with qtbot.waitSignal(page.action_finished, timeout=1000):
            qtbot.mouseClick(page.read_notifs_btn, 1)

            assert not page.read_notifs_btn.isEnabled()
            assert page.read_notifs_progress.isVisible()

            page.on_chunk_progress(1, 4)
            assert page.read_notifs_progress.value() == 1
            assert page.read_notifs_progress.maximum() == 4

            page.on_read_finished(100, 0, "")

        assert page.read_notifs_btn.isEnabled()
        assert not page.read_notifs_progress.isVisible()


def test_logs_page_and_export(qtbot):
    page = LogsPage()
    qtbot.addWidget(page)
    page.show()

    p1, p2, p3 = "N" * 24, "A" * 6, "B" * 27
    test_token = f"{p1}.{p2}.{p3}"
    page.log_msg(f"Test log line with token {test_token}", "info")
    assert "Test log line" in page.log_textbox.toPlainText()

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        with patch("ui.pages.logs.QFileDialog.getSaveFileName", return_value=(tmp_path, "Text Files (*.txt)")):
            page.export_log()

        with open(tmp_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert test_token not in content
        assert "[REDACTED_TOKEN]" in content
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_friends_page_block_selected(qtbot):
    page = FriendsPage()
    qtbot.addWidget(page)
    page.show()

    page.friends_data = [
        {"id": "1", "user": {"username": "BadFriend"}}
    ]
    page.populate_table()
    page.friends_table.item(0, 0).setCheckState(2)

    with patch("ui.pages.friends.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.friends.BlockUsersWorker"):
            with qtbot.waitSignal(page.action_finished, timeout=1000):
                qtbot.mouseClick(page.block_friends_btn, 1)
                assert not page.block_friends_btn.isEnabled()
                page.on_block_finished(1, 0)

        assert page.block_friends_btn.isEnabled()


def test_friends_page_fetched_handlers(qtbot):
    page = FriendsPage()
    qtbot.addWidget(page)
    # Stale callback when logged out (token is empty)
    page.token = ""
    page.on_friends_fetched([{"id": "1", "user": {"username": "User"}}], "")
    assert page.friends_table.rowCount() == 0

    # Success with items
    page.token = "valid_token"
    page.on_friends_fetched([{"id": "1", "user": {"username": "User"}}], "")
    assert page.friends_table.rowCount() == 1
    # Success empty
    page.on_friends_fetched([], "")
    assert page.empty_label.text() == "No friends found."
    # Error
    page.on_friends_fetched([], "Network timeout")
    assert "Failed to load" in page.empty_label.text()


def test_servers_page_fetched_handlers(qtbot):
    page = ServersPage()
    qtbot.addWidget(page)
    # Stale callback when logged out
    page.token = ""
    page.on_servers_fetched([{"id": "1", "name": "Server", "owner": False}], "")
    assert page.servers_table.rowCount() == 0

    page.token = "valid_token"
    page.on_servers_fetched([{"id": "1", "name": "Server", "owner": False}], "")
    assert page.servers_table.rowCount() == 1
    page.on_servers_fetched([], "")
    assert page.empty_label.text() == "No leavable servers found."
    page.on_servers_fetched([], "Network timeout")
    assert "Failed to load" in page.empty_label.text()


def test_blocked_page_fetched_handlers(qtbot):
    page = BlockedPage()
    qtbot.addWidget(page)
    # Stale callback when logged out
    page.token = ""
    page.on_blocked_fetched([{"id": "1", "user": {"username": "Blocked"}}], "")
    assert page.blocked_table.rowCount() == 0

    page.token = "valid_token"
    page.on_blocked_fetched([{"id": "1", "user": {"username": "Blocked"}}], "")
    assert page.blocked_table.rowCount() == 1
    page.on_blocked_fetched([], "")
    assert page.empty_label.text() == "No blocked users to display."
    page.on_blocked_fetched([], "Network timeout")
    assert "Failed to load" in page.empty_label.text()


def test_logs_page_html_escaping(qtbot):
    """Verify that HTML entities and special characters are safely escaped in log messages."""
    page = LogsPage()
    qtbot.addWidget(page)
    malicious_msg = "<img src=x onerror=alert('xss')> & <Server>"
    page.log_msg(malicious_msg, "info")
    plain = page.log_textbox.toPlainText()
    assert "<img src=x onerror=alert('xss')>" in plain
    assert "<Server>" in plain

    page.set_log_filter("info")
    plain_filtered = page.log_textbox.toPlainText()
    assert "<img src=x onerror=alert('xss')>" in plain_filtered


def test_get_length_str_edge_cases():
    from ui.components import get_length_str
    assert get_length_str(None) == "Unknown"
    assert get_length_str("") == "Unknown"
    assert get_length_str("invalid_snowflake") == "Unknown"
    # Negative / out-of-range snowflake
    assert get_length_str("-12345") == "Unknown"
    assert get_length_str("999999999999999999999999999999") == "Unknown"
    # ISO fallback
    assert get_length_str(None, fallback_timestamp="2020-01-01T00:00:00Z") != "Unknown"
    assert get_length_str(None, fallback_timestamp="invalid-iso") == "Unknown"
