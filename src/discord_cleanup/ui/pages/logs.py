from __future__ import annotations

import html
from datetime import datetime

import qtawesome as qta
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.ui.components import SectionHeader
from discord_cleanup.ui.theme import (
    DANGER,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WARNING,
)


class LogsPage(QWidget):
    """Activity and diagnostic log viewer with filtering, search, and export capabilities."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.all_logs: list[tuple[str, str, str]] = []  # (timestamp, message, level)
        self.current_filter = "All"
        self.search_query = ""
        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # 1. Header
        header_row = QHBoxLayout()
        header = SectionHeader("fa5s.terminal", "Activity Logs")
        header_row.addWidget(header)
        header_row.addStretch()

        self.export_btn = QPushButton(" Export Logs")
        self.export_btn.setObjectName("GhostBtn")
        self.export_btn.setIcon(qta.icon("fa5s.download", color=TEXT_SECONDARY))
        self.export_btn.setIconSize(QSize(13, 13))
        self.export_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.export_btn.clicked.connect(self.export_logs)
        header_row.addWidget(self.export_btn)

        self.clear_btn = QPushButton(" Clear")
        self.clear_btn.setObjectName("GhostBtn")
        self.clear_btn.setIcon(qta.icon("fa5s.trash-alt", color=TEXT_SECONDARY))
        self.clear_btn.setIconSize(QSize(13, 13))
        self.clear_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.clear_btn.clicked.connect(self.clear_logs)
        header_row.addWidget(self.clear_btn)

        layout.addLayout(header_row)

        # 2. Controls (Filter + Search)
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(10)

        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["All", "Info", "Success", "Warning", "Error"])
        self.filter_combo.currentTextChanged.connect(self.on_filter_changed)
        self.filter_combo.setFixedHeight(38)
        self.filter_combo.setFixedWidth(130)
        self.filter_combo.setStyleSheet("""
            QComboBox {
                background-color: #0c141f;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                padding: 4px 12px;
                color: #f8fafc;
                font-size: 13px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #101924;
                color: #f8fafc;
                border: 1px solid rgba(255, 255, 255, 0.1);
                selection-background-color: rgba(56, 189, 248, 0.15);
            }
        """)
        ctrl_layout.addWidget(self.filter_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter logs by keyword...")
        self.search_input.textChanged.connect(self.on_search_changed)
        self.search_input.setFixedHeight(38)
        ctrl_layout.addWidget(self.search_input, 1)

        layout.addLayout(ctrl_layout)

        # 3. Log Text Area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        layout.addWidget(self.log_text, 1)

    def append_log(self, message: str, msg_type: str = "info") -> None:
        clean_msg = sanitize_token(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.all_logs.append((timestamp, clean_msg, msg_type))

        if self._should_display(msg_type, clean_msg):
            self._render_log_entry(timestamp, clean_msg, msg_type)

    def _should_display(self, msg_type: str, message: str) -> bool:
        if self.current_filter != "All" and msg_type.lower() != self.current_filter.lower():
            return False
        if self.search_query and self.search_query.lower() not in message.lower():
            return False
        return True

    def _render_log_entry(self, timestamp: str, message: str, msg_type: str) -> None:
        color_map = {
            "info": TEXT_SECONDARY,
            "success": SUCCESS,
            "warning": WARNING,
            "error": DANGER,
            "debug": TEXT_MUTED,
        }
        color = color_map.get(msg_type.lower(), TEXT_SECONDARY)
        safe_msg = html.escape(message)
        entry_html = (
            f'<div style="margin-bottom: 4px; font-family: Consolas, monospace;">'
            f'<span style="color: {TEXT_DIM};">[{timestamp}]</span> '
            f'<span style="color: {color};">{safe_msg}</span>'
            f"</div>"
        )
        self.log_text.append(entry_html)

    def on_filter_changed(self, text: str) -> None:
        self.current_filter = text
        self._rebuild_logs()

    def on_search_changed(self, text: str) -> None:
        self.search_query = text.strip()
        self._rebuild_logs()

    def _rebuild_logs(self) -> None:
        self.log_text.clear()
        for timestamp, message, msg_type in self.all_logs:
            if self._should_display(msg_type, message):
                self._render_log_entry(timestamp, message, msg_type)

    def clear_logs(self) -> None:
        self.all_logs.clear()
        self.log_text.clear()

    def export_logs(self) -> None:
        if not self.all_logs:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Activity Logs", "discord_cleanup_logs.txt", "Text Files (*.txt)")
        if not path:
            return

        lines: list[str] = []
        for timestamp, msg, level in self.all_logs:
            clean_msg = sanitize_token(msg)
            lines.append(f"[{timestamp}] [{level.upper()}] {clean_msg}")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.append_log(f"Exported {len(lines)} log lines to {path}", "success")
        except Exception as exc:
            self.append_log(f"Failed to export logs: {exc}", "error")
