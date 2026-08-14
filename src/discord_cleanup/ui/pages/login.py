from __future__ import annotations

import qtawesome as qta
from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.ui.theme import (
    ACCENT,
    DANGER,
    TEXT_DIM,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)


class LoginPage(QWidget):
    """Login and authentication page."""

    login_requested = pyqtSignal(str, bool)  # (token, save_to_keyring)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.init_ui()

    def init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignCenter)
        main_layout.setContentsMargins(40, 40, 40, 40)

        container = QFrame()
        container.setFixedWidth(440)
        container.setStyleSheet("""
            QFrame {
                background-color: #0c141f;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
            }
        """)
        c_layout = QVBoxLayout(container)
        c_layout.setContentsMargins(40, 40, 40, 40)
        c_layout.setSpacing(0)

        icon_frame = QFrame()
        icon_frame.setFixedSize(60, 60)
        icon_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(56, 189, 248, 0.1);
                border-radius: 18px;
                border: 1px solid rgba(56, 189, 248, 0.2);
            }
        """)
        icon_inner_layout = QVBoxLayout(icon_frame)
        icon_inner_layout.setContentsMargins(0, 0, 0, 0)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(qta.icon("fa5s.shield-alt", color=ACCENT).pixmap(QSize(26, 26)))
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_inner_layout.addWidget(icon_lbl)

        icon_row = QHBoxLayout()
        icon_row.setAlignment(Qt.AlignCenter)
        icon_row.addWidget(icon_frame)
        c_layout.addLayout(icon_row)
        c_layout.addSpacing(20)

        title = QLabel("Authentication")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"""
            font-size: 22px;
            font-weight: 700;
            color: {TEXT_PRIMARY};
            letter-spacing: -0.4px;
        """)
        c_layout.addWidget(title)
        c_layout.addSpacing(6)

        subtitle = QLabel("Paste your Discord token below to authenticate.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"""
            font-size: 13px;
            color: {TEXT_SECONDARY};
            line-height: 1.4;
        """)
        c_layout.addWidget(subtitle)
        c_layout.addSpacing(28)

        token_lbl = QLabel("DISCORD TOKEN")
        token_lbl.setStyleSheet(f"""
            font-size: 10px;
            font-weight: 700;
            color: {TEXT_DIM};
            letter-spacing: 0.8px;
        """)
        c_layout.addWidget(token_lbl)
        c_layout.addSpacing(6)

        input_wrapper = QFrame()
        input_wrapper.setStyleSheet("""
            QFrame {
                background-color: rgba(6, 9, 14, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 10px;
            }
        """)
        iw_layout = QHBoxLayout(input_wrapper)
        iw_layout.setContentsMargins(12, 0, 8, 0)
        iw_layout.setSpacing(8)

        self.token_entry = QLineEdit()
        self.token_entry.setPlaceholderText("Paste token here...")
        self.token_entry.setEchoMode(QLineEdit.Password)
        self.token_entry.returnPressed.connect(self.request_login)
        self.token_entry.setStyleSheet("""
            QLineEdit {
                background: transparent;
                border: none;
                color: #f8fafc;
                font-size: 13px;
                padding: 10px 0px;
            }
        """)
        iw_layout.addWidget(self.token_entry, 1)

        self.toggle_pwd_btn = QPushButton()
        self.toggle_pwd_btn.setIcon(qta.icon("fa5s.eye", color=TEXT_DIM))
        self.toggle_pwd_btn.setIconSize(QSize(14, 14))
        self.toggle_pwd_btn.setFixedSize(28, 28)
        self.toggle_pwd_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.toggle_pwd_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        self.toggle_pwd_btn.clicked.connect(self.toggle_token_visibility)
        iw_layout.addWidget(self.toggle_pwd_btn)

        c_layout.addWidget(input_wrapper)
        c_layout.addSpacing(16)

        self.save_token_checkbox = QCheckBox("Save token securely to OS Keyring")
        self.save_token_checkbox.setChecked(True)
        self.save_token_checkbox.setStyleSheet(f"""
            QCheckBox {{
                color: {TEXT_SECONDARY};
                font-size: 12px;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid rgba(148, 163, 184, 0.4);
                background-color: rgba(12, 20, 31, 0.6);
            }}
            QCheckBox::indicator:checked {{
                border-color: {ACCENT};
                background-color: {ACCENT};
            }}
        """)
        c_layout.addWidget(self.save_token_checkbox)
        c_layout.addSpacing(24)

        self.login_btn = QPushButton("Connect Account")
        self.login_btn.setObjectName("ActionBtn")
        self.login_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.login_btn.clicked.connect(self.request_login)
        c_layout.addWidget(self.login_btn)
        c_layout.addSpacing(12)

        self.login_status = QLabel("")
        self.login_status.setAlignment(Qt.AlignCenter)
        self.login_status.setWordWrap(True)
        self.login_status.setStyleSheet(f"""
            color: {DANGER};
            font-size: 12px;
            font-weight: 500;
        """)
        c_layout.addWidget(self.login_status)

        main_layout.addWidget(container)

    def toggle_token_visibility(self) -> None:
        if self.token_entry.echoMode() == QLineEdit.Password:
            self.token_entry.setEchoMode(QLineEdit.Normal)
            self.toggle_pwd_btn.setIcon(qta.icon("fa5s.eye-slash", color=ACCENT))
        else:
            self.token_entry.setEchoMode(QLineEdit.Password)
            self.toggle_pwd_btn.setIcon(qta.icon("fa5s.eye", color=TEXT_DIM))

    def request_login(self) -> None:
        token = self.token_entry.text().strip()
        if not token:
            self.set_status("Please enter your token")
            return
        self.login_status.setText("")
        self.login_requested.emit(token, self.save_token_checkbox.isChecked())

    def set_loading(self, loading: bool) -> None:
        self.login_btn.setEnabled(not loading)
        self.token_entry.setEnabled(not loading)
        if loading:
            self.login_btn.setText("Verifying Token...")
        else:
            self.login_btn.setText("Connect Account")

    def set_status(self, text: str) -> None:
        self.login_status.setText(text)

    def clear(self) -> None:
        self.token_entry.clear()
        self.login_status.setText("")
