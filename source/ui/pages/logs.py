import html
import time
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QFileDialog, QButtonGroup
from PyQt5.QtGui import QCursor
from PyQt5.QtCore import Qt, QSize
import qtawesome as qta
import discord_mass_cleanup as dmc
from ui.theme import ACCENT, DANGER, SUCCESS, TEXT_DIM, TEXT_PRIMARY, WARNING
from ui.components import SectionHeader


class LogsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.raw_logs = []  # list of tuples (timestamp, message, msg_type)
        self.active_filter = "all"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 32)
        layout.setSpacing(14)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(12)

        header = SectionHeader('fa5s.terminal', 'Terminal & System Logs')
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

        # ── Filter Bar ──────────────────────────────────────────────────────
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(8)

        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)

        for filter_key, filter_name, icon_name, color in [
            ("all", "All Logs", "fa5s.list", TEXT_PRIMARY),
            ("info", "Info", "fa5s.info-circle", ACCENT),
            ("success", "Success", "fa5s.check-circle", SUCCESS),
            ("error", "Errors", "fa5s.exclamation-circle", DANGER),
        ]:
            btn = QPushButton(f"  {filter_name}")
            btn.setObjectName("GhostBtn")
            btn.setIcon(qta.icon(icon_name, color=color))
            btn.setIconSize(QSize(12, 12))
            btn.setCheckable(True)
            btn.setChecked(filter_key == "all")
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.clicked.connect(lambda checked, k=filter_key: self.set_log_filter(k))
            self.filter_group.addButton(btn)
            filter_bar.addWidget(btn)

        filter_bar.addStretch()
        layout.addLayout(filter_bar)

        self.log_textbox = QTextEdit()
        self.log_textbox.setReadOnly(True)
        self.log_textbox.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_textbox.document().setMaximumBlockCount(2000)
        layout.addWidget(self.log_textbox)

        self.log_msg("Terminal initialized.", "debug")

    def set_log_filter(self, filter_key):
        self.active_filter = filter_key
        self.rebuild_log_view()

    def rebuild_log_view(self):
        self.log_textbox.clear()
        colors = {
            "info": ACCENT,
            "error": DANGER,
            "debug": TEXT_DIM,
            "success": SUCCESS,
            "warning": WARNING,
        }
        for timestamp, message, msg_type in self.raw_logs:
            if self.active_filter != "all" and msg_type != self.active_filter:
                continue
            color = colors.get(msg_type, ACCENT)
            safe_msg = html.escape(str(message))
            html_chunk = (
                f"<span style='color: {TEXT_DIM};'>{timestamp}</span> "
                f"<span style='color: {color};'>{safe_msg}</span>"
            )
            self.log_textbox.append(html_chunk)

    def log_msg(self, message, msg_type="info"):
        colors = {
            "info": ACCENT,
            "error": DANGER,
            "debug": TEXT_DIM,
            "success": SUCCESS,
            "warning": WARNING,
        }
        color = colors.get(msg_type, ACCENT)
        timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
        self.raw_logs.append((timestamp, message, msg_type))
        if self.active_filter == "all" or self.active_filter == msg_type:
            safe_msg = html.escape(str(message))
            html_chunk = (
                f"<span style='color: {TEXT_DIM};'>{timestamp}</span> "
                f"<span style='color: {color};'>{safe_msg}</span>"
            )
            self.log_textbox.append(html_chunk)

    def clear(self):
        self.raw_logs.clear()
        self.log_textbox.clear()

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Log", "cleanup_log.txt", "Text Files (*.txt)")
        if path:
            try:
                sanitized = dmc.sanitize_token(self.log_textbox.toPlainText())
                with open(path, "w", encoding="utf-8") as f:
                    f.write(sanitized)
                self.log_msg(f"Log exported to {path}", "success")
            except Exception as e:
                self.log_msg(f"Failed to export log: {e}", "error")
