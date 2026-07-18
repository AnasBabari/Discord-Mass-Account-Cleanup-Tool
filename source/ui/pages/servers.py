from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPushButton, QProgressBar, QMessageBox, QStackedWidget, QLabel
from PyQt5.QtGui import QCursor, QColor, QBrush
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import qtawesome as qta
from ui.theme import *
from ui.components import SectionHeader, StatBadge, LoadingOverlay, get_length_str
from workers import FetchServersWorker, LeaveServersWorker

class ServersPage(QWidget):
    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.token = ""
        self.servers_data = []
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 44)
        layout.setSpacing(16)
        
        header = SectionHeader('fa5s.server', 'Servers')
        layout.addWidget(header)
        
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        self.servers_search = QLineEdit()
        self.servers_search.setPlaceholderText("Search servers...")
        self.servers_search.setFixedHeight(38)
        self.servers_search.addAction(qta.icon('fa5s.search', color=TEXT_DIM), QLineEdit.LeadingPosition)
        self.servers_search.textChanged.connect(self.filter_servers)
        top_bar.addWidget(self.servers_search)
        
        top_bar.addStretch()

        self.servers_status = StatBadge()
        self.servers_status.setText("Selected: 0 / 0")
        top_bar.addWidget(self.servers_status)
        layout.addLayout(top_bar)
        
        self.servers_table = QTableWidget(0, 4)
        self.servers_table.setHorizontalHeaderLabels(["", "Server Name", "ID", "Member Since"])
        self.servers_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.servers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.servers_table.setColumnWidth(0, 52)
        self.servers_table.setColumnWidth(3, 120)
        self.servers_table.setColumnHidden(2, True)
        self.servers_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.servers_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.servers_table.setShowGrid(False)
        self.servers_table.setAlternatingRowColors(False)
        self.servers_table.verticalHeader().setVisible(False)
        self.servers_table.verticalHeader().setDefaultSectionSize(46)
        self.servers_table.cellClicked.connect(self.servers_table_clicked)
        
        # Empty state for "no servers"
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        self.empty_label = QLabel("No leavable servers found.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-weight: 500;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch()

        # Loading overlay splash
        self.loading_overlay = LoadingOverlay()
        self.loading_overlay.set_status("Fetching servers...")
        
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.servers_table)   # index 0
        self.table_stack.addWidget(self.empty_state)     # index 1
        self.table_stack.addWidget(self.loading_overlay)  # index 2
        layout.addWidget(self.table_stack)
        
        self.servers_progress = QProgressBar()
        self.servers_progress.hide()
        layout.addWidget(self.servers_progress)
        
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.sel_all_servers_btn = QPushButton("  Select All")
        self.sel_all_servers_btn.setObjectName("GhostBtn")
        self.sel_all_servers_btn.setIcon(qta.icon('fa5s.check-double', color=ACCENT))
        self.sel_all_servers_btn.setIconSize(QSize(14, 14))
        self.sel_all_servers_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.sel_all_servers_btn.clicked.connect(self.select_all_servers)
        controls.addWidget(self.sel_all_servers_btn)
        
        controls.addStretch()
        
        self.leave_servers_btn = QPushButton("  Leave Selected")
        self.leave_servers_btn.setObjectName("DangerBtn")
        self.leave_servers_btn.setIcon(qta.icon('fa5s.sign-out-alt', color=DANGER))
        self.leave_servers_btn.setIconSize(QSize(14, 14))
        self.leave_servers_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.leave_servers_btn.clicked.connect(self.leave_selected_servers)
        controls.addWidget(self.leave_servers_btn)
        
        layout.addLayout(controls)

    def set_token(self, token):
        self.token = token

    def fetch_data(self):
        # Show the loading splash
        self.table_stack.setCurrentIndex(2)
        self.loading_overlay.set_status("Fetching servers...")
        self.loading_overlay.set_detail("")
        self.log_msg_signal.emit("Fetching server list...", "info")
        self.servers_worker = FetchServersWorker(self.token)
        self.servers_worker.finished.connect(self.servers_worker.deleteLater)
        self.servers_worker.result_signal.connect(self.on_servers_fetched)
        self.servers_worker.start()

    def on_servers_fetched(self, guilds, err):
        if err:
            self.log_msg_signal.emit(f"ERR fetching servers: {err}", "error")
            self.empty_label.setText(f"Failed to load servers: {err}")
            self.table_stack.setCurrentIndex(1)
            return
        self.servers_data = [g for g in guilds if not g.get("owner")]
        self.log_msg_signal.emit(f"Loaded {len(guilds)} servers ({len(self.servers_data)} leavable)", "success")
        if not self.servers_data:
            self.empty_label.setText("No leavable servers found.")
            self.table_stack.setCurrentIndex(1)
        else:
            # Populate table and reveal immediately (no member data fetching)
            self.populate_table()
            self.table_stack.setCurrentIndex(0)



    def populate_table(self):
        self.servers_table.setSortingEnabled(False)
        self.servers_table.setRowCount(0)
        for g in self.servers_data:
            row = self.servers_table.rowCount()
            self.servers_table.insertRow(row)
            
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Unchecked)
            self.servers_table.setItem(row, 0, cb)
            
            name_item = QTableWidgetItem(g['name'])
            name_item.setForeground(QBrush(QColor(TEXT_PRIMARY)))
            self.servers_table.setItem(row, 1, name_item)

            self.servers_table.setItem(row, 2, QTableWidgetItem(g['id']))

            length = get_length_str(g['id'], None)
            member_since_item = QTableWidgetItem(length)
            member_since_item.setToolTip("Derived from Server ID")
            member_since_item.setForeground(QBrush(QColor(TEXT_DIM)))
            member_since_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.servers_table.setItem(row, 3, member_since_item)
            
        self.servers_table.setSortingEnabled(True)
        self.update_status()

    def filter_servers(self, text):
        for i in range(self.servers_table.rowCount()):
            item = self.servers_table.item(i, 1)
            if item is None:
                continue
            name = item.text().lower()
            self.servers_table.setRowHidden(i, text.lower() not in name)

    def servers_table_clicked(self, row, col):
        if col != 0:
            item = self.servers_table.item(row, 0)
            item.setCheckState(Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked)
        self.update_status()

    def update_status(self):
        selected = sum(1 for i in range(self.servers_table.rowCount()) if self.servers_table.item(i, 0).checkState() == Qt.Checked)
        total = self.servers_table.rowCount()
        self.servers_status.setText(f"Selected: {selected} / {total}")

    def select_all_servers(self):
        visible_rows = [i for i in range(self.servers_table.rowCount()) if not self.servers_table.isRowHidden(i)]
        if not visible_rows:
            return
        target_state = Qt.Checked
        if all(self.servers_table.item(i, 0).checkState() == Qt.Checked for i in visible_rows):
            target_state = Qt.Unchecked
            
        for i in visible_rows:
            self.servers_table.item(i, 0).setCheckState(target_state)
        self.update_status()

    def leave_selected_servers(self):
        to_leave = []
        for i in range(self.servers_table.rowCount()):
            if self.servers_table.item(i, 0).checkState() == Qt.Checked:
                name = self.servers_table.item(i, 1).text()
                s_id = self.servers_table.item(i, 2).text()
                to_leave.append({"name": name, "id": s_id})
                
        if not to_leave:
            self.log_msg_signal.emit("No servers selected.", "warning")
            return
            
        reply = QMessageBox.question(self, "Confirm Action", f"Are you sure you want to leave {len(to_leave)} servers?\nThis cannot be undone.", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: return
        
        self.leave_servers_btn.setEnabled(False)
        self.servers_progress.setMaximum(len(to_leave))
        self.servers_progress.setValue(0)
        self.servers_progress.show()
        
        self.leave_worker = LeaveServersWorker(self.token, to_leave)
        self.leave_worker.finished.connect(self.leave_worker.deleteLater)
        self.leave_worker.progress_signal.connect(self.on_leave_progress)
        self.leave_worker.finished_signal.connect(self.on_leave_finished)
        self.leave_worker.start()

    def on_leave_progress(self, current, log_msg):
        self.servers_progress.setValue(current)
        msg_type = "error" if "[-] FAILED" in log_msg else "success"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_leave_finished(self, success, failed):
        self.servers_progress.hide()
        self.leave_servers_btn.setEnabled(True)
        self.action_finished.emit()
        self.fetch_data()
        
    def clear(self):
        self.servers_table.setRowCount(0)
        self.servers_data = []
        self.update_status()
