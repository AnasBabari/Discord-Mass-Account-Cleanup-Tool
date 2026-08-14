from unittest.mock import MagicMock, patch
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLineEdit

from discord_cleanup.ui.pages.blocked import BlockedPage
from discord_cleanup.ui.pages.friends import FriendsPage
from discord_cleanup.ui.pages.login import LoginPage
from discord_cleanup.ui.pages.logs import LogsPage
from discord_cleanup.ui.pages.notifications import NotificationsPage
from discord_cleanup.ui.pages.servers import ServersPage


class TestLoginPage:
    def test_toggle_token_visibility(self, qapp, qtbot):
        page = LoginPage()
        qtbot.addWidget(page)

        assert page.token_entry.echoMode() == QLineEdit.Password
        page.toggle_token_visibility()
        assert page.token_entry.echoMode() == QLineEdit.Normal
        page.toggle_token_visibility()
        assert page.token_entry.echoMode() == QLineEdit.Password

    def test_request_login_signal(self, qapp, qtbot):
        page = LoginPage()
        qtbot.addWidget(page)

        page.token_entry.setText("my_test_token")
        with qtbot.waitSignal(page.login_requested, timeout=1000) as blocker:
            page.request_login()

        token, save = blocker.args
        assert token == "my_test_token"
        assert save is True


class TestServersPage:
    def test_populate_and_filter(self, qapp, qtbot):
        page = ServersPage()
        qtbot.addWidget(page)
        page.set_token("test_token")

        data = [
            {"id": "1", "name": "Gaming Guild", "owner": False},
            {"id": "2", "name": "Python Hub", "owner": False},
            {"id": "3", "name": "My Personal Server", "owner": True},
        ]
        page.on_servers_fetched(data, "")

        assert page.servers_table.rowCount() == 2  # owned server excluded
        assert page.stat_total.val_lbl.text() == "3"
        assert page.stat_leavable.val_lbl.text() == "2"
        assert page.stat_owned.val_lbl.text() == "1"

        page.filter_servers("Gaming")
        assert not page.servers_table.isRowHidden(0)
        assert page.servers_table.isRowHidden(1)

    def test_select_all_and_deselect(self, qapp, qtbot):
        page = ServersPage()
        qtbot.addWidget(page)
        page.set_token("test_token")
        data = [{"id": "1", "name": "Server 1", "owner": False}]
        page.on_servers_fetched(data, "")

        page.select_all_servers()
        assert page.servers_table.item(0, 0).checkState() == Qt.Checked

        page.deselect_all_servers()
        assert page.servers_table.item(0, 0).checkState() == Qt.Unchecked


class TestFriendsPage:
    def test_populate_and_select(self, qapp, qtbot):
        page = FriendsPage()
        qtbot.addWidget(page)
        page.set_token("test_token")
        data = [
            {"id": "f1", "user": {"id": "f1", "username": "Alice", "global_name": "Alice in Wonderland"}},
            {"id": "f2", "user": {"id": "f2", "username": "Bob", "global_name": None}},
        ]
        page.on_friends_fetched(data, "")

        assert page.friends_table.rowCount() == 2
        assert "Alice" in page.friends_table.item(0, 1).text()

        page.select_all_friends()
        selected = page._get_selected_friends()
        assert len(selected) == 2


class TestBlockedPage:
    def test_populate_and_select(self, qapp, qtbot):
        page = BlockedPage()
        qtbot.addWidget(page)
        page.set_token("test_token")
        data = [
            {"id": "b1", "user": {"id": "b1", "username": "Spammer"}},
        ]
        page.on_blocked_fetched(data, "")

        assert page.blocked_table.rowCount() == 1
        page.select_all_blocked()
        assert page.unblock_btn.isEnabled()


class TestNotificationsPage:
    def test_start_clear_notifications(self, qapp, qtbot):
        page = NotificationsPage()
        qtbot.addWidget(page)
        page.set_token("test_token")

        with patch("discord_cleanup.ui.pages.notifications.ReadNotifsWorker") as mock_worker_cls:
            mock_instance = MagicMock()
            mock_worker_cls.return_value = mock_instance
            page.start_clear_notifications()
            assert not page.clear_notifs_btn.isEnabled()


class TestLogsPage:
    def test_append_filter_and_search(self, qapp, qtbot):
        page = LogsPage()
        qtbot.addWidget(page)

        page.append_log("Application initialized", "info")
        page.append_log("Warning rate limit high", "warning")
        page.append_log("Error in connection", "error")

        assert len(page.all_logs) == 3

        page.on_filter_changed("Error")
        assert "Error in connection" in page.log_text.toPlainText()
        assert "Application initialized" not in page.log_text.toPlainText()

        page.on_filter_changed("All")
        page.on_search_changed("rate limit")
        assert "Warning rate limit high" in page.log_text.toPlainText()
        assert "Error in connection" not in page.log_text.toPlainText()

    def test_clear_logs(self, qapp, qtbot):
        page = LogsPage()
        qtbot.addWidget(page)
        page.append_log("Log entry", "info")
        page.clear_logs()
        assert len(page.all_logs) == 0
        assert page.log_text.toPlainText() == ""
