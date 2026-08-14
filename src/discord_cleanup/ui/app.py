from __future__ import annotations

import logging
import sys
from typing import Any

import qtawesome as qta
from PyQt5.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QCursor, QFont, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.security.credentials import DEFAULT_CREDENTIAL_STORE
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.ui.components import ToastOverlay
from discord_cleanup.ui.pages.blocked import BlockedPage
from discord_cleanup.ui.pages.friends import FriendsPage
from discord_cleanup.ui.pages.login import LoginPage
from discord_cleanup.ui.pages.logs import LogsPage
from discord_cleanup.ui.pages.notifications import NotificationsPage
from discord_cleanup.ui.pages.servers import ServersPage
from discord_cleanup.ui.theme import (
    ACCENT,
    DANGER,
    TEXT_DIM,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    load_stylesheet,
)
from discord_cleanup.workers.login import LoginWorker

logger = logging.getLogger(__name__)


class StreamInterceptor(QObject):
    """Intercept standard stdout/stderr streams, sanitize credentials, and emit Qt signal."""

    text_written = pyqtSignal(str)

    def __init__(self, original_stream: Any):
        super().__init__()
        self.original_stream = original_stream

    def write(self, text: str) -> None:
        if self.original_stream:
            self.original_stream.write(text)
        if text.strip():
            clean = sanitize_token(text.strip())
            self.text_written.emit(clean)

    def flush(self) -> None:
        if self.original_stream and hasattr(self.original_stream, "flush"):
            self.original_stream.flush()


class MainWindow(QMainWindow):
    """Main Desktop Application Window managing pages, navigation, and background worker lifecycles."""

    def __init__(self):
        super().__init__()
        self.active_workers: list[Any] = []
        self.current_token: str = ""
        self.user_display_name: str = ""
        self.user_handle: str = ""
        self.avatar_pixmap: QPixmap | None = None
        self.login_worker: Any = None

        self.init_window()
        self.init_ui()
        self.setup_interceptors()
        self.check_saved_token()

    def init_window(self) -> None:
        self.setWindowTitle("Discord Account Cleanup Tool")
        self.setMinimumSize(1000, 680)
        self.resize(1120, 740)
        self.setWindowIcon(qta.icon("fa5s.broom", color=ACCENT))

    def init_ui(self) -> None:
        self.setStyleSheet(load_stylesheet())

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Sidebar
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(240)
        s_layout = QVBoxLayout(self.sidebar)
        s_layout.setContentsMargins(16, 24, 16, 20)
        s_layout.setSpacing(6)

        # Brand header
        brand_frame = QFrame()
        brand_frame.setStyleSheet("background: transparent; border: none;")
        bf_layout = QHBoxLayout(brand_frame)
        bf_layout.setContentsMargins(4, 0, 4, 16)
        bf_layout.setSpacing(10)

        logo_box = QFrame()
        logo_box.setFixedSize(36, 36)
        logo_box.setStyleSheet("""
            QFrame {
                background-color: rgba(56, 189, 248, 0.12);
                border: 1px solid rgba(56, 189, 248, 0.25);
                border-radius: 10px;
            }
        """)
        lb_layout = QVBoxLayout(logo_box)
        lb_layout.setContentsMargins(0, 0, 0, 0)
        logo_icon = QLabel()
        logo_icon.setPixmap(qta.icon("fa5s.broom", color=ACCENT).pixmap(QSize(18, 18)))
        logo_icon.setAlignment(Qt.AlignCenter)
        lb_layout.addWidget(logo_icon)
        bf_layout.addWidget(logo_box)

        brand_text_layout = QVBoxLayout()
        brand_text_layout.setSpacing(1)
        brand_title = QLabel("Cleanup Tool")
        brand_title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT_PRIMARY};")
        brand_sub = QLabel("Account Manager")
        brand_sub.setStyleSheet(f"font-size: 11px; color: {TEXT_DIM};")
        brand_text_layout.addWidget(brand_title)
        brand_text_layout.addWidget(brand_sub)
        bf_layout.addLayout(brand_text_layout)
        s_layout.addWidget(brand_frame)

        # Navigation Category
        nav_lbl = QLabel("MANAGEMENT")
        nav_lbl.setStyleSheet(f"""
            font-size: 10px;
            font-weight: 700;
            color: {TEXT_DIM};
            letter-spacing: 0.8px;
            padding: 8px 6px 4px 6px;
        """)
        s_layout.addWidget(nav_lbl)

        # Navigation Buttons Group
        self.nav_btn_group = QButtonGroup(self)
        self.nav_btn_group.setExclusive(True)

        self.btn_servers = self._create_nav_btn(" Servers", "fa5s.server", 0)
        self.btn_friends = self._create_nav_btn(" Friends", "fa5s.user-friends", 1)
        self.btn_blocked = self._create_nav_btn(" Blocked Users", "fa5s.user-slash", 2)
        self.btn_notifs = self._create_nav_btn(" Notifications", "fa5s.bell", 3)
        self.btn_logs = self._create_nav_btn(" Activity Logs", "fa5s.terminal", 4)

        for btn in (self.btn_servers, self.btn_friends, self.btn_blocked, self.btn_notifs, self.btn_logs):
            s_layout.addWidget(btn)

        s_layout.addStretch()

        # User profile badge in sidebar bottom
        self.profile_frame = QFrame()
        self.profile_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(15, 24, 36, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 12px;
                padding: 4px;
            }
        """)
        pf_layout = QHBoxLayout(self.profile_frame)
        pf_layout.setContentsMargins(10, 8, 10, 8)
        pf_layout.setSpacing(10)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(32, 32)
        self.avatar_lbl.setStyleSheet("border-radius: 16px; background-color: rgba(56, 189, 248, 0.1);")
        self.avatar_lbl.setPixmap(qta.icon("fa5s.user", color=ACCENT).pixmap(QSize(18, 18)))
        self.avatar_lbl.setAlignment(Qt.AlignCenter)
        pf_layout.addWidget(self.avatar_lbl)

        p_info = QVBoxLayout()
        p_info.setSpacing(1)
        self.p_name = QLabel("Authenticated")
        self.p_name.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {TEXT_PRIMARY}; border: none;")
        self.p_tag = QLabel("@user")
        self.p_tag.setStyleSheet(f"font-size: 11px; color: {TEXT_DIM}; border: none;")
        p_info.addWidget(self.p_name)
        p_info.addWidget(self.p_tag)
        pf_layout.addLayout(p_info)
        pf_layout.addStretch()

        s_layout.addWidget(self.profile_frame)
        s_layout.addSpacing(8)

        # Logout Button
        self.logout_btn = QPushButton(" Sign Out")
        self.logout_btn.setObjectName("LogoutBtn")
        self.logout_btn.setIcon(qta.icon("fa5s.sign-out-alt", color=DANGER))
        self.logout_btn.setIconSize(QSize(12, 12))
        self.logout_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.logout_btn.clicked.connect(self.logout)
        s_layout.addWidget(self.logout_btn)

        main_layout.addWidget(self.sidebar)

        # 2. Main Stack
        self.main_stack = QStackedWidget()
        main_layout.addWidget(self.main_stack, 1)

        # 3. Initialize Pages
        self.login_page = LoginPage()
        self.login_page.login_requested.connect(self.handle_login_request)
        self.main_stack.addWidget(self.login_page)  # index 0

        self.servers_page = ServersPage(worker_tracker=self.track_worker)
        self.servers_page.log_msg_signal.connect(self.log_message)
        self.servers_page.action_finished.connect(lambda: self.show_toast("Server operation finished", msg_type="info"))
        self.main_stack.addWidget(self.servers_page)  # index 1

        self.friends_page = FriendsPage(worker_tracker=self.track_worker)
        self.friends_page.log_msg_signal.connect(self.log_message)
        self.friends_page.action_finished.connect(lambda: self.show_toast("Friend operation finished", msg_type="info"))
        self.main_stack.addWidget(self.friends_page)  # index 2

        self.blocked_page = BlockedPage(worker_tracker=self.track_worker)
        self.blocked_page.log_msg_signal.connect(self.log_message)
        self.blocked_page.action_finished.connect(lambda: self.show_toast("Unblock operation finished", msg_type="info"))
        self.main_stack.addWidget(self.blocked_page)  # index 3

        self.notifs_page = NotificationsPage(worker_tracker=self.track_worker)
        self.notifs_page.log_msg_signal.connect(self.log_message)
        self.notifs_page.action_finished.connect(lambda: self.show_toast("Notifications marked read", msg_type="success"))
        self.main_stack.addWidget(self.notifs_page)  # index 4

        self.logs_page = LogsPage()
        self.main_stack.addWidget(self.logs_page)  # index 5

        # Toast Overlay
        self.toast = ToastOverlay(self)

        self.show_logged_out_state()

    def _create_nav_btn(self, text: str, icon_name: str, page_index: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("NavBtn")
        btn.setCheckable(True)
        btn.setIcon(qta.icon(icon_name, color=TEXT_SECONDARY))
        btn.setIconSize(QSize(15, 15))
        btn.setCursor(QCursor(Qt.PointingHandCursor))
        btn.clicked.connect(lambda: self.navigate_to_page(page_index))
        self.nav_btn_group.addButton(btn, page_index)
        return btn

    def setup_interceptors(self) -> None:
        self.out_interceptor = StreamInterceptor(sys.stdout)
        self.err_interceptor = StreamInterceptor(sys.stderr)
        self.out_interceptor.text_written.connect(lambda t: self.logs_page.append_log(t, "info"))
        self.err_interceptor.text_written.connect(lambda t: self.logs_page.append_log(t, "error"))
        sys.stdout = self.out_interceptor  # type: ignore[assignment]
        sys.stderr = self.err_interceptor  # type: ignore[assignment]

    def track_worker(self, worker: Any) -> Any:
        """Keep track of running QThread workers to cleanly stop them during teardown."""
        self.active_workers.append(worker)
        worker.finished.connect(lambda: self._untrack_worker(worker))
        return worker

    def _untrack_worker(self, worker: Any) -> None:
        if worker in self.active_workers:
            self.active_workers.remove(worker)

    def cancel_all_workers(self) -> None:
        for worker in list(self.active_workers):
            if hasattr(worker, "cancel"):
                worker.cancel()
            elif hasattr(worker, "quit"):
                worker.quit()

    def check_saved_token(self) -> None:
        token = DEFAULT_CREDENTIAL_STORE.get_token()
        if token:
            self.login_page.set_loading(True)
            self.handle_login_request(token, save=False)

    def handle_login_request(self, token: str, save: bool) -> None:
        self.login_page.set_loading(True)
        if self.login_worker is not None and self.login_worker.isRunning():
            self.login_worker.cancel()

        self.login_worker = self.track_worker(LoginWorker(token, save=save))
        self.login_worker.result_signal.connect(self.on_login_finished)
        self.login_worker.start()

    def on_login_finished(
        self, success: bool, name: str, username: str, token: str, avatar_bytes: bytes, save: bool
    ) -> None:
        self.login_page.set_loading(False)
        if success:
            self.current_token = token
            self.user_display_name = name
            self.user_handle = username
            if save:
                DEFAULT_CREDENTIAL_STORE.save_token(token)

            self.p_name.setText(name)
            self.p_tag.setText(f"@{username}")
            if avatar_bytes:
                pix = QPixmap()
                if pix.loadFromData(avatar_bytes):
                    rounded = QPixmap(32, 32)
                    rounded.fill(Qt.transparent)
                    p = QPainter(rounded)
                    p.setRenderHint(QPainter.Antialiasing)
                    p.setBrush(pix.scaled(32, 32, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
                    p.drawRoundedRect(0, 0, 32, 32, 16, 16)
                    p.end()
                    self.avatar_lbl.setPixmap(rounded)

            self.servers_page.set_token(token)
            self.friends_page.set_token(token)
            self.blocked_page.set_token(token)
            self.notifs_page.set_token(token)

            self.show_logged_in_state()
            self.navigate_to_page(0)  # Go to servers page
            self.show_toast(f"Authenticated as {name}", msg_type="success")
            self.log_message(f"Logged in as {name} (@{username})", "success")
        else:
            self.login_page.set_status(name or "Invalid Token")
            self.log_message(f"Login failed: {name}", "error")

    def show_logged_in_state(self) -> None:
        self.sidebar.show()
        self.btn_servers.setChecked(True)

    def show_logged_out_state(self) -> None:
        self.sidebar.hide()
        self.main_stack.setCurrentIndex(0)

    def navigate_to_page(self, index: int) -> None:
        # Map navigation index (0..4) to main_stack page index (1..5)
        stack_index = index + 1
        self.main_stack.setCurrentIndex(stack_index)
        if index == 0:
            self.servers_page.fetch_data()
        elif index == 1:
            self.friends_page.fetch_data()
        elif index == 2:
            self.blocked_page.fetch_data()

    def logout(self) -> None:
        self.cancel_all_workers()
        DEFAULT_CREDENTIAL_STORE.delete_token()
        self.current_token = ""
        self.user_display_name = ""
        self.user_handle = ""

        self.servers_page.clear()
        self.friends_page.clear()
        self.blocked_page.clear()
        self.notifs_page.clear()
        self.login_page.clear()

        self.avatar_lbl.setPixmap(qta.icon("fa5s.user", color=ACCENT).pixmap(QSize(18, 18)))
        self.p_name.setText("Authenticated")
        self.p_tag.setText("@user")

        self.show_logged_out_state()
        self.show_toast("Logged out cleanly", msg_type="info")
        self.log_message("User signed out. In-memory credentials scrubbed.", "info")

    def log_message(self, text: str, msg_type: str = "info") -> None:
        self.logs_page.append_log(text, msg_type)

    def show_toast(self, message: str, duration: int = 3500, msg_type: str = "info") -> None:
        self.toast.show_message(message, duration, msg_type)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.toast.reposition()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.cancel_all_workers()
        for worker in list(self.active_workers):
            worker.wait(1000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI Variable", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
