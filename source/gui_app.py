# pyrefly: ignore[missing-import]
import sys
import keyring
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QStackedWidget, QFrame
from PyQt5.QtCore import Qt, QTimer, QSize, QObject, pyqtSignal
from PyQt5.QtGui import QFont, QCursor, QFontMetrics
import qtawesome as qta
from ui.theme import *
from ui.components import ToastOverlay
from ui.pages.login import LoginPage
from ui.pages.servers import ServersPage
from ui.pages.friends import FriendsPage
from ui.pages.blocked import BlockedPage
from ui.pages.notifications import NotificationsPage
from ui.pages.logs import LogsPage
from workers import LoginWorker

SERVICE_ID = "DiscordMassCleanupTool"
KEY_ID = "user_token"

class StreamInterceptor(QObject):
    log_signal = pyqtSignal(str, str)
    def __init__(self, msg_type="debug"):
        super().__init__()
        self.msg_type = msg_type
    def write(self, msg):
        if msg.strip():
            self.log_signal.emit(msg.strip(), self.msg_type)
    def flush(self): pass

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Discord Mass Account Cleanup Tool")
        self.resize(1100, 750)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(load_stylesheet())
        
        self.token = ""
        self.account_name = ""
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # ── Sidebar ─────────────────────────────────────────────────────────
        self.sidebar = QWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(210)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(16, 24, 16, 20)
        sidebar_layout.setSpacing(4)
        
        brand_frame = QFrame()
        brand_frame.setStyleSheet(f"""
            QFrame {{
                background: transparent;
                border-bottom: 1px solid {BORDER};
                padding-bottom: 16px;
                margin-bottom: 8px;
            }}
        """)
        brand_layout = QVBoxLayout(brand_frame)
        brand_layout.setContentsMargins(4, 0, 4, 16)
        brand_layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        app_icon = QLabel()
        app_icon.setPixmap(qta.icon('mdi.broom', color=ACCENT).pixmap(QSize(22, 22)))
        app_icon.setFixedSize(28, 28)
        app_icon.setAlignment(Qt.AlignCenter)
        brand_row.addWidget(app_icon)

        app_title = QLabel("Cleanup Tool")
        app_title.setStyleSheet(f"""
            color: {TEXT_PRIMARY};
            font-size: 16px;
            font-weight: 700;
            letter-spacing: -0.3px;
        """)
        brand_row.addWidget(app_title)
        brand_row.addStretch()
        brand_layout.addLayout(brand_row)

        app_desc = QLabel()
        app_desc.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px; padding-left: 38px;")
        elided_desc = QFontMetrics(app_desc.font()).elidedText("Discord Account Manager", Qt.ElideRight, 130)
        app_desc.setText(elided_desc)
        brand_layout.addWidget(app_desc)

        sidebar_layout.addWidget(brand_frame)
        
        nav_label = QLabel("• NAVIGATION")
        nav_label.setStyleSheet(f"""
            color: {TEXT_DIM};
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.2px;
            padding: 12px 4px 6px 4px;
        """)
        sidebar_layout.addWidget(nav_label)
        
        self.nav_btns = {}
        nav_items = [
            ("Servers",       'fa5s.server',       "servers"),
            ("Friends",       'mdi.account-multiple',  "friends"),
            ("Blocked",       'fa5s.user-slash',       "blocked"),
            ("Notifications", 'fa5s.bell',          "notifications"),
            ("Terminal",      'fa5s.terminal',      "logs")
        ]
        
        for text, icon_name, ref in nav_items:
            btn = QPushButton(f"  {text}")
            btn.setIcon(qta.icon(icon_name, color=TEXT_SECONDARY))
            btn.setIconSize(QSize(16, 16))
            btn.setCheckable(True)
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.clicked.connect(lambda checked, r=ref, b=btn: self._on_nav_click(r, b))
            sidebar_layout.addWidget(btn)
            self.nav_btns[ref] = btn
            
        sidebar_layout.addStretch()

        self.account_frame = QFrame()
        self.account_frame.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(56, 189, 248, 0.05);
                border: 1px solid rgba(56, 189, 248, 0.1);
                border-radius: 10px;
            }}
        """)
        account_layout = QVBoxLayout(self.account_frame)
        account_layout.setContentsMargins(12, 10, 12, 10)
        account_layout.setSpacing(4)

        account_header = QHBoxLayout()
        account_header.setSpacing(8)
        self.account_avatar = QLabel("?")
        self.account_avatar.setFixedSize(24, 24)
        self.account_avatar.setAlignment(Qt.AlignCenter)
        self.account_avatar.setStyleSheet(f"""
            background-color: {ACCENT};
            color: {BG_DARKEST};
            border-radius: 12px;
            font-weight: 700;
            font-size: 11px;
        """)
        account_header.addWidget(self.account_avatar)
        
        acct_lbl = QLabel("Account")
        acct_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px; font-weight: 600; letter-spacing: 0.5px;")
        account_header.addWidget(acct_lbl)
        account_header.addStretch()
        account_layout.addLayout(account_header)

        self.account_name_label = QLabel("")
        self.account_name_label.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 14px; font-weight: 600; padding-left: 32px;")
        self.account_name_label.setWordWrap(True)
        account_layout.addWidget(self.account_name_label)

        sidebar_layout.addWidget(self.account_frame)
        self.account_frame.hide()

        sidebar_layout.addSpacing(8)
        
        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background-color: rgba(239, 68, 68, 0.15); margin: 0 8px;")
        sidebar_layout.addWidget(separator)
        
        self.logout_btn = QPushButton("  Sign Out")
        self.logout_btn.setObjectName("LogoutBtn")
        self.logout_btn.setIcon(qta.icon('fa5s.sign-out-alt', color=DANGER))
        self.logout_btn.setIconSize(QSize(14, 14))
        self.logout_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.logout_btn.clicked.connect(self.logout)
        sidebar_layout.addWidget(self.logout_btn)
        
        main_layout.addWidget(self.sidebar)
        
        # ── Content Area ────────────────────────────────────────────────────
        self.pages = QStackedWidget()
        main_layout.addWidget(self.pages)
        
        self.login_page = LoginPage()
        self.login_page.login_requested.connect(self.start_login)
        self.pages.addWidget(self.login_page)

        self.servers_page = ServersPage()
        self.servers_page.log_msg_signal.connect(self.log_msg)
        self.servers_page.action_finished.connect(lambda: self.toast.show_message("Server action completed."))
        self.pages.addWidget(self.servers_page)

        self.friends_page = FriendsPage()
        self.friends_page.log_msg_signal.connect(self.log_msg)
        self.friends_page.action_finished.connect(lambda: self.toast.show_message("Friend action completed."))
        self.pages.addWidget(self.friends_page)

        self.blocked_page = BlockedPage()
        self.blocked_page.log_msg_signal.connect(self.log_msg)
        self.blocked_page.action_finished.connect(lambda: self.toast.show_message("Users Unblocked Successfully", msg_type="success"))
        self.pages.addWidget(self.blocked_page)

        self.notifications_page = NotificationsPage()
        self.notifications_page.log_msg_signal.connect(self.log_msg)
        self.notifications_page.action_finished.connect(lambda msg, mtype: self.toast.show_message(msg, msg_type=mtype))
        self.pages.addWidget(self.notifications_page)

        self.logs_page = LogsPage()
        self.pages.addWidget(self.logs_page)
        
        self.stdout_interceptor = StreamInterceptor("debug")
        self.stdout_interceptor.log_signal.connect(self.log_msg)
        sys.stdout = self.stdout_interceptor

        self.stderr_interceptor = StreamInterceptor("error")
        self.stderr_interceptor.log_signal.connect(self.log_msg)
        sys.stderr = self.stderr_interceptor
        
        self.toast = ToastOverlay(self)
        
        def my_excepthook(type, value, tback):
            import os, traceback
            os.makedirs('scratch', exist_ok=True)
            with open('scratch/crash.log', 'w') as f:
                f.write(''.join(traceback.format_exception(type, value, tback)))
            sys.__excepthook__(type, value, tback)
        sys.excepthook = my_excepthook
        
        self.set_authenticated(False)
        self.switch_page("login")
        
        QTimer.singleShot(100, self.check_saved_token)

    def _on_nav_click(self, ref, btn):
        self.switch_page(ref)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.move(self.width() - self.toast.width() - 24, 24)
            
    def set_authenticated(self, state):
        for btn in self.nav_btns.values():
            btn.setEnabled(state)
        self.logout_btn.setVisible(state)
        self.account_frame.setVisible(state)
        
    def switch_page(self, ref):
        for name, btn in self.nav_btns.items():
            is_active = name == ref
            btn.setChecked(is_active)
            nav_icons = {
                "servers": 'fa5s.server',
                "friends": 'mdi.account-multiple',
                "blocked": 'fa5s.user-slash',
                "notifications": 'fa5s.bell',
                "logs": 'fa5s.terminal'
            }
            if name in nav_icons:
                color = ACCENT if is_active else TEXT_SECONDARY
                btn.setIcon(qta.icon(nav_icons[name], color=color))
            
        page_index = {"login": 0, "servers": 1, "friends": 2, "blocked": 3, "notifications": 4, "logs": 5}.get(ref, 0)
        self.pages.setCurrentIndex(page_index)

    def log_msg(self, message, msg_type="info"):
        self.logs_page.log_msg(message, msg_type)

    def check_saved_token(self):
        self.log_msg("Querying credential manager...", "debug")
        try:
            saved_token = keyring.get_password(SERVICE_ID, KEY_ID)
        except Exception as e:
            self.log_msg(f"Failed to read credential store: {e}", "error")
            saved_token = None
        if saved_token:
            self.log_msg("Cached token detected. Verifying...")
            self.start_login(saved_token, save=False)
        else:
            self.log_msg("No token found. Awaiting manual input.", "debug")

    def start_login(self, token, save=True):
        self.login_page.set_loading(True)
        self.login_page.set_status("")
        
        self.login_worker = LoginWorker(token, save=save)
        self.login_worker.finished.connect(self.login_worker.deleteLater)
        self.login_worker.result_signal.connect(self.on_login_result)
        self.login_worker.start()

    def on_login_result(self, success, message, raw_username, token, avatar_bytes, save):
        self.login_page.set_loading(False)
        
        if not success:
            self.log_msg(f"AUTH FAILED: {message}", "error")
            self.login_page.set_status(f"Authentication failed: {message}")
            try:
                keyring.delete_password(SERVICE_ID, KEY_ID)
            except Exception:
                pass
            return
            
        self.log_msg(f"AUTH OK: {message}", "success")
        self.account_name = message
        self.account_name_label.setText(self.account_name)
        if self.account_name:
            if avatar_bytes:
                from PyQt5.QtGui import QPixmap, QPainter, QPainterPath
                from PyQt5.QtCore import Qt
                pixmap = QPixmap()
                if pixmap.loadFromData(avatar_bytes):
                    pixmap = pixmap.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    
                    # Create circular mask
                    rounded = QPixmap(24, 24)
                    rounded.fill(Qt.transparent)
                    painter = QPainter(rounded)
                    painter.setRenderHint(QPainter.Antialiasing)
                    path = QPainterPath()
                    path.addEllipse(0, 0, 24, 24)
                    painter.setClipPath(path)
                    painter.drawPixmap(0, 0, pixmap)
                    painter.end()
                    
                    self.account_avatar.setStyleSheet("background-color: transparent;")
                    self.account_avatar.setPixmap(rounded)
                    self.account_avatar.setText("")
                else:
                    self.account_avatar.setStyleSheet(f"background-color: {ACCENT}; color: {BG_DARKEST}; border-radius: 12px; font-weight: 700; font-size: 11px;")
                    self.account_avatar.setText(self.account_name[0].upper() if self.account_name else "?")
                    self.account_avatar.setPixmap(QPixmap())
            else:
                self.account_avatar.setStyleSheet(f"background-color: {ACCENT}; color: {BG_DARKEST}; border-radius: 12px; font-weight: 700; font-size: 11px;")
                self.account_avatar.setText(self.account_name[0].upper() if self.account_name else "?")
                self.account_avatar.setPixmap(QPixmap())
            self.setWindowTitle(f"Cleanup Tool - {self.account_name}")
        self.toast.show_message(f"Logged in as {self.account_name}", msg_type="success")
        self.token = token
        self.servers_page.set_token(token)
        self.friends_page.set_token(token)
        self.notifications_page.set_token(token)
        self.blocked_page.set_token(token)

        if save:
            self.log_msg("Writing token to secure storage...", "debug")
            try:
                keyring.set_password(SERVICE_ID, KEY_ID, token)
            except Exception as e:
                self.log_msg(f"Failed to save token: {e}", "error")
            
        self.set_authenticated(True)
        self.login_page.clear()
        self.switch_page("servers")
        
        self.servers_page.fetch_data()
        self.friends_page.fetch_data()
        self.blocked_page.fetch_data()

    def logout(self):
        self.token = ""
        self.account_name = ""
        self.account_name_label.setText("")
        self.account_avatar.setStyleSheet(f"background-color: {ACCENT}; color: {BG_DARKEST}; border-radius: 12px; font-weight: 700; font-size: 11px;")
        self.account_avatar.setText("?")
        self.account_avatar.setPixmap(QPixmap())
        self.setWindowTitle("Discord Mass Account Cleanup Tool")
        try:
            keyring.delete_password(SERVICE_ID, KEY_ID)
            self.log_msg("Token purged from local storage.")
        except Exception:
            pass
        self.set_authenticated(False)
        self.switch_page("login")
        
        self.servers_page.clear()
        self.friends_page.clear()
        self.notifications_page.set_token("")
        self.log_msg("Session closed.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
