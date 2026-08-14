from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QLineEdit,
    QPushButton, QGraphicsOpacityEffect, QGraphicsDropShadowEffect
)
from PyQt5.QtGui import QColor, QCursor
from PyQt5.QtCore import Qt, QSize, pyqtSignal, QPropertyAnimation
import qtawesome as qta
from ui.theme import (
    ACCENT, BG_CARD, DANGER, TEXT_DIM, TEXT_PRIMARY, TEXT_SECONDARY
)


class LoginPage(QWidget):
    login_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        container = QFrame()
        container.setObjectName("LoginContainer")
        container.setStyleSheet(f"""
            QFrame#LoginContainer {{
                background-color: {BG_CARD};
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: 18px;
            }}
        """)
        container.setFixedWidth(440)

        shadow = QGraphicsDropShadowEffect(container)
        shadow.setBlurRadius(50)
        shadow.setColor(QColor(0, 0, 0, 110))
        shadow.setOffset(0, 8)
        container.setGraphicsEffect(shadow)

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
        icon_lbl.setPixmap(qta.icon('fa5s.shield-alt', color=ACCENT).pixmap(QSize(26, 26)))
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_inner_layout.addWidget(icon_lbl)

        icon_row = QHBoxLayout()
        icon_row.addStretch()
        icon_row.addWidget(icon_frame)
        icon_row.addStretch()
        c_layout.addLayout(icon_row)

        c_layout.addSpacing(22)

        title = QLabel("Sign In")
        title.setStyleSheet(f"""
            color: {TEXT_PRIMARY};
            font-size: 24px;
            font-weight: 700;
            letter-spacing: -0.4px;
        """)
        title.setAlignment(Qt.AlignCenter)
        c_layout.addWidget(title)

        c_layout.addSpacing(8)

        desc = QLabel("Enter your Discord token to manage your account")
        desc.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px;")
        desc.setAlignment(Qt.AlignCenter)
        c_layout.addWidget(desc)

        c_layout.addSpacing(26)

        token_label = QLabel("Discord User Token")
        token_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; font-weight: 600; margin-bottom: 4px;")
        c_layout.addWidget(token_label)
        c_layout.addSpacing(6)

        self.token_entry = QLineEdit()
        self.token_entry.setPlaceholderText("Paste your token here...")
        self.token_entry.setEchoMode(QLineEdit.Password)
        self.token_entry.setFixedHeight(44)
        self.token_entry.returnPressed.connect(self.request_login)

        self.toggle_action = self.token_entry.addAction(
            qta.icon('fa5s.eye', color=TEXT_SECONDARY), QLineEdit.TrailingPosition
        )
        self.toggle_action.triggered.connect(self.toggle_token_visibility)

        c_layout.addWidget(self.token_entry)

        c_layout.addSpacing(18)

        self.login_btn = QPushButton("Sign In")
        self.login_btn.setObjectName("ActionBtn")
        self.login_btn.setFixedHeight(44)
        self.login_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.login_btn.clicked.connect(self.request_login)
        c_layout.addWidget(self.login_btn)

        c_layout.addSpacing(12)

        remember_label = QLabel("Tokens are securely stored in your system credential vault")
        remember_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 11px;")
        remember_label.setAlignment(Qt.AlignCenter)
        c_layout.addWidget(remember_label)

        c_layout.addSpacing(12)

        self.login_status = QLabel("")
        self.login_status.setStyleSheet(f"color: {DANGER}; font-size: 12px; font-weight: 500;")
        self.login_status.setAlignment(Qt.AlignCenter)
        self.login_status.setWordWrap(True)

        self.status_opacity = QGraphicsOpacityEffect(self.login_status)
        self.login_status.setGraphicsEffect(self.status_opacity)

        c_layout.addWidget(self.login_status)

        c_layout.addStretch()
        layout.addWidget(container)

    def toggle_token_visibility(self):
        if self.token_entry.echoMode() == QLineEdit.Password:
            self.token_entry.setEchoMode(QLineEdit.Normal)
            self.toggle_action.setIcon(qta.icon('fa5s.eye-slash', color=TEXT_SECONDARY))
        else:
            self.token_entry.setEchoMode(QLineEdit.Password)
            self.toggle_action.setIcon(qta.icon('fa5s.eye', color=TEXT_SECONDARY))

    def request_login(self):
        token = self.token_entry.text().strip()
        if not token:
            self.set_status("Please enter your token")
            return
        self.login_requested.emit(token)

    def set_status(self, text):
        self.login_status.setText(text)
        if text:
            self.anim = QPropertyAnimation(self.status_opacity, b"opacity")
            self.anim.setDuration(300)
            self.anim.setStartValue(0.0)
            self.anim.setEndValue(1.0)
            self.anim.start()

    def set_loading(self, is_loading: bool):
        self.login_btn.setEnabled(not is_loading)
        self.token_entry.setEnabled(not is_loading)
        if is_loading:
            self.login_btn.setText("Signing In...")
        else:
            self.login_btn.setText("Sign In")

    def clear(self):
        self.token_entry.clear()
        self.login_status.setText("")
