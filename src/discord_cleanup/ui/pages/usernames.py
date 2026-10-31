from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CandidatePreferences,
    DiagnosticsSummary,
    ProviderHealth,
    ProviderHealthState,
    RequestBudget,
    UsernameProgress,
    UsernameScan,
)
from discord_cleanup.ui.components import SectionHeader
from discord_cleanup.ui.theme import ACCENT_MUTED, BG_CARD, BG_INPUT, SUCCESS, TEXT_DIM, TEXT_PRIMARY, TEXT_SECONDARY
from discord_cleanup.usernames.candidates import candidate_count, parse_custom_names, preview_candidates
from discord_cleanup.usernames.public_client import PUBLIC_CHECK_URL
from discord_cleanup.usernames.store import UsernameScanStore
from discord_cleanup.workers.base import CancellableTokenWorker
from discord_cleanup.workers.usernames import UsernameResultsWorker, UsernameScanWorker


class UsernamesPage(QWidget):
    """Disk-backed username search with explicit starts, smart candidate strategies, and resumable workers."""

    log_msg_signal = pyqtSignal(str, str)
    action_finished = pyqtSignal()

    def __init__(
        self,
        worker_tracker: Callable[[Any], Any] | None = None,
        parent: QWidget | None = None,
        store_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        super().__init__(parent)
        self.worker_tracker = worker_tracker or (lambda worker: worker)
        self.store_path = store_path
        self.clock = clock
        self.token = ""
        self.account_id = ""
        self.scan: UsernameScan | None = None
        self.worker: UsernameScanWorker | None = None
        self.results_worker: UsernameResultsWorker | None = None
        self._retired_workers: list[CancellableTokenWorker] = []
        self._session = 0
        self._preview_generation = 0
        self.offset = 0
        self._store: UsernameScanStore | None = None
        self._last_cooldown_error = ""
        self._last_health: ProviderHealth | None = None
        self._last_diagnostics: DiagnosticsSummary | None = None
        self._next_expiry_epoch: float | None = None
        self.init_ui()

    def store(self) -> UsernameScanStore:
        if self._store is None:
            self._store = UsernameScanStore(self.store_path, clock=self.clock)
        return self._store

    def init_ui(self) -> None:
        chevron = (Path(__file__).resolve().parents[1] / "assets" / "chevron-down.svg").as_posix()
        self.setStyleSheet(f"""
            QWidget {{ font-family: "Segoe UI"; }}
            QLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
            QComboBox, QPlainTextEdit, QSpinBox {{
                background-color: {BG_INPUT}; color: {TEXT_PRIMARY};
                border: 1px solid #243244; border-radius: 7px;
                padding: 4px 8px; font-size: 12px;
            }}
            QComboBox, QSpinBox {{ min-height: 20px; }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 8px; }}
            QComboBox QAbstractItemView {{
                background-color: {BG_CARD}; color: {TEXT_PRIMARY};
                selection-background-color: {ACCENT_MUTED}; border: 1px solid #243244;
            }}
            QCheckBox {{ color: {TEXT_SECONDARY}; font-size: 12px; spacing: 6px; }}
            QComboBox:disabled, QPlainTextEdit:disabled, QSpinBox:disabled {{ color: {TEXT_DIM}; }}
            QPushButton:disabled {{ color: {TEXT_DIM}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(6)
        layout.addWidget(SectionHeader("fa5s.search", "Username Checker"))
