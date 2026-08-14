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
from discord_cleanup.workers.batch import LeaveServersWorker
from discord_cleanup.workers.fetch import FetchServersWorker


class ServersPage(QWidget):
    """Servers management and cleanup page."""

    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()

    def __init__(self, worker_tracker: Callable[[Any], Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.worker_tracker = worker_tracker or (lambda w: w)
        self.token = ""
        self.servers_data: list[dict[str, Any]] = []
        self.servers_worker: Any = None
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # 1. Header
        header_row = QHBoxLayout()
        header = SectionHeader("fa5s.server", "Server Management")
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

        # 2. Stat Cards
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)
        self.stat_total = StatCard("TOTAL SERVERS", "0", "fa5s.server", ACCENT)
        self.stat_leavable = StatCard("LEAVABLE", "0", "fa5s.sign-out-alt", DANGER)
        self.stat_owned = StatCard("OWNED", "0", "fa5s.crown", "#fbbf24")
        stats_layout.addWidget(self.stat_total)
        stats_layout.addWidget(self.stat_leavable)
        stats_layout.addWidget(self.stat_owned)
        layout.addLayout(stats_layout)

        # 3. Action Toolbar & Filter
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search servers by name or ID...")
        self.search_input.textChanged.connect(self.filter_servers)
        self.search_input.setFixedHeight(38)
        toolbar.addWidget(self.search_input, 1)

        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setObjectName("GhostBtn")
        self.select_all_btn.setFixedHeight(38)
        self.select_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.select_all_btn.clicked.connect(self.select_all_servers)
        toolbar.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setObjectName("GhostBtn")
        self.deselect_all_btn.setFixedHeight(38)
        self.deselect_all_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.deselect_all_btn.clicked.connect(self.deselect_all_servers)
        toolbar.addWidget(self.deselect_all_btn)

        self.stat_badge = StatBadge()
        toolbar.addWidget(self.stat_badge)

        self.leave_servers_btn = QPushButton(" Leave Selected")
        self.leave_servers_btn.setObjectName("DangerBtn")
        self.leave_servers_btn.setIcon(qta.icon("fa5s.sign-out-alt", color="#ffffff"))
        self.leave_servers_btn.setIconSize(QSize(14, 14))
        self.leave_servers_btn.setFixedHeight(38)
        self.leave_servers_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.leave_servers_btn.clicked.connect(self.leave_selected_servers)
        toolbar.addWidget(self.leave_servers_btn)

        layout.addLayout(toolbar)

        # Progress bar
        self.servers_progress = QProgressBar()
        self.servers_progress.setTextVisible(False)
        self.servers_progress.hide()
        layout.addWidget(self.servers_progress)

        # 4. Table Stack (Table vs Loading Overlay vs Empty State)
        self.table_stack = QStackedWidget()
        self.table_stack.setFrameShape(QFrame.NoFrame)

        self.servers_table = QTableWidget()
        self.servers_table.setColumnCount(4)
        self.servers_table.setHorizontalHeaderLabels(["", "Server Name", "Age / Member Since", "Server ID"])
        self.servers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.servers_table.setColumnWidth(0, 48)
        self.servers_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.servers_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.servers_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.servers_table.verticalHeader().setVisible(False)
        self.servers_table.setShowGrid(False)
        self.servers_table.itemChanged.connect(self.update_status)
        self.table_stack.addWidget(self.servers_table)

        self.loading_overlay = LoadingOverlay()
        self.table_stack.addWidget(self.loading_overlay)

        empty_widget = QWidget()
        empty_layout = QVBoxLayout(empty_widget)
        empty_layout.setAlignment(Qt.AlignCenter)
        self.empty_label = QLabel("No servers found.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px;")
        empty_layout.addWidget(self.empty_label)
        self.table_stack.addWidget(empty_widget)

        layout.addWidget(self.table_stack, 1)

    def set_token(self, token: str) -> None:
        self.token = token

    def fetch_data(self) -> None:
        if not self.token:
            return
        if self.servers_worker is not None and self.servers_worker.isRunning():
            self.servers_worker.cancel()

        self.table_stack.setCurrentIndex(1)
        self.loading_overlay.set_status("Fetching joined servers...")
        self.loading_overlay.set_detail("Communicating with Discord API")

        self.servers_worker = self.worker_tracker(FetchServersWorker(self.token))
        self.servers_worker.result_signal.connect(self.on_servers_fetched)
        self.servers_worker.start()

    def on_servers_fetched(self, guilds: list[dict[str, Any]], error: str) -> None:
        if not self.token:
            return
        if error:
            self.log_msg_signal.emit(f"Failed to fetch servers: {error}", "error")
            self.empty_label.setText(f"Failed to load servers: {error}")
            self.table_stack.setCurrentIndex(2)
            return

        self.servers_data = guilds
        total = len(guilds)
        owned = len([g for g in guilds if g.get("owner", False)])
        leavable = total - owned

        self.stat_total.set_value(str(total))
        self.stat_leavable.set_value(str(leavable))
        self.stat_owned.set_value(str(owned))

        self.populate_table()
        if not guilds or leavable == 0:
            self.empty_label.setText("No leavable servers found.")
            self.table_stack.setCurrentIndex(2)
        else:
            self.table_stack.setCurrentIndex(0)

    def populate_table(self) -> None:
        self.servers_table.blockSignals(True)
        self.servers_table.setRowCount(0)

        for guild in self.servers_data:
            if guild.get("owner", False):
                continue

            row = self.servers_table.rowCount()
            self.servers_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignCenter)
            self.servers_table.setItem(row, 0, chk)

            name_item = QTableWidgetItem(guild.get("name", "Unknown Server"))
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            name_item.setForeground(Qt.white)
            self.servers_table.setItem(row, 1, name_item)

            g_id = str(guild.get("id", ""))
            age_str = get_length_str(g_id)
            age_item = QTableWidgetItem(age_str)
            age_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            age_item.setForeground(Qt.lightGray)
            self.servers_table.setItem(row, 2, age_item)

            id_item = QTableWidgetItem(g_id)
            id_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            id_item.setForeground(Qt.darkGray)
            self.servers_table.setItem(row, 3, id_item)

        self.servers_table.blockSignals(False)
        self.update_status()

    def filter_servers(self, query: str) -> None:
        query = query.strip().lower()
        for row in range(self.servers_table.rowCount()):
            name = (self.servers_table.item(row, 1).text() if self.servers_table.item(row, 1) else "").lower()
            sid = (self.servers_table.item(row, 3).text() if self.servers_table.item(row, 3) else "").lower()
            match = query in name or query in sid
            self.servers_table.setRowHidden(row, not match)
        self.update_status()

    def select_all_servers(self) -> None:
        self.servers_table.blockSignals(True)
        for row in range(self.servers_table.rowCount()):
            if not self.servers_table.isRowHidden(row):
                item = self.servers_table.item(row, 0)
                if item:
                    item.setCheckState(Qt.Checked)
        self.servers_table.blockSignals(False)
        self.update_status()

    def deselect_all_servers(self) -> None:
        self.servers_table.blockSignals(True)
        for row in range(self.servers_table.rowCount()):
            item = self.servers_table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked)
        self.servers_table.blockSignals(False)
        self.update_status()

    def update_status(self) -> None:
        selected = 0
        visible = 0
        for row in range(self.servers_table.rowCount()):
            if not self.servers_table.isRowHidden(row):
                visible += 1
                item = self.servers_table.item(row, 0)
                if item and item.checkState() == Qt.Checked:
                    selected += 1

        self.stat_badge.setText(f"Selected: {selected} / {visible}")
        self.leave_servers_btn.setEnabled(selected > 0)

    def leave_selected_servers(self) -> None:
        selected_guilds: list[dict[str, Any]] = []
        for row in range(self.servers_table.rowCount()):
            chk = self.servers_table.item(row, 0)
            if chk and chk.checkState() == Qt.Checked:
                sid = self.servers_table.item(row, 3).text() if self.servers_table.item(row, 3) else ""
                name = self.servers_table.item(row, 1).text() if self.servers_table.item(row, 1) else ""
                selected_guilds.append({"id": sid, "name": name, "owner": False})

        if not selected_guilds:
            return

        reply = QMessageBox.question(
            self,
            "Confirm Leave Servers",
            f"Are you sure you want to leave {len(selected_guilds)} server(s)?\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.leave_servers_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.servers_progress.setMaximum(len(selected_guilds))
        self.servers_progress.setValue(0)
        self.servers_progress.show()

        worker = self.worker_tracker(LeaveServersWorker(self.token, selected_guilds))
        worker.progress_signal.connect(self.on_leave_progress)
        worker.finished_signal.connect(self.on_leave_finished)
        worker.start()

    def on_leave_progress(self, count: int, log_msg: str) -> None:
        self.servers_progress.setValue(count)
        msg_type = "info" if log_msg.startswith("[+]") else "error"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_leave_finished(self, success: int, failed: int) -> None:
        self.servers_progress.hide()
        self.leave_servers_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.log_msg_signal.emit(f"Server Leave Completed: {success} Left, {failed} Failed", "success" if failed == 0 else "warning")
        self.action_finished.emit()
        self.fetch_data()

    def clear(self) -> None:
        self.token = ""
        self.servers_data = []
        self.servers_table.setRowCount(0)
        self.stat_total.set_value("0")
        self.stat_leavable.set_value("0")
        self.stat_owned.set_value("0")
        self.stat_badge.setText("Selected: 0 / 0")
        self.leave_servers_btn.setEnabled(False)
