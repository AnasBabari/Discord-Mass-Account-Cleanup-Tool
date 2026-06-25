from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QFileDialog
from PyQt5.QtGui import QCursor
from PyQt5.QtCore import Qt, QSize
import qtawesome as qta
import time
from ui.theme import *
from ui.components import SectionHeader

class LogsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)
        
        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        header = SectionHeader('fa5s.terminal', 'Terminal')
        top_bar.addWidget(header)
        
        top_bar.addStretch()
        self.export_btn = QPushButton("  Export Log")
        self.export_btn.setObjectName("GhostBtn")
        self.export_btn.setIcon(qta.icon('fa5s.file-export', color=SUCCESS))
        self.export_btn.setIconSize(QSize(13, 13))
        self.export_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.export_btn.clicked.connect(self.export_log)
        top_bar.addWidget(self.export_btn)
        
        clear_btn = QPushButton("  Clear")
        clear_btn.setObjectName("GhostBtn")
        clear_btn.setIcon(qta.icon('fa5s.trash-alt', color=ACCENT))
        clear_btn.setIconSize(QSize(13, 13))
        clear_btn.setCursor(QCursor(Qt.PointingHandCursor))
        clear_btn.clicked.connect(self.clear)
        top_bar.addWidget(clear_btn)
        
        layout.addLayout(top_bar)
        
        self.log_textbox = QTextEdit()
        self.log_textbox.setReadOnly(True)
        self.log_textbox.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_textbox.document().setMaximumBlockCount(1000)
        layout.addWidget(self.log_textbox)
        self.log_msg("Terminal initialized.", "debug")

    def log_msg(self, message, msg_type="info"):
        colors = {
            "info":    ACCENT,
            "error":   DANGER,
            "debug":   TEXT_DIM,
            "success": SUCCESS,
            "warning": WARNING,
        }
        color = colors.get(msg_type, ACCENT)
        timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
        html = f"<span style='color: {TEXT_DIM};'>{timestamp}</span> <span style='color: {color};'>{message}</span>"
        self.log_textbox.append(html)

    def clear(self):
        self.log_textbox.clear()

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Log", "cleanup_log.txt", "Text Files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.log_textbox.toPlainText())
                self.log_msg(f"Log exported to {path}", "success")
            except Exception as e:
                self.log_msg(f"Failed to export log: {e}", "error")
