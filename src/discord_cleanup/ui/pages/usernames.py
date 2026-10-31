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
        note = QLabel(
            "Check 3- and 4-character names directly with Discord. Public checks need no login. "
            "Observations are cached safely with original check timestamps; claim names in Discord."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        settings = QHBoxLayout()
        self.provider_input = QComboBox()
        self.provider_input.setAccessibleName("Check source")
        self.provider_input.addItem("Public · no login", "public")
        self.provider_input.currentIndexChanged.connect(self.change_source)

        self.mode_input = QComboBox()
        self.mode_input.setAccessibleName("Search mode")
        self.mode_input.addItem("Exhaustive search", "generated")
        self.mode_input.addItem("Custom list", "custom")
        self.mode_input.addItem("Watchlist", "watchlist")
        self.mode_input.currentIndexChanged.connect(self.update_mode)

        self.length_input = QComboBox()
        self.length_input.setAccessibleName("Username length")
        for label, lengths in (("3 + 4 characters", (3, 4)), ("3 characters", (3,)), ("4 characters", (4,))):
            self.length_input.addItem(label, lengths)
        self.length_input.currentIndexChanged.connect(self.update_estimate)

        settings.addWidget(self.provider_input)
        settings.addWidget(self.mode_input)
        settings.addWidget(self.length_input)
        layout.addLayout(settings)

        # Row 2: Strategy, Charset, Pattern, Seed, Budget, Cache
        strategy_row = QHBoxLayout()
        self.strategy_input = QComboBox()
        self.strategy_input.setAccessibleName("Candidate strategy")
        for label, strat in (
            ("Order: Exhaustive (a-z)", "exhaustive"),
            ("Order: Randomized (seeded)", "randomized"),
            ("Order: Priority heuristic", "priority"),
        ):
            self.strategy_input.addItem(label, strat)
        self.strategy_input.currentIndexChanged.connect(self.update_estimate)

        self.charset_input = QComboBox()
        self.charset_input.setAccessibleName("Character set")
        for label, cset in (
            ("Chars: All allowed (a-z, 0-9, _ .)", "all"),
            ("Chars: Letters only (a-z)", "letters"),
            ("Chars: Alphanumeric (a-z, 0-9)", "alphanumeric"),
            ("Chars: No punctuation", "no_punctuation"),
        ):
            self.charset_input.addItem(label, cset)
        self.charset_input.currentIndexChanged.connect(self.update_estimate)

        self.pattern_input = QComboBox()
        self.pattern_input.setAccessibleName("Pattern preference")
        for label, pat in (
            ("Pattern: Any valid", "any"),
            ("Pattern: Pronounceable (CVC/CV)", "pronounceable"),
            ("Pattern: Starts with letter", "starts_with_letter"),
            ("Pattern: No edge _ or .", "no_edge_punctuation"),
            ("Pattern: Palindrome", "palindrome"),
            ("Pattern: LLL / LLLL", "LLL,LLLL"),
            ("Pattern: CVC", "CVC"),
            ("Pattern: CVCC", "CVCC"),
            ("Pattern: CVCV", "CVCV"),
            ("Pattern: LL# / LL##", "LL#,LL##"),
        ):
            self.pattern_input.addItem(label, pat)
        self.pattern_input.currentIndexChanged.connect(self.update_estimate)

        self.seed_input = QSpinBox()
        self.seed_input.setAccessibleName("Random seed")
        self.seed_input.setRange(0, 999999)
        self.seed_input.setValue(42)
        self.seed_input.setPrefix("Seed: ")
        self.seed_input.valueChanged.connect(self.update_estimate)

        self.budget_requests_input = QSpinBox()
        self.budget_requests_input.setAccessibleName("Network request budget")
        self.budget_requests_input.setRange(0, 100000)
        self.budget_requests_input.setValue(0)
        self.budget_requests_input.setSpecialValueText("Max Net: Unlimited")
        self.budget_requests_input.setPrefix("Max Net: ")

        self.use_cache_checkbox = QCheckBox("Use Cache")
        self.use_cache_checkbox.setAccessibleName("Use observation cache")
        self.use_cache_checkbox.setChecked(True)
        self.use_cache_checkbox.setToolTip("Reuse fresh cached observations without spending network requests.")

        strategy_row.addWidget(self.strategy_input)
        strategy_row.addWidget(self.charset_input)
        strategy_row.addWidget(self.pattern_input)
        strategy_row.addWidget(self.seed_input)
        strategy_row.addWidget(self.budget_requests_input)
        strategy_row.addWidget(self.use_cache_checkbox)
        layout.addLayout(strategy_row)

        self.custom_input = QPlainTextEdit()
        self.custom_input.setAccessibleName("Custom usernames")
        self.custom_input.setPlaceholderText("Paste names separated by spaces, commas, or new lines...")
        self.custom_input.setMaximumHeight(58)
        self.custom_input.hide()
        self.custom_input.textChanged.connect(self.update_estimate)
        layout.addWidget(self.custom_input)

        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        layout.addWidget(self.estimate_label)

        controls = QHBoxLayout()
        self.start_btn = QPushButton("Start New Scan")
        self.start_btn.setAccessibleName("Start new scan")
        self.start_btn.setObjectName("ActionBtn")
        self.start_btn.clicked.connect(self.start_scan)
        self.pause_btn = QPushButton("Pause")
