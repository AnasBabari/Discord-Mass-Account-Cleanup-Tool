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
        self.pause_btn.setAccessibleName("Pause active scan")
        self.pause_btn.clicked.connect(self.pause_scan)
        self.resume_btn = QPushButton("Resume Selected")
        self.resume_btn.setAccessibleName("Resume selected scan")
        self.resume_btn.clicked.connect(self.resume_scan)
        self.recheck_btn = QPushButton("Fresh Recheck")
        self.recheck_btn.setAccessibleName("Run fresh live recheck")
        self.recheck_btn.setToolTip("Force a fresh live network check for selected or custom usernames.")
        self.recheck_btn.clicked.connect(self.recheck_selected)
        self.watchlist_add_btn = QPushButton("+ Watchlist")
        self.watchlist_add_btn.setAccessibleName("Add usernames to watchlist")
        self.watchlist_add_btn.setToolTip("Save custom or selected usernames to the persistent watchlist.")
        self.watchlist_add_btn.clicked.connect(self.add_selected_to_watchlist)
        self.reset_health_btn = QPushButton("Reset Health")
        self.reset_health_btn.setAccessibleName("Reset provider health circuit breaker")
        self.reset_health_btn.setToolTip("Clear circuit-breaker pause state while preserving active server cooldowns.")
        self.reset_health_btn.clicked.connect(self.reset_provider_health)
        self.diagnostics_btn = QPushButton("Diagnostics")
        self.diagnostics_btn.setAccessibleName("View username checker diagnostics")
        self.diagnostics_btn.clicked.connect(self.show_diagnostics_dialog)

        for button in (
            self.start_btn,
            self.pause_btn,
            self.resume_btn,
            self.recheck_btn,
            self.watchlist_add_btn,
            self.reset_health_btn,
            self.diagnostics_btn,
        ):
            controls.addWidget(button)
        layout.addLayout(controls)

        history = QHBoxLayout()
        self.history_input = QComboBox()
        self.history_input.setAccessibleName("Saved scans")
        self.history_input.setPlaceholderText("Load saved scans to resume")
        self.history_input.currentIndexChanged.connect(self.select_scan)
        self.refresh_btn = QPushButton("Load Saved Scans")
        self.refresh_btn.setAccessibleName("Load saved scans")
        self.refresh_btn.clicked.connect(self.load_scans)
        history.addWidget(self.history_input, 1)
        history.addWidget(self.refresh_btn)
        layout.addLayout(history)

        self.status_label = QLabel("Ready. Start a new scan or load saved progress.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.cooldown_label = QLabel("Source: Discord public check · no login required")
        self.cooldown_label.setWordWrap(True)
        layout.addWidget(self.cooldown_label)

        self.metrics_label = QLabel(
            "Health: Ready · Net checks: 0 · Cache hits: 0 · Deduped: 0 · Effective rate: insufficient data"
        )
        self.metrics_label.setWordWrap(True)
        layout.addWidget(self.metrics_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        toolbar = QHBoxLayout()
        self.filter_input = QComboBox()
        self.filter_input.setAccessibleName("Results filter")
        self.filter_input.addItem("Available names", True)
        self.filter_input.addItem("All results", False)
        self.filter_input.addItem("Available (fresh only)", "available_fresh")
        self.filter_input.currentIndexChanged.connect(self.reset_results)
        self.copy_btn = QPushButton("Copy Available")
        self.copy_btn.setAccessibleName("Copy available usernames")
        self.copy_btn.clicked.connect(self.copy_available)
        self.export_btn = QPushButton("Export Results CSV")
        self.export_btn.setAccessibleName("Export username results to CSV")
        self.export_btn.clicked.connect(self.export_results)
        toolbar.addWidget(self.filter_input)
        toolbar.addStretch()
        toolbar.addWidget(self.copy_btn)
        toolbar.addWidget(self.export_btn)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("Username check results table")
        self.table.setHorizontalHeaderLabels(["Username", "Status", "Checked at (UTC)", "Details / Provenance"])
        horizontal_header, vertical_header = self.table.horizontalHeader(), self.table.verticalHeader()
        assert horizontal_header is not None and vertical_header is not None
        horizontal_header.setSectionResizeMode(QHeaderView.Stretch)
        vertical_header.hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table, 1)

        pagination = QHBoxLayout()
        self.previous_btn = QPushButton("Previous")
        self.previous_btn.setAccessibleName("Previous results page")
        self.previous_btn.setEnabled(False)
        self.previous_btn.clicked.connect(lambda: self.change_page(-100))
        self.next_btn = QPushButton("Next")
        self.next_btn.setAccessibleName("Next results page")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(lambda: self.change_page(100))
        self.page_label = QLabel("0 results")
        pagination.addWidget(self.previous_btn)
        pagination.addWidget(self.page_label, 1)
        pagination.addWidget(self.next_btn)
        layout.addLayout(pagination)

        self.update_estimate()
        self.set_busy(False)
        self.cooldown_timer = QTimer(self)
        self.cooldown_timer.timeout.connect(self.update_cooldown)
        self.cooldown_timer.start(1000)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._check_live_expiry_transition()
        self.update_cooldown()

    def _check_live_expiry_transition(self) -> None:
        if (
            self.scan is not None
            and self.account_id
            and self._next_expiry_epoch is not None
            and self.clock() >= self._next_expiry_epoch
        ):
            self._next_expiry_epoch = None
            self.show_scan(refresh_results=True)

    def current_preferences(self) -> CandidatePreferences:
        return CandidatePreferences(
            strategy=str(self.strategy_input.currentData() or "exhaustive"),
            charset_mode=str(self.charset_input.currentData() or "all"),
            pattern=str(self.pattern_input.currentData() or "any"),
            seed=int(self.seed_input.value()),
        )

    def current_budget(self) -> RequestBudget | None:
        max_net = int(self.budget_requests_input.value())
        if max_net <= 0:
            return None
        return RequestBudget(max_network_requests=max_net)

    def _active_scope(self) -> str:
        if self.provider_input.currentData() == "public":
            return PUBLIC_USERNAME_SCOPE
        return self.account_id

    def update_cooldown(self) -> None:
        self._check_live_expiry_transition()
        if self.provider_input.currentData() != "public":
            self.cooldown_label.setText("Source: Signed-in Discord account")
            self._refresh_metrics_label()
            return
        self.cooldown_label.setText("Source: Discord public check · no login required")
        if self.account_id != PUBLIC_USERNAME_SCOPE:
            return
        try:
            deadline, reason = self.store().cooldown(PUBLIC_CHECK_URL)
            remaining = max(0, math.ceil(deadline - self.clock()))
            if remaining and reason == "Rate limited":
                retry = datetime.fromtimestamp(deadline, timezone.utc).strftime("%d %b %H:%M:%S UTC")
                minutes, seconds = divmod(remaining, 60)
                auto_resume = (
                    "saved across restarts · resumes automatically while open"
                    if (self.worker is not None and self.worker.isRunning())
                    else "saved across restarts"
                )
                self.cooldown_label.setText(
                    f"Public checker cooldown: {minutes}m {seconds:02}s · retry after {retry} · {auto_resume}"
                )
            self._last_cooldown_error = ""
            self._refresh_metrics_label()
        except Exception as exc:
            self.cooldown_label.setText("Saved cooldown unavailable; checks will pause safely.")
            if str(exc) != self._last_cooldown_error:
                self._last_cooldown_error = str(exc)
