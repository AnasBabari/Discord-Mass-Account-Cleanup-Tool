from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton, QProgressBar, QGraphicsDropShadowEffect
from PyQt5.QtGui import QCursor, QColor
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import qtawesome as qta
from ui.theme import *
from ui.components import SectionHeader
from workers import ReadNotifsWorker

class NotificationsPage(QWidget):
    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal(str, str) # title, msg_type
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.token = ""
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(0)
        
        header = SectionHeader('fa5s.bell', 'Notifications')
        layout.addWidget(header)
        
        layout.addStretch()

        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER};
                border-radius: 16px;
            }}
        """)
        card.setFixedWidth(440)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(40)
        shadow.setColor(QColor(0, 0, 0, 60))
        shadow.setOffset(0, 6)
        card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(36, 32, 36, 32)
        card_layout.setSpacing(0)
        card_layout.setAlignment(Qt.AlignCenter)

        bell_frame = QFrame()
        bell_frame.setFixedSize(64, 64)
        bell_frame.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(245, 158, 11, 0.1);
                border-radius: 20px;
                border: 1px solid rgba(245, 158, 11, 0.15);
            }}
        """)
        bell_inner = QVBoxLayout(bell_frame)
        bell_inner.setContentsMargins(0, 0, 0, 0)
        bell_icon = QLabel()
        bell_icon.setPixmap(qta.icon('fa5s.bell', color=WARNING).pixmap(QSize(28, 28)))
        bell_icon.setAlignment(Qt.AlignCenter)
        bell_inner.addWidget(bell_icon)

        bell_row = QHBoxLayout()
        bell_row.addStretch()
        bell_row.addWidget(bell_frame)
        bell_row.addStretch()
        card_layout.addLayout(bell_row)

        card_layout.addSpacing(20)

        notif_title = QLabel("Mark All as Read")
        notif_title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 18px; font-weight: 700;")
        notif_title.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(notif_title)

        card_layout.addSpacing(8)

        desc = QLabel("This will mark all DMs, group chats, and\nserver channels as read instantly.")
        desc.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px; line-height: 1.5;")
        desc.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(desc)
        
        card_layout.addSpacing(24)

        self.read_notifs_btn = QPushButton("  Mark All Read")
        self.read_notifs_btn.setObjectName("ActionBtn")
        self.read_notifs_btn.setIcon(qta.icon('fa5s.check-double', color=BG_DARKEST))
        self.read_notifs_btn.setIconSize(QSize(16, 16))
        self.read_notifs_btn.setFixedHeight(44)
        self.read_notifs_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.read_notifs_btn.clicked.connect(self.read_notifications)
        card_layout.addWidget(self.read_notifs_btn)

        card_layout.addSpacing(16)
        self.read_notifs_progress = QProgressBar()
        self.read_notifs_progress.setRange(0, 0)
        self.read_notifs_progress.setFixedHeight(6)
        self.read_notifs_progress.setTextVisible(False)
        self.read_notifs_progress.hide()
        card_layout.addWidget(self.read_notifs_progress)

        card_row = QHBoxLayout()
        card_row.addStretch()
        card_row.addWidget(card)
        card_row.addStretch()
        layout.addLayout(card_row)
        
        layout.addStretch()

    def set_token(self, token):
        self.token = token

    def read_notifications(self):
        if not self.token:
            self.log_msg_signal.emit("No token set. Please log in first.", "error")
            return
        self.read_notifs_btn.setEnabled(False)
        self.read_notifs_btn.setText("  Processing...")
        self.read_notifs_progress.show()
        self.log_msg_signal.emit("Scraping unread states... (ETA: 5-10s)", "info")
        
        self.read_worker = ReadNotifsWorker(self.token)
        self.read_worker.finished.connect(self.read_worker.deleteLater)
        self.read_worker.progress_signal.connect(lambda msg: self.log_msg_signal.emit(msg, "info"))
        self.read_worker.finished_signal.connect(self.on_read_finished)
        self.read_worker.start()

    def on_read_finished(self, success, failed, err):
        self.read_notifs_btn.setEnabled(True)
        self.read_notifs_btn.setText("  Mark All Read")
        self.read_notifs_progress.hide()
        if err:
            self.log_msg_signal.emit(f"[-] ERR: {err}", "error")
            self.action_finished.emit(f"Error: {err}", "error")
        else:
            self.log_msg_signal.emit(f"COMPLETE. READ: {success}, FAIL: {failed}", "success")
            self.action_finished.emit(f"Marked {success} channels as read.", "info")
