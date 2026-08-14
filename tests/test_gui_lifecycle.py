from unittest.mock import MagicMock, patch

from PyQt5.QtGui import QCloseEvent

from discord_cleanup.ui.app import MainWindow, StreamInterceptor


class TestGuiLifecycle:
    def test_main_window_init(self, qapp, qtbot):
        with patch("discord_cleanup.security.credentials.DEFAULT_CREDENTIAL_STORE.get_token", return_value=None):
            window = MainWindow()
            qtbot.addWidget(window)
            assert window.windowTitle() == "Discord Account Cleanup Tool"
            assert window.sidebar.isHidden()
            assert window.main_stack.currentIndex() == 0

    def test_stream_interceptor_redaction(self, qapp, qtbot):
        mock_stream = MagicMock()
        interceptor = StreamInterceptor(mock_stream)

        token = "M" * 24 + "." + "G" * 6 + "." + "a" * 27
        with qtbot.waitSignal(interceptor.text_written, timeout=1000) as blocker:
            interceptor.write(f"Connecting with token: {token}")

        emitted_text = blocker.args[0]
        assert "[REDACTED_TOKEN]" in emitted_text
        assert token not in emitted_text

    def test_login_and_logout_lifecycle(self, qapp, qtbot):
        with patch("discord_cleanup.security.credentials.DEFAULT_CREDENTIAL_STORE.get_token", return_value=None):
            window = MainWindow()
            qtbot.addWidget(window)

            # Mock successful login
            window.on_login_finished(
                success=True,
                name="Test User",
                username="testuser",
                token="valid_token",
                avatar_bytes=b"",
                save=False,
            )

            assert not window.sidebar.isHidden()
            assert window.current_token == "valid_token"
            assert window.p_name.text() == "Test User"

            # Perform logout
            window.logout()
            assert window.sidebar.isHidden()
            assert window.current_token == ""
            assert window.main_stack.currentIndex() == 0

    def test_close_event_cancels_workers(self, qapp, qtbot):
        with patch("discord_cleanup.security.credentials.DEFAULT_CREDENTIAL_STORE.get_token", return_value=None):
            window = MainWindow()
            qtbot.addWidget(window)

            mock_worker = MagicMock()
            window.track_worker(mock_worker)
            assert mock_worker in window.active_workers

            event = QCloseEvent()
            window.closeEvent(event)
            mock_worker.cancel.assert_called_once()
