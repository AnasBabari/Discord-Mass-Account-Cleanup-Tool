from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPushButton, QProgressBar, QMessageBox, QMenu, QStackedWidget, QLabel, QShortcut
from PyQt5.QtGui import QCursor, QColor, QBrush, QKeySequence
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import qtawesome as qta
from ui.theme import *
from ui.components import SectionHeader, StatBadge, LoadingOverlay, get_length_str
from workers import FetchBlockedWorker, UnblockUsersWorker

class BlockedPage(QWidget):
    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.token = ""
        self.blocked_data = []
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 44)
        layout.setSpacing(16)
        
        header = SectionHeader('fa5s.user-slash', 'Blocked Users')
        layout.addWidget(header)
        
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        self.blocked_search = QLineEdit()
        self.blocked_search.setPlaceholderText("Search blocked users...")
        self.blocked_search.setFixedHeight(38)
        self.blocked_search.addAction(qta.icon('fa5s.search', color=TEXT_DIM), QLineEdit.LeadingPosition)
        self.blocked_search.textChanged.connect(self.filter_blocked)
        top_bar.addWidget(self.blocked_search)
        
        top_bar.addStretch()

        self.blocked_status = QLabel("Selected: 0 / 0")
        self.blocked_status.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px;")
        self.blocked_status.setText("Selected: 0 / 0")
        top_bar.addWidget(self.blocked_status)
        layout.addLayout(top_bar)
        
        self.blocked_table = QTableWidget()
        self.blocked_table.setColumnCount(4)
        self.blocked_table.setHorizontalHeaderLabels(["", "User", "ID", "Global Name"])
        self.blocked_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.blocked_table.setColumnWidth(0, 40)
        self.blocked_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.blocked_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.blocked_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.blocked_table.verticalHeader().setVisible(False)
        self.blocked_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.blocked_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.blocked_table.setShowGrid(False)
        self.blocked_table.cellClicked.connect(self.blocked_table_clicked)
        self.blocked_table.verticalHeader().setVisible(False)
        self.blocked_table.verticalHeader().setDefaultSectionSize(46)
        
        # Empty state
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        self.empty_label = QLabel("No blocked users found.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-weight: 500;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch()

        # Loading overlay splash
        self.loading_overlay = LoadingOverlay()
        self.loading_overlay.set_status("Fetching blocked users...")
        
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.blocked_table)   # index 0
        self.table_stack.addWidget(self.empty_state)      # index 1
        self.table_stack.addWidget(self.loading_overlay)   # index 2
        layout.addWidget(self.table_stack)
        
        self.blocked_progress = QProgressBar()
        self.blocked_progress.hide()
        layout.addWidget(self.blocked_progress)
        
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.sel_all_blocked_btn = QPushButton("  Select All")
        self.sel_all_blocked_btn.setObjectName("GhostBtn")
        self.sel_all_blocked_btn.setIcon(qta.icon('fa5s.check-double', color=ACCENT))
        self.sel_all_blocked_btn.setIconSize(QSize(14, 14))
        self.sel_all_blocked_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.sel_all_blocked_btn.clicked.connect(self.select_all_blocked)
        controls.addWidget(self.sel_all_blocked_btn)
        
        controls.addStretch()
        
        self.unblock_btn = QPushButton("  Unblock Selected")
        self.unblock_btn.setObjectName("DangerBtn")
        self.unblock_btn.setIcon(qta.icon('fa5s.unlock', color=DANGER))
        self.unblock_btn.setIconSize(QSize(14, 14))
        self.unblock_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.unblock_btn.clicked.connect(self.unblock_selected)
        controls.addWidget(self.unblock_btn)
        
        layout.addLayout(controls)

    def set_token(self, token):
        self.token = token

    def fetch_data(self):
        # Show loading splash
        self.table_stack.setCurrentIndex(2)
        self.loading_overlay.set_status("Fetching blocked users...")
        self.log_msg_signal.emit("Fetching blocked user list...", "info")
        self.blocked_worker = FetchBlockedWorker(self.token)
        self.blocked_worker.finished.connect(self.blocked_worker.deleteLater)
        self.blocked_worker.result_signal.connect(self.on_blocked_fetched)
        self.blocked_worker.start()

    def on_blocked_fetched(self, users, err):
        if err:
            self.log_msg_signal.emit(f"ERR fetching blocked users: {err}", "error")
            self.empty_label.setText(f"Failed to load blocked users: {err}")
            self.table_stack.setCurrentIndex(1)
            return
        self.blocked_data = users
        self.log_msg_signal.emit(f"Loaded {len(self.blocked_data)} blocked users", "success")
        if not self.blocked_data:
            self.empty_label.setText("No blocked users to display.")
            self.table_stack.setCurrentIndex(1)
        else:
            self.populate_table()
            self.table_stack.setCurrentIndex(0)

    def populate_table(self):
        self.blocked_table.setSortingEnabled(False)
        self.blocked_table.setRowCount(0)
        for rel in self.blocked_data:
            row = self.blocked_table.rowCount()
            self.blocked_table.insertRow(row)
            
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Unchecked)
            self.blocked_table.setItem(row, 0, cb)
            
            user_obj = rel.get("user", {})
            username = user_obj.get("username", "Unknown")
            name_item = QTableWidgetItem(username)
            name_item.setForeground(QBrush(QColor(TEXT_PRIMARY)))
            self.blocked_table.setItem(row, 1, name_item)

            self.blocked_table.setItem(row, 2, QTableWidgetItem(rel.get("id", "")))

            global_name = user_obj.get("global_name", "")
            gn_item = QTableWidgetItem(global_name)
            gn_item.setForeground(QBrush(QColor(TEXT_PRIMARY if global_name else TEXT_DIM)))
            self.blocked_table.setItem(row, 3, gn_item)
            
        self.update_status()

    def filter_blocked(self, text):
        for i in range(self.blocked_table.rowCount()):
            item = self.blocked_table.item(i, 1)
            if item is None:
                continue
            name = item.text().lower()
            self.blocked_table.setRowHidden(i, text.lower() not in name)

    def blocked_table_clicked(self, row, col):
        if col != 0:
            item = self.blocked_table.item(row, 0)
            item.setCheckState(Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked)
        self.update_status()

    def update_status(self):
        selected = sum(1 for i in range(self.blocked_table.rowCount()) if self.blocked_table.item(i, 0).checkState() == Qt.Checked)
        total = self.blocked_table.rowCount()
        self.blocked_status.setText(f"Selected: {selected} / {total}")

    def select_all_blocked(self):
        visible_rows = [i for i in range(self.blocked_table.rowCount()) if not self.blocked_table.isRowHidden(i)]
        if not visible_rows:
            return
        target_state = Qt.Checked
        if all(self.blocked_table.item(i, 0).checkState() == Qt.Checked for i in visible_rows):
            target_state = Qt.Unchecked
            
        for i in visible_rows:
            self.blocked_table.item(i, 0).setCheckState(target_state)
        self.update_status()

    def unblock_selected(self):
        to_unblock = []
        for i in range(self.blocked_table.rowCount()):
            if self.blocked_table.item(i, 0).checkState() == Qt.Checked:
                name = self.blocked_table.item(i, 1).text()
                u_id = self.blocked_table.item(i, 2).text()
                to_unblock.append({"name": name, "id": u_id})
                
        if not to_unblock:
            self.log_msg_signal.emit("No users selected to unblock.", "warning")
            return
            
        reply = QMessageBox.question(self, "Confirm Action", f"Are you sure you want to unblock {len(to_unblock)} users?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: return
        
        self.unblock_btn.setEnabled(False)
        self.blocked_progress.setMaximum(len(to_unblock))
        self.blocked_progress.setValue(0)
        self.blocked_progress.show()
        
        self.unblock_worker = UnblockUsersWorker(self.token, to_unblock)
        self.unblock_worker.finished.connect(self.unblock_worker.deleteLater)
        self.unblock_worker.progress_signal.connect(self.on_unblock_progress)
        self.unblock_worker.finished_signal.connect(self.on_unblock_finished)
        self.unblock_worker.start()

    def on_unblock_progress(self, current, log_msg):
        self.blocked_progress.setValue(current)
        msg_type = "error" if "[-] FAILED" in log_msg else "success"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_unblock_finished(self, success, failed):
        self.blocked_progress.hide()
        self.unblock_btn.setEnabled(True)
        self.action_finished.emit()
        self.fetch_data()
        
    def clear(self):
        self.blocked_table.setRowCount(0)
        self.blocked_data = []
        self.update_status()
