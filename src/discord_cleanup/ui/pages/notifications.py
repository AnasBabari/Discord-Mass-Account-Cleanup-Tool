from __future__ import annotations

from typing import Any, Callable
import qtawesome as qta
from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.ui.components import GlassCard, SectionHeader
from discord_cleanup.ui.theme import (
    ACCENT,
    TEXT_DIM,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from discord_cleanup.workers.notifications import ReadNotifsWorker


class NotificationsPage(QWidget):
    """Notification cleanup and mark-as-read page."""

    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()

    def __init__(self, worker_tracker: Callable[[Any], Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.worker_tracker = worker_tracker or (lambda w: w)
        self.token = ""
        self.notif_worker: Any = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(24)

        header = SectionHeader("fa5s.bell", "Notification Management")
        layout.addWidget(header)

        # Central Card
        card = GlassCard()
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(32, 32, 32, 32)
        c_layout.setSpacing(18)
        c_layout.setAlignment(Qt.AlignCenter)

        icon_frame = QFrame()
        icon_frame.setFixedSize(64, 64)
        icon_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(56, 189, 248, 0.1);
                border-radius: 20px;
                border: 1px solid rgba(56, 189, 248, 0.2);
            }
        """)
        if_layout = QVBoxLayout(icon_frame)
        if_layout.setContentsMargins(0, 0, 0, 0)
        icon_lbl = QLabel()
        icon_lbl.setPixmap(qta.icon("fa5s.bell-slash", color=ACCENT).pixmap(QSize(28, 28)))
        icon_lbl.setAlignment(Qt.AlignCenter)
        if_layout.addWidget(icon_lbl)

        icon_row = QHBoxLayout()
        icon_row.setAlignment(Qt.AlignCenter)
        icon_row.addWidget(icon_frame)
        c_layout.addLayout(icon_row)

        card_title = QLabel("Bulk Mark Notifications as Read")
        card_title.setAlignment(Qt.AlignCenter)
        card_title.setStyleSheet(f"""
            font-size: 18px;
            font-weight: 700;
            color: {TEXT_PRIMARY};
        """)
        c_layout.addWidget(card_title)

        card_desc = QLabel(
            "Connects to the Discord Gateway, finds unread channels across all joined servers "
            "and DMs, and acknowledges all notification badges."
        )
        card_desc.setAlignment(Qt.AlignCenter)
        card_desc.setWordWrap(True)
        card_desc.setStyleSheet(f"""
            font-size: 13px;
            color: {TEXT_SECONDARY};
            max-width: 480px;
            line-height: 1.5;
        """)
        c_layout.addWidget(card_desc)
        c_layout.addSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)
        self.clear_notifs_btn = QPushButton(" Mark All as Read")
        self.clear_notifs_btn.setObjectName("ActionBtn")
        self.clear_notifs_btn.setIcon(qta.icon("fa5s.check-double", color="#020617"))
        self.clear_notifs_btn.setIconSize(QSize(15, 15))
        self.clear_notifs_btn.setFixedSize(220, 44)
        self.clear_notifs_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.clear_notifs_btn.clicked.connect(self.start_clear_notifications)
        btn_row.addWidget(self.clear_notifs_btn)
        c_layout.addLayout(btn_row)

        self.notifs_progress = QProgressBar()
        self.notifs_progress.setTextVisible(False)
        self.notifs_progress.setFixedWidth(400)
        self.notifs_progress.hide()
        p_row = QHBoxLayout()
        p_row.setAlignment(Qt.AlignCenter)
        p_row.addWidget(self.notifs_progress)
        c_layout.addLayout(p_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        c_layout.addWidget(self.status_lbl)

        layout.addWidget(card)
        layout.addStretch()

    def set_token(self, token: str) -> None:
        self.token = token

    def start_clear_notifications(self) -> None:
        if not self.token:
            return

        self.clear_notifs_btn.setEnabled(False)
        self.status_lbl.setText("Connecting to Discord Gateway...")
        self.notifs_progress.setValue(0)
        self.notifs_progress.show()

        self.notif_worker = self.worker_tracker(ReadNotifsWorker(self.token))
        self.notif_worker.progress_signal.connect(self.on_notif_progress)
        self.notif_worker.chunk_progress_signal.connect(self.on_chunk_progress)
        self.notif_worker.finished_signal.connect(self.on_notif_finished)
        self.notif_worker.start()

    def on_notif_progress(self, msg: str) -> None:
        self.status_lbl.setText(msg)
        self.log_msg_signal.emit(msg, "info")

    def on_chunk_progress(self, current: int, total: int) -> None:
        self.notifs_progress.setMaximum(total)
        self.notifs_progress.setValue(current)

    def on_notif_finished(self, success: int, failed: int, err: str) -> None:
        self.notifs_progress.hide()
        self.clear_notifs_btn.setEnabled(True)
        if err:
            self.status_lbl.setText(f"Completed with notice: {err}")
            self.log_msg_signal.emit(f"Notifications: {err}", "warning")
        else:
            self.status_lbl.setText(f"Completed: {success} channels marked read.")
            self.log_msg_signal.emit(f"Notifications Cleared: {success} Read, {failed} Failed", "success")
        self.action_finished.emit()

    def clear(self) -> None:
        self.token = ""
        self.status_lbl.setText("")
        self.clear_notifs_btn.setEnabled(True)
