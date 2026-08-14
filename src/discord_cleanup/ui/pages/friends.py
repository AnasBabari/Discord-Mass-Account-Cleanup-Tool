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
    get_length_str,
)
from discord_cleanup.ui.theme import (
    ACCENT,
    DANGER,
    TEXT_DIM,
    TEXT_SECONDARY,
)
from discord_cleanup.workers.batch import BlockUsersWorker, RemoveFriendsWorker
from discord_cleanup.workers.fetch import FetchFriendsWorker


class FriendsPage(QWidget):
    """Friends management and cleanup page."""

    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()

    def __init__(self, worker_tracker: Callable[[Any], Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.worker_tracker = worker_tracker or (lambda w: w)
        self.token = ""
        self.friends_data: list[dict[str, Any]] = []
        self.friends_worker: Any = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # 1. Header
        header_row = QHBoxLayout()
        header = SectionHeader("fa5s.user-friends", "Friend Management")
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
        self.stat_total = StatCard("TOTAL FRIENDS", "0", "fa5s.user-friends", ACCENT)
        stats_layout.addWidget(self.stat_total)
        layout.addLayout(stats_layout)

        # 3. Actions Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search friends by name, handle, or ID...")
        self.search_input.textChanged.connect(self.filter_friends)
        self.search_input.setFixedHeight(38)
        toolbar.addWidget(self.search_input, 1)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setObjectName("GhostBtn")
        self.select_all_btn.setFixedHeight(38)
        self.select_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.select_all_btn.clicked.connect(self.select_all_friends)
        toolbar.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setObjectName("GhostBtn")
        self.deselect_all_btn.setFixedHeight(38)
        self.deselect_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.deselect_all_btn.clicked.connect(self.deselect_all_friends)
        toolbar.addWidget(self.deselect_all_btn)

        self.stat_badge = StatBadge()
        toolbar.addWidget(self.stat_badge)

        self.remove_friends_btn = QPushButton(" Remove Selected")
        self.remove_friends_btn.setObjectName("DangerBtn")
        self.remove_friends_btn.setIcon(qta.icon("fa5s.user-minus", color="#ffffff"))
        self.remove_friends_btn.setIconSize(QSize(14, 14))
        self.remove_friends_btn.setFixedHeight(38)
        self.remove_friends_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.remove_friends_btn.clicked.connect(self.remove_selected_friends)
        toolbar.addWidget(self.remove_friends_btn)

        self.block_friends_btn = QPushButton(" Block Selected")
        self.block_friends_btn.setObjectName("GhostBtn")
        self.block_friends_btn.setIcon(qta.icon("fa5s.user-slash", color=DANGER))
        self.block_friends_btn.setIconSize(QSize(14, 14))
        self.block_friends_btn.setFixedHeight(38)
        self.block_friends_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.block_friends_btn.clicked.connect(self.block_selected_friends)
        toolbar.addWidget(self.block_friends_btn)

        layout.addLayout(toolbar)

        # Progress bar
        self.friends_progress = QProgressBar()
        self.friends_progress.setTextVisible(False)
        self.friends_progress.hide()
        layout.addWidget(self.friends_progress)

        # 4. Table Stack
        self.table_stack = QStackedWidget()
        self.table_stack.setFrameShape(QFrame.NoFrame)

        self.friends_table = QTableWidget()
        self.friends_table.setColumnCount(4)
        self.friends_table.setHorizontalHeaderLabels(["", "Display Name / Handle", "Friend Since", "User ID"])
        self.friends_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.friends_table.setColumnWidth(0, 48)
        self.friends_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.friends_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.friends_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.friends_table.verticalHeader().setVisible(False)
        self.friends_table.setShowGrid(False)
        self.friends_table.itemChanged.connect(self.update_status)
        self.table_stack.addWidget(self.friends_table)

        self.loading_overlay = LoadingOverlay()
        self.table_stack.addWidget(self.loading_overlay)

        empty_widget = QWidget()
        empty_layout = QVBoxLayout(empty_widget)
        empty_layout.setAlignment(Qt.AlignCenter)
        self.empty_label = QLabel("No friends found.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px;")
        empty_layout.addWidget(self.empty_label)
        self.table_stack.addWidget(empty_widget)

        layout.addWidget(self.table_stack, 1)

    def set_token(self, token: str) -> None:
        self.token = token

    def fetch_data(self) -> None:
        if not self.token:
            return
        if self.friends_worker is not None and self.friends_worker.isRunning():
            self.friends_worker.cancel()

        self.table_stack.setCurrentIndex(1)
        self.loading_overlay.set_status("Fetching friend list...")
        self.loading_overlay.set_detail("Communicating with Discord API")

        self.friends_worker = self.worker_tracker(FetchFriendsWorker(self.token))
        self.friends_worker.result_signal.connect(self.on_friends_fetched)
        self.friends_worker.start()

    def on_friends_fetched(self, friends: list[dict[str, Any]], error: str) -> None:
        if not self.token:
            return
        if error:
            self.log_msg_signal.emit(f"Failed to fetch friends: {error}", "error")
            self.empty_label.setText(f"Failed to load friends: {error}")
            self.table_stack.setCurrentIndex(2)
            return

        self.friends_data = friends
        total = len(friends)
        self.stat_total.set_value(str(total))
        self.populate_table()

        if not friends:
            self.empty_label.setText("No friends found.")
            self.table_stack.setCurrentIndex(2)
        else:
            self.table_stack.setCurrentIndex(0)

    def populate_table(self) -> None:
        self.friends_table.blockSignals(True)
        self.friends_table.setRowCount(0)

        for rel in self.friends_data:
            user = rel.get("user", {})
            row = self.friends_table.rowCount()
            self.friends_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignCenter)
            self.friends_table.setItem(row, 0, chk)

            username = user.get("username", "Unknown")
            global_name = user.get("global_name")
            display_text = f"{global_name} (@{username})" if global_name else f"@{username}"
            name_item = QTableWidgetItem(display_text)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            name_item.setForeground(Qt.white)
            self.friends_table.setItem(row, 1, name_item)

            u_id = str(user.get("id") or rel.get("id", ""))
            since_val = rel.get("since")
            age_str = get_length_str(u_id, fallback_timestamp=since_val)
            age_item = QTableWidgetItem(age_str)
            age_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            age_item.setForeground(Qt.lightGray)
            self.friends_table.setItem(row, 2, age_item)

            id_item = QTableWidgetItem(u_id)
            id_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            id_item.setForeground(Qt.darkGray)
            self.friends_table.setItem(row, 3, id_item)

        self.friends_table.blockSignals(False)
        self.update_status()

    def filter_friends(self, query: str) -> None:
        query = query.strip().lower()
        for row in range(self.friends_table.rowCount()):
            name = (self.friends_table.item(row, 1).text() if self.friends_table.item(row, 1) else "").lower()
            uid = (self.friends_table.item(row, 3).text() if self.friends_table.item(row, 3) else "").lower()
            match = query in name or query in uid
            self.friends_table.setRowHidden(row, not match)
        self.update_status()

    def select_all_friends(self) -> None:
        self.friends_table.blockSignals(True)
        for row in range(self.friends_table.rowCount()):
            if not self.friends_table.isRowHidden(row):
                item = self.friends_table.item(row, 0)
                if item:
                    item.setCheckState(Qt.Checked)
        self.friends_table.blockSignals(False)
        self.update_status()

    def deselect_all_friends(self) -> None:
        self.friends_table.blockSignals(True)
        for row in range(self.friends_table.rowCount()):
            item = self.friends_table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked)
        self.friends_table.blockSignals(False)
        self.update_status()

    def update_status(self) -> None:
        selected = 0
        visible = 0
        for row in range(self.friends_table.rowCount()):
            if not self.friends_table.isRowHidden(row):
                visible += 1
                item = self.friends_table.item(row, 0)
                if item and item.checkState() == Qt.Checked:
                    selected += 1

        self.stat_badge.setText(f"Selected: {selected} / {visible}")
        self.remove_friends_btn.setEnabled(selected > 0)
        self.block_friends_btn.setEnabled(selected > 0)

    def _get_selected_friends(self) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in range(self.friends_table.rowCount()):
            chk = self.friends_table.item(row, 0)
            if chk and chk.checkState() == Qt.Checked:
                uid = self.friends_table.item(row, 3).text() if self.friends_table.item(row, 3) else ""
                name = self.friends_table.item(row, 1).text() if self.friends_table.item(row, 1) else ""
                selected.append({"id": uid, "user": {"id": uid, "username": name}})
        return selected

    def remove_selected_friends(self) -> None:
        selected = self._get_selected_friends()
        if not selected:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Remove Friends",
            f"Are you sure you want to remove {len(selected)} friend(s)?\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._start_action_worker(RemoveFriendsWorker(self.token, selected), "Removal")

    def block_selected_friends(self) -> None:
        selected = self._get_selected_friends()
        if not selected:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Block Users",
            f"Are you sure you want to block {len(selected)} user(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._start_action_worker(BlockUsersWorker(self.token, selected), "Block")

    def _start_action_worker(self, worker: Any, action_title: str) -> None:
        self.remove_friends_btn.setEnabled(False)
        self.block_friends_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.friends_progress.setMaximum(len(worker.items))
        self.friends_progress.setValue(0)
        self.friends_progress.show()

        tracked = self.worker_tracker(worker)
        tracked.progress_signal.connect(self.on_remove_progress)
        if action_title == "Removal":
            tracked.finished_signal.connect(self.on_remove_finished)
        else:
            tracked.finished_signal.connect(self.on_block_finished)
        tracked.start()

    def on_remove_progress(self, count: int, log_msg: str) -> None:
        self.friends_progress.setValue(count)
        msg_type = "info" if log_msg.startswith("[+]") else "error"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_remove_finished(self, success: int, failed: int) -> None:
        self.friends_progress.hide()
        self.remove_friends_btn.setEnabled(True)
        self.block_friends_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.log_msg_signal.emit(f"Friend Removal Completed: {success} Removed, {failed} Failed", "success" if failed == 0 else "warning")
        self.action_finished.emit()
        self.fetch_data()

    def on_block_finished(self, success: int, failed: int) -> None:
        self.friends_progress.hide()
        self.remove_friends_btn.setEnabled(True)
        self.block_friends_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.log_msg_signal.emit(f"Block Completed: {success} Blocked, {failed} Failed", "success" if failed == 0 else "warning")
        self.action_finished.emit()
        self.fetch_data()

    def clear(self) -> None:
        self.token = ""
        self.friends_data = []
        self.friends_table.setRowCount(0)
        self.stat_total.set_value("0")
        self.stat_badge.setText("Selected: 0 / 0")
        self.remove_friends_btn.setEnabled(False)
        self.block_friends_btn.setEnabled(False)
