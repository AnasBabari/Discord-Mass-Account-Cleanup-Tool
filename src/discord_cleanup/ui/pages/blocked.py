from __future__ import annotations

from collections.abc import Callable
from typing import Any

import qtawesome as qta
from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.ui.components import (
    LoadingOverlay,
    SectionHeader,
    StatBadge,
    StatCard,
)
from discord_cleanup.ui.theme import (
    ACCENT,
    TEXT_DIM,
    TEXT_SECONDARY,
)
from discord_cleanup.workers.batch import UnblockUsersWorker
from discord_cleanup.workers.fetch import FetchBlockedWorker


class BlockedPage(QWidget):
    """Blocked users management and cleanup page."""

    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()

    def __init__(self, worker_tracker: Callable[[Any], Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.worker_tracker = worker_tracker or (lambda w: w)
        self.token = ""
        self.blocked_data: list[dict[str, Any]] = []
        self.blocked_worker: Any = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # 1. Header
        header_row = QHBoxLayout()
        header = SectionHeader("fa5s.user-slash", "Blocked Users Management")
        header_row.addWidget(header)
        header_row.addStretch()

        self.refresh_btn = QPushButton(" Refresh")
        self.refresh_btn.setIcon(qta.icon("fa5s.sync-alt", color=TEXT_SECONDARY))
        self.refresh_btn.setIconSize(QSize(13, 13))
        self.refresh_btn.setObjectName("GhostBtn")
        self.refresh_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.refresh_btn.clicked.connect(self.fetch_data)
        header_row.addWidget(self.refresh_btn)
        layout.addLayout(header_row)

        # 2. Stats
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)
        self.stat_total = StatCard("TOTAL BLOCKED", "0", "fa5s.user-slash", ACCENT)
        stats_layout.addWidget(self.stat_total)
        layout.addLayout(stats_layout)

        # 3. Actions Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search blocked users by name, handle, or ID...")
        self.search_input.textChanged.connect(self.filter_blocked)
        self.search_input.setFixedHeight(38)
        toolbar.addWidget(self.search_input, 1)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setObjectName("GhostBtn")
        self.select_all_btn.setFixedHeight(38)
        self.select_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.select_all_btn.clicked.connect(self.select_all_blocked)
        toolbar.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setObjectName("GhostBtn")
        self.deselect_all_btn.setFixedHeight(38)
        self.deselect_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.deselect_all_btn.clicked.connect(self.deselect_all_blocked)
        toolbar.addWidget(self.deselect_all_btn)

        self.stat_badge = StatBadge()
        toolbar.addWidget(self.stat_badge)

        self.unblock_btn = QPushButton(" Unblock Selected")
        self.unblock_btn.setObjectName("ActionBtn")
        self.unblock_btn.setIcon(qta.icon("fa5s.unlock", color="#ffffff"))
        self.unblock_btn.setIconSize(QSize(14, 14))
        self.unblock_btn.setFixedHeight(38)
        self.unblock_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.unblock_btn.clicked.connect(self.unblock_selected_users)
        toolbar.addWidget(self.unblock_btn)

        layout.addLayout(toolbar)

        # Progress bar
        self.blocked_progress = QProgressBar()
        self.blocked_progress.setTextVisible(False)
        self.blocked_progress.hide()
        layout.addWidget(self.blocked_progress)

        # 4. Table Stack
        self.table_stack = QStackedWidget()
        self.table_stack.setFrameShape(QFrame.NoFrame)

        self.blocked_table = QTableWidget()
        self.blocked_table.setColumnCount(3)
        self.blocked_table.setHorizontalHeaderLabels(["", "Display Name / Handle", "User ID"])
        self.blocked_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.blocked_table.setColumnWidth(0, 48)
        self.blocked_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.blocked_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.blocked_table.verticalHeader().setVisible(False)
        self.blocked_table.setShowGrid(False)
        self.blocked_table.itemChanged.connect(self.update_status)
        self.table_stack.addWidget(self.blocked_table)

        self.loading_overlay = LoadingOverlay()
        self.table_stack.addWidget(self.loading_overlay)

        empty_widget = QWidget()
        empty_layout = QVBoxLayout(empty_widget)
        empty_layout.setAlignment(Qt.AlignCenter)
        self.empty_label = QLabel("No blocked users to display.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px;")
        empty_layout.addWidget(self.empty_label)
        self.table_stack.addWidget(empty_widget)

        layout.addWidget(self.table_stack, 1)

    def set_token(self, token: str) -> None:
        self.token = token

    def fetch_data(self) -> None:
        if not self.token:
            return
        if self.blocked_worker is not None and self.blocked_worker.isRunning():
            self.blocked_worker.cancel()

        self.table_stack.setCurrentIndex(1)
        self.loading_overlay.set_status("Fetching blocked users...")
        self.loading_overlay.set_detail("Communicating with Discord API")

        self.blocked_worker = self.worker_tracker(FetchBlockedWorker(self.token))
        self.blocked_worker.result_signal.connect(self.on_blocked_fetched)
        self.blocked_worker.start()

    def on_blocked_fetched(self, blocked: list[dict[str, Any]], error: str) -> None:
        if not self.token:
            return
        if error:
            self.log_msg_signal.emit(f"Failed to fetch blocked users: {error}", "error")
            self.empty_label.setText(f"Failed to load blocked users: {error}")
            self.table_stack.setCurrentIndex(2)
            return

        self.blocked_data = blocked
        total = len(blocked)
        self.stat_total.set_value(str(total))
        self.populate_table()

        if not blocked:
            self.empty_label.setText("No blocked users to display.")
            self.table_stack.setCurrentIndex(2)
        else:
            self.table_stack.setCurrentIndex(0)

    def populate_table(self) -> None:
        self.blocked_table.blockSignals(True)
        self.blocked_table.setRowCount(0)

        for rel in self.blocked_data:
            user = rel.get("user", {})
            row = self.blocked_table.rowCount()
            self.blocked_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignCenter)
            self.blocked_table.setItem(row, 0, chk)

            username = user.get("username", "Unknown")
            global_name = user.get("global_name")
            display_text = f"{global_name} (@{username})" if global_name else f"@{username}"
            name_item = QTableWidgetItem(display_text)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            name_item.setForeground(Qt.white)
            self.blocked_table.setItem(row, 1, name_item)

            u_id = str(user.get("id") or rel.get("id", ""))
            id_item = QTableWidgetItem(u_id)
            id_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            id_item.setForeground(Qt.darkGray)
            self.blocked_table.setItem(row, 2, id_item)

        self.blocked_table.blockSignals(False)
        self.update_status()

    def filter_blocked(self, query: str) -> None:
        query = query.strip().lower()
        for row in range(self.blocked_table.rowCount()):
            name = (self.blocked_table.item(row, 1).text() if self.blocked_table.item(row, 1) else "").lower()
            uid = (self.blocked_table.item(row, 2).text() if self.blocked_table.item(row, 2) else "").lower()
            match = query in name or query in uid
            self.blocked_table.setRowHidden(row, not match)
        self.update_status()

    def select_all_blocked(self) -> None:
        self.blocked_table.blockSignals(True)
        for row in range(self.blocked_table.rowCount()):
            if not self.blocked_table.isRowHidden(row):
                item = self.blocked_table.item(row, 0)
                if item:
                    item.setCheckState(Qt.Checked)
        self.blocked_table.blockSignals(False)
        self.update_status()

    def deselect_all_blocked(self) -> None:
        self.blocked_table.blockSignals(True)
        for row in range(self.blocked_table.rowCount()):
            item = self.blocked_table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked)
        self.blocked_table.blockSignals(False)
        self.update_status()

    def update_status(self) -> None:
        selected = 0
        visible = 0
        for row in range(self.blocked_table.rowCount()):
            if not self.blocked_table.isRowHidden(row):
                visible += 1
                item = self.blocked_table.item(row, 0)
                if item and item.checkState() == Qt.Checked:
                    selected += 1

        self.stat_badge.setText(f"Selected: {selected} / {visible}")
        self.unblock_btn.setEnabled(selected > 0)

    def unblock_selected_users(self) -> None:
        selected: list[dict[str, Any]] = []
        for row in range(self.blocked_table.rowCount()):
            chk = self.blocked_table.item(row, 0)
            if chk and chk.checkState() == Qt.Checked:
                uid = self.blocked_table.item(row, 2).text() if self.blocked_table.item(row, 2) else ""
                name = self.blocked_table.item(row, 1).text() if self.blocked_table.item(row, 1) else ""
                selected.append({"id": uid, "user": {"id": uid, "username": name}})

        if not selected:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Unblock Users",
            f"Are you sure you want to unblock {len(selected)} user(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.unblock_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.blocked_progress.setMaximum(len(selected))
        self.blocked_progress.setValue(0)
        self.blocked_progress.show()

        worker = self.worker_tracker(UnblockUsersWorker(self.token, selected))
        worker.progress_signal.connect(self.on_unblock_progress)
        worker.finished_signal.connect(self.on_unblock_finished)
        worker.start()

    def on_unblock_progress(self, count: int, log_msg: str) -> None:
        self.blocked_progress.setValue(count)
        msg_type = "info" if log_msg.startswith("[+]") else "error"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_unblock_finished(self, success: int, failed: int) -> None:
        self.blocked_progress.hide()
        self.unblock_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.log_msg_signal.emit(f"Unblock Completed: {success} Unblocked, {failed} Failed", "success" if failed == 0 else "warning")
        self.action_finished.emit()
        self.fetch_data()

    def clear(self) -> None:
        self.token = ""
        self.blocked_data = []
        self.blocked_table.setRowCount(0)
        self.stat_total.set_value("0")
        self.stat_badge.setText("Selected: 0 / 0")
        self.unblock_btn.setEnabled(False)
