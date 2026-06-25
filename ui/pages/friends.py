from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QPushButton, QProgressBar, QMessageBox, QMenu, QStackedWidget, QLabel, QShortcut
from PyQt5.QtGui import QCursor, QColor, QBrush, QKeySequence
from PyQt5.QtCore import Qt, QSize, pyqtSignal
import qtawesome as qta
from ui.theme import *
from ui.components import SectionHeader, StatBadge, LoadingOverlay, get_length_str
from workers import FetchFriendsWorker, RemoveFriendsWorker, BlockUsersWorker

class FriendsPage(QWidget):
    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.token = ""
        self.friends_data = []
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 44)
        layout.setSpacing(16)
        
        header = SectionHeader('mdi.account-multiple', 'Friends')
        layout.addWidget(header)
        
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        self.friends_search = QLineEdit()
        self.friends_search.setPlaceholderText("Search friends...")
        self.friends_search.setFixedHeight(38)
        self.friends_search.addAction(qta.icon('fa5s.search', color=TEXT_DIM), QLineEdit.LeadingPosition)
        self.friends_search.textChanged.connect(self.filter_friends)
        top_bar.addWidget(self.friends_search)
        
        top_bar.addStretch()

        self.friends_status = StatBadge()
        self.friends_status.setText("Selected: 0 / 0")
        top_bar.addWidget(self.friends_status)
        layout.addLayout(top_bar)
        
        self.friends_table = QTableWidget(0, 5)
        self.friends_table.setHorizontalHeaderLabels(["", "Display Name", "Username", "ID", "Friends Since"])
        self.friends_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.friends_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.friends_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.friends_table.setColumnWidth(0, 52)
        self.friends_table.setColumnWidth(4, 120)
        self.friends_table.setColumnHidden(3, True)
        self.friends_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.friends_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.friends_table.setShowGrid(False)
        self.friends_table.setAlternatingRowColors(False)
        self.friends_table.verticalHeader().setVisible(False)
        self.friends_table.verticalHeader().setDefaultSectionSize(46)
        self.friends_table.cellClicked.connect(self.friends_table_clicked)
        self.friends_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.friends_table.customContextMenuRequested.connect(self.friends_context_menu)
        
        # Empty state
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        self.empty_label = QLabel("No friends found.")
        self.empty_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px; font-weight: 500;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch()

        # Loading overlay splash
        self.loading_overlay = LoadingOverlay()
        self.loading_overlay.set_status("Fetching friends...")
        
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.friends_table)    # index 0
        self.table_stack.addWidget(self.empty_state)      # index 1
        self.table_stack.addWidget(self.loading_overlay)   # index 2
        layout.addWidget(self.table_stack)
        
        QShortcut(QKeySequence("Ctrl+A"), self.friends_table, self.select_all_friends)
        QShortcut(QKeySequence("Delete"), self.friends_table, self.remove_selected_friends)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.friends_search.setFocus())
        
        self.friends_progress = QProgressBar()
        self.friends_progress.hide()
        layout.addWidget(self.friends_progress)
        
        controls = QHBoxLayout()
        controls.setSpacing(10)

        self.sel_all_friends_btn = QPushButton("  Select All")
        self.sel_all_friends_btn.setObjectName("GhostBtn")
        self.sel_all_friends_btn.setIcon(qta.icon('fa5s.check-double', color=ACCENT))
        self.sel_all_friends_btn.setIconSize(QSize(14, 14))
        self.sel_all_friends_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.sel_all_friends_btn.clicked.connect(self.select_all_friends)
        controls.addWidget(self.sel_all_friends_btn)
        
        controls.addStretch()
        
        self.block_friends_btn = QPushButton("  Block Selected")
        self.block_friends_btn.setObjectName("DangerBtn")
        self.block_friends_btn.setIcon(qta.icon('fa5s.ban', color=DANGER))
        self.block_friends_btn.setIconSize(QSize(14, 14))
        self.block_friends_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.block_friends_btn.clicked.connect(self.block_selected_users)
        controls.addWidget(self.block_friends_btn)
        
        self.remove_friends_btn = QPushButton("  Remove Selected")
        self.remove_friends_btn.setObjectName("DangerBtn")
        self.remove_friends_btn.setIcon(qta.icon('fa5s.user-times', color=DANGER))
        self.remove_friends_btn.setIconSize(QSize(14, 14))
        self.remove_friends_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.remove_friends_btn.clicked.connect(self.remove_selected_friends)
        controls.addWidget(self.remove_friends_btn)
        
        layout.addLayout(controls)

    def set_token(self, token):
        self.token = token

    def fetch_data(self):
        # Show loading splash
        self.table_stack.setCurrentIndex(2)
        self.loading_overlay.set_status("Fetching friends...")
        self.loading_overlay.set_detail("")
        self.log_msg_signal.emit("Fetching friends list...", "info")
        self.friends_worker = FetchFriendsWorker(self.token)
        self.friends_worker.finished.connect(self.friends_worker.deleteLater)
        self.friends_worker.result_signal.connect(self.on_friends_fetched)
        self.friends_worker.start()

    def on_friends_fetched(self, friends, err):
        if err:
            self.log_msg_signal.emit(f"ERR fetching friends: {err}", "error")
            self.empty_label.setText(f"Failed to load friends: {err}")
            self.table_stack.setCurrentIndex(1)
            return
        self.friends_data = friends
        self.log_msg_signal.emit(f"Loaded {len(friends)} friends", "success")
        if not self.friends_data:
            self.empty_label.setText("No friends found.")
            self.table_stack.setCurrentIndex(1)
        else:
            self.populate_table()
            # Friends data loads in a single API call so reveal immediately
            self.table_stack.setCurrentIndex(0)

    def populate_table(self):
        self.friends_table.setSortingEnabled(False)
        self.friends_table.setRowCount(0)
        for f in self.friends_data:
            row = self.friends_table.rowCount()
            self.friends_table.insertRow(row)
            
            cb = QTableWidgetItem()
            cb.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            cb.setCheckState(Qt.Unchecked)
            self.friends_table.setItem(row, 0, cb)
            
            user = f.get("user", {})
            name = user.get("global_name") or user.get("username", "Unknown")
            uname = "@" + user.get("username", "")
            
            name_item = QTableWidgetItem(name)
            name_item.setForeground(QBrush(QColor(TEXT_PRIMARY)))
            self.friends_table.setItem(row, 1, name_item)

            uname_item = QTableWidgetItem(uname)
            uname_item.setForeground(QBrush(QColor(TEXT_SECONDARY)))
            self.friends_table.setItem(row, 2, uname_item)

            self.friends_table.setItem(row, 3, QTableWidgetItem(f.get("id", "")))

            length = get_length_str(f.get("id"), f.get("since"))
            length_item = QTableWidgetItem(length)
            length_item.setToolTip(str(f.get("since", "Unknown Date")))
            length_item.setForeground(QBrush(QColor(TEXT_DIM)))
            self.friends_table.setItem(row, 4, length_item)
            
        self.friends_table.setSortingEnabled(True)
        self.update_status()

    def filter_friends(self, text):
        for i in range(self.friends_table.rowCount()):
            name_item = self.friends_table.item(i, 1)
            uname_item = self.friends_table.item(i, 2)
            if name_item is None or uname_item is None:
                continue
            name = name_item.text().lower()
            uname = uname_item.text().lower()
            self.friends_table.setRowHidden(i, text.lower() not in name and text.lower() not in uname)

    def friends_table_clicked(self, row, col):
        if col != 0:
            item = self.friends_table.item(row, 0)
            item.setCheckState(Qt.Checked if item.checkState() == Qt.Unchecked else Qt.Unchecked)
        self.update_status()

    def update_status(self):
        selected = sum(1 for i in range(self.friends_table.rowCount()) if self.friends_table.item(i, 0).checkState() == Qt.Checked)
        total = self.friends_table.rowCount()
        self.friends_status.setText(f"Selected: {selected} / {total}")

    def select_all_friends(self):
        visible_rows = [i for i in range(self.friends_table.rowCount()) if not self.friends_table.isRowHidden(i)]
        if not visible_rows:
            return
        target_state = Qt.Checked
        if all(self.friends_table.item(i, 0).checkState() == Qt.Checked for i in visible_rows):
            target_state = Qt.Unchecked
        for i in visible_rows:
            self.friends_table.item(i, 0).setCheckState(target_state)
        self.update_status()

    def friends_context_menu(self, pos):
        item = self.friends_table.itemAt(pos)
        if item is None: return
        
        row = item.row()
        menu = QMenu(self)
        
        remove_action = menu.addAction("  Remove Friend")
        remove_action.setIcon(qta.icon('fa5s.user-times', color=DANGER))
        
        block_action = menu.addAction("  Block User")
        block_action.setIcon(qta.icon('fa5s.ban', color=DANGER))
        
        action = menu.exec_(self.friends_table.viewport().mapToGlobal(pos))
        
        if action == remove_action:
            self.friends_table.item(row, 0).setCheckState(Qt.Checked)
            self.remove_selected_friends()
        elif action == block_action:
            self.friends_table.item(row, 0).setCheckState(Qt.Checked)
            self.block_selected_users()

    def remove_selected_friends(self):
        to_remove = []
        for i in range(self.friends_table.rowCount()):
            if self.friends_table.item(i, 0).checkState() == Qt.Checked:
                display_name = self.friends_table.item(i, 1).text()
                username = self.friends_table.item(i, 2).text().lstrip("@")
                f_id = self.friends_table.item(i, 3).text()
                to_remove.append({"user": {"global_name": display_name, "username": username}, "id": f_id})
                
        if not to_remove:
            self.log_msg_signal.emit("No friends selected.", "warning")
            return
            
        reply = QMessageBox.question(self, "Confirm Action", f"Are you sure you want to remove {len(to_remove)} friends?\nThis cannot be undone.", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: return
        
        self.remove_friends_btn.setEnabled(False)
        self.friends_progress.setMaximum(len(to_remove))
        self.friends_progress.setValue(0)
        self.friends_progress.show()
        
        self.remove_worker = RemoveFriendsWorker(self.token, to_remove)
        self.remove_worker.finished.connect(self.remove_worker.deleteLater)
        self.remove_worker.progress_signal.connect(self.on_remove_progress)
        self.remove_worker.finished_signal.connect(self.on_remove_finished)
        self.remove_worker.start()

    def on_remove_progress(self, current, log_msg):
        self.friends_progress.setValue(current)
        msg_type = "error" if "[-] FAILED" in log_msg else "success"
        self.log_msg_signal.emit(log_msg, msg_type)

    def on_remove_finished(self, success, failed):
        self.friends_progress.hide()
        self.remove_friends_btn.setEnabled(True)
        self.action_finished.emit()
        self.fetch_data()

    def block_selected_users(self):
        to_block = []
        for i in range(self.friends_table.rowCount()):
            if self.friends_table.item(i, 0).checkState() == Qt.Checked:
                display_name = self.friends_table.item(i, 1).text()
                username = self.friends_table.item(i, 2).text().lstrip("@")
                f_id = self.friends_table.item(i, 3).text()
                to_block.append({"user": {"global_name": display_name, "username": username}, "id": f_id})
                
        if not to_block:
            self.log_msg_signal.emit("No users selected to block.", "warning")
            return
            
        reply = QMessageBox.question(self, "Confirm Action", f"Are you sure you want to block {len(to_block)} users?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.No: return
        
        self.block_friends_btn.setEnabled(False)
        self.friends_progress.setMaximum(len(to_block))
        self.friends_progress.setValue(0)
        self.friends_progress.show()
        
        self.block_worker = BlockUsersWorker(self.token, to_block)
        self.block_worker.finished.connect(self.block_worker.deleteLater)
        self.block_worker.progress_signal.connect(self.on_remove_progress)
        self.block_worker.finished_signal.connect(self.on_block_finished)
        self.block_worker.start()

    def on_block_finished(self, success, failed):
        self.friends_progress.hide()
        self.block_friends_btn.setEnabled(True)
        self.action_finished.emit()
        self.fetch_data()
        
    def clear(self):
        self.friends_table.setRowCount(0)
        self.friends_data = []
        self.update_status()
