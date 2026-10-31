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
                self.on_error(str(exc))

    def _refresh_metrics_label(self) -> None:
        scope = self._active_scope()
        if not scope:
            return
        try:
            now = self.clock()
            route = PUBLIC_CHECK_URL if scope == PUBLIC_USERNAME_SCOPE else "/users/@me/pomelo-attempt"
            remaining_cands = max(0, self.scan.total - self.scan.checked) if self.scan else 0
            health = self._last_health or self.store().get_provider_health(scope, route=route, now=now)
            diag = self._last_diagnostics or self.store().diagnostics_summary(
                scope, remaining_candidates=remaining_cands, route=route, now=now
            )
            net_checks = self.scan.network_checks if self.scan else diag.total_network_checks
            cache_hits = self.scan.cache_hits if self.scan else diag.total_cache_hits
            deduped = self.scan.deduplicated_hits if self.scan else diag.total_deduplicated
            rate_str = (
                f"{diag.effective_checks_per_minute:.2f}/min"
                if diag.effective_checks_per_minute is not None
                else "insufficient data"
            )
            eta_part = f" · ETA: {diag.eta_label}" if self.scan and self.scan.status != "completed" else ""
            pause_part = (
                f" · Pause: {self.scan.pause_reason}"
                if (self.scan and self.scan.pause_reason and self.scan.status in ("paused", "failed"))
                else ""
            )
            self.metrics_label.setText(
                f"Health: {health.state.label} · Net checks: {net_checks:,} · Cache hits: {cache_hits:,} · "
                f"Deduped: {deduped:,} · Rate: {rate_str}{eta_part}{pause_part}"
            )
        except Exception:
            pass

    def update_mode(self) -> None:
        mode = self.mode_input.currentData()
        custom = mode == "custom"
        generated = mode == "generated"
        if custom:
            self.length_input.setCurrentIndex(0)
        self.custom_input.setVisible(custom)
        self.length_input.setEnabled(generated)
        self.charset_input.setEnabled(generated)
        self.pattern_input.setEnabled(generated)
        self.seed_input.setEnabled(generated or custom)
        self.update_estimate()

    def update_estimate(self) -> None:
        self._preview_generation += 1
        generation = self._preview_generation
        mode = self.mode_input.currentData()
        prefs = self.current_preferences()
        if mode == "custom":
            names = parse_custom_names(self.custom_input.toPlainText())
            preview = preview_candidates("custom", (3, 4), names, prefs, sample_size=6)
            if generation != self._preview_generation:
                return
            sample_str = f" · First: {', '.join(preview.sample_candidates)}" if preview.sample_candidates else ""
            self.estimate_label.setText(
                f"{len(names):,} unique entries ({preview.valid_custom_count:,} valid, "
                f"{preview.invalid_custom_count:,} invalid reported locally){sample_str}."
            )
        elif mode == "watchlist":
            try:
                scope = self._active_scope() or PUBLIC_USERNAME_SCOPE
                wl_entries = self.store().list_watchlist(scope)
                if generation != self._preview_generation:
                    return
                wl = [str(item["username"]) if isinstance(item, dict) else str(item) for item in wl_entries]
                sample = ", ".join(wl[:6])
                self.estimate_label.setText(
                    f"Watchlist: {len(wl):,} saved candidate(s){' (' + sample + ')' if sample else ''}. "
                    "Rechecks stale entries first while respecting cache freshness."
                )
            except Exception:
                if generation == self._preview_generation:
                    self.estimate_label.setText("Watchlist mode checks saved priority usernames.")
        else:
            lengths = tuple(self.length_input.currentData())
            if prefs.is_default_exhaustive():
                count = candidate_count(lengths)
                preview = preview_candidates("generated", lengths, [], prefs, sample_size=5)
                if generation != self._preview_generation:
                    return
                sample_str = ", ".join(preview.sample_candidates)
                self.estimate_label.setText(
                    f"{count:,} names using a-z, 0-9, underscores and periods (e.g. {sample_str}). "
                    "Discord sets the rate; exhaustive scans can take a long time. Cooldowns and progress are saved automatically."
                )
            else:
                preview = preview_candidates("generated", lengths, [], prefs, sample_size=6)
                if generation != self._preview_generation:
                    return
                sample_str = ", ".join(preview.sample_candidates) if preview.sample_candidates else "none"
                self.estimate_label.setText(
                    f"{preview.total_candidates:,} matching candidates ({preview.strategy_summary}) · "
                    f"Preview: {sample_str}. Cooldowns and progress are saved automatically."
                )

    def set_token(self, token: str) -> None:
        self.clear()
        self.token = token
        if token:
            self.provider_input.addItem("Signed-in account", "account")
        self.set_busy(False)

    def change_source(self) -> None:
        self._session += 1
        if self.results_worker is not None and self.results_worker.isRunning():
            self._retire_worker(self.results_worker)
        self.results_worker = None
        self.account_id = ""
        self.scan = None
        self._last_health = None
        self._last_diagnostics = None
        self._next_expiry_epoch = None
        self.history_input.clear()
        self.table.setRowCount(0)
        self.offset = 0
        self.progress.setValue(0)
        self.page_label.setText("0 results")
        self.status_label.setText("Ready. Start a new scan or load saved progress.")
        self.update_cooldown()
        self.update_estimate()
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        ready = self.provider_input.currentData() == "public" or bool(self.token)
        mode = self.mode_input.currentData()
        self.start_btn.setEnabled(ready and not busy)
        self.refresh_btn.setEnabled(ready and not busy)
        self.pause_btn.setEnabled(busy)
        self.resume_btn.setEnabled(not busy and self.scan is not None and self.scan.status != "completed")
        self.recheck_btn.setEnabled(ready and not busy)
        self.watchlist_add_btn.setEnabled(not busy)
        self.reset_health_btn.setEnabled(not busy)
        self.history_input.setEnabled(not busy)
        self.provider_input.setEnabled(not busy)
        self.mode_input.setEnabled(not busy)
        self.length_input.setEnabled(not busy and mode == "generated")
        self.strategy_input.setEnabled(not busy and mode in ("generated", "custom"))
        self.charset_input.setEnabled(not busy and mode == "generated")
        self.pattern_input.setEnabled(not busy and mode == "generated")
        self.seed_input.setEnabled(not busy and mode in ("generated", "custom"))
        self.budget_requests_input.setEnabled(not busy)
        self.use_cache_checkbox.setEnabled(not busy)
        self.custom_input.setEnabled(not busy)
        preparing_results = self.results_worker is not None and self.results_worker.isRunning()
        self.copy_btn.setEnabled(self.scan is not None and not preparing_results)
        self.export_btn.setEnabled(self.scan is not None and not preparing_results)

    def _launch(
        self,
        mode: str,
        scan_id: str | None = None,
        *,
        force_recheck: bool = False,
        recheck_names: list[str] | None = None,
    ) -> None:
        public = self.provider_input.currentData() == "public"
        if (not public and not self.token) or (self.worker is not None and self.worker.isRunning()):
            return
        session = self._session
        prefs = self.current_preferences()
        budget = self.current_budget()
        use_cache = bool(self.use_cache_checkbox.isChecked()) and not force_recheck
        worker_kwargs: dict[str, Any] = {}
        if not prefs.is_default_exhaustive():
            worker_kwargs["preferences"] = prefs
        if budget is not None:
            worker_kwargs["budget"] = budget
        if not use_cache:
            worker_kwargs["use_cache"] = False
        if force_recheck:
            worker_kwargs["force_recheck"] = True
        if recheck_names is not None:
            worker_kwargs["recheck_names"] = recheck_names
        if self.clock is not time.time:
            worker_kwargs["clock"] = self.clock

        worker = UsernameScanWorker(
            "" if public else self.token,
            mode,
            tuple(self.length_input.currentData()),
            self.custom_input.toPlainText(),
            scan_id,
            self.store_path,
            access="public" if public else "account",
            **worker_kwargs,
        )
        self.worker = self.worker_tracker(worker)

        def current() -> bool:
            return session == self._session and self.worker is worker

        worker.account_ready.connect(lambda account, scans: self.on_account(account, scans) if current() else None)
        worker.progress_signal.connect(lambda progress: self.on_progress(progress) if current() else None)
        worker.completed_signal.connect(lambda scan: self.on_completed(scan) if current() else None)
        worker.error_signal.connect(lambda message: self.on_error(message) if current() else None)
        worker.finished.connect(lambda: self.set_busy(False) if current() else None)
        self.set_busy(True)
        self.status_label.setText(
            "Loading public scans..." if public else "Verifying account and loading saved progress..."
        )
        worker.start()

    def load_scans(self) -> None:
        self._launch("list")

    def start_scan(self) -> None:
        mode = str(self.mode_input.currentData())
        if mode == "custom" and not parse_custom_names(self.custom_input.toPlainText()):
            self.status_label.setText("Enter at least one username to check.")
            return
        if mode == "watchlist":
            scope = self._active_scope() or PUBLIC_USERNAME_SCOPE
            if not self.store().list_watchlist(scope):
                self.status_label.setText("Watchlist is empty. Add usernames to the watchlist first.")
                return
        if mode == "generated":
            prefs = self.current_preferences()
            if not prefs.is_default_exhaustive():
                preview = preview_candidates("generated", tuple(self.length_input.currentData()), [], prefs, sample_size=1)
                if preview.total_candidates == 0:
                    self.status_label.setText("No candidates match the selected character set and pattern.")
                    return
        self._launch(mode)

    def resume_scan(self) -> None:
        if self.scan is not None:
            self._launch("resume", self.scan.id)

    def _selected_table_usernames(self) -> list[str]:
        selected: list[str] = []
        sel_model = self.table.selectionModel()
        for idx in sel_model.selectedRows() if sel_model is not None else []:
            item = self.table.item(idx.row(), 0)
            if item is not None and item.text():
                selected.append(item.text().strip().lower())
        return selected

    def recheck_selected(self) -> None:
        names = self._selected_table_usernames()
        if not names:
            names = parse_custom_names(self.custom_input.toPlainText())
        if not names and self.scan is not None and self.account_id:
            rows, _ = self.store().results(
                self.scan.id, self.account_id, available_only=True, offset=0, limit=25, now=self.clock()
            )
            names = [r.username for r in rows]
        if not names:
            self.status_label.setText("Select rows in the table or enter custom names to run a fresh recheck.")
            return
        self._launch("recheck", force_recheck=True, recheck_names=names)

    def add_selected_to_watchlist(self) -> None:
        scope = self._active_scope() or PUBLIC_USERNAME_SCOPE
        names = self._selected_table_usernames() or parse_custom_names(self.custom_input.toPlainText())
        if not names:
            self.status_label.setText("Select rows in the table or paste custom names to add to the watchlist.")
            return
        try:
            added = self.store().add_watchlist(scope, names)
            total = len(self.store().list_watchlist(scope))
            self.status_label.setText(f"Added {len(added):,} username(s) to watchlist ({total:,} total saved).")
            self.update_estimate()
        except Exception as exc:
            self.on_error(str(exc))

    def reset_provider_health(self) -> None:
        scope = self._active_scope() or PUBLIC_USERNAME_SCOPE
        route = PUBLIC_CHECK_URL if scope == PUBLIC_USERNAME_SCOPE else "/users/@me/pomelo-attempt"
        try:
            health = self.store().reset_provider_health(scope, route=route, now=self.clock())
            self._last_health = health
            self._refresh_metrics_label()
            if health.state == ProviderHealthState.WAITING_COOLDOWN:
                self.status_label.setText("Circuit breaker cleared; active server cooldown remains enforced.")
            else:
                self.status_label.setText("Provider health reset to Ready. Active server cooldowns are preserved.")
        except Exception as exc:
            self.on_error(str(exc))

    def show_diagnostics_dialog(self) -> None:
        scope = self._active_scope() or PUBLIC_USERNAME_SCOPE
        route = PUBLIC_CHECK_URL if scope == PUBLIC_USERNAME_SCOPE else "/users/@me/pomelo-attempt"
        remaining_cands = max(0, self.scan.total - self.scan.checked) if self.scan else 0
        try:
            summary = self.store().diagnostics_summary(
                scope, remaining_candidates=remaining_cands, route=route, now=self.clock()
            )
            recent = self.store().recent_diagnostics(scope, limit=25)
        except Exception as exc:
            self.on_error(str(exc))
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Username Checker Diagnostics")
        dialog.resize(680, 440)
        d_layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)
        rate_str = (
            f"{summary.effective_checks_per_minute:.2f} checks/min"
            if summary.effective_checks_per_minute is not None
            else "insufficient data"
        )
        lines = [
            f"Scope / Provider: {scope}",
            f"Provider Health: {summary.provider_health.state.label} ({summary.provider_health.reason or 'OK'})",
            f"Total Network Requests: {summary.network_requests_total:,}",
            f"Definitive Network Checks: {summary.definitive_network_checks:,}",
            f"Total Cache Hits: {summary.total_cache_hits:,}",
            f"Total Deduplicated Requests Avoided: {summary.total_deduplicated:,}",
            f"Total Local Invalid Rejections: {summary.total_local_invalid:,}",
            f"Cumulative Cooldown/Backoff Wait: {summary.total_wait_seconds:.1f}s",
            f"Recent Definitive Successes (last 50): {summary.recent_success_count}",
            f"Recent HTTP 429 Rate Limits (last 50): {summary.recent_rate_limit_count}",
            f"Recent Failures / Challenges (last 50): {summary.recent_failure_count}",
            f"Effective Live Network Rate: {rate_str}",
            f"Realistic Remaining ETA: {summary.eta_label}",
            "",
            "--- Recent Diagnostic Events (Sanitized) ---",
        ]
        for entry in recent:
            lines.append(
                f"[{entry.completed_at}] {entry.username or '-'} | {entry.origin} | "
                f"{entry.classification} | HTTP {entry.http_status or '-'} | "
                f"wait={entry.wait_seconds:.1f}s | {entry.detail}"
            )
        text.setPlainText("\n".join(lines))
        d_layout.addWidget(text)
        close_btn = QPushButton("Close", dialog)
        close_btn.clicked.connect(dialog.accept)
        d_layout.addWidget(close_btn)
        dialog.exec_()

    def pause_scan(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.pause_btn.setEnabled(False)
            self.status_label.setText("Pausing; waiting for any active request to finish...")

    def on_account(self, account_id: str, scans: list[UsernameScan]) -> None:
        self.account_id = account_id
        self.update_cooldown()
        selected = self.scan.id if self.scan else None
        self.history_input.blockSignals(True)
        self.history_input.clear()
        for scan in scans:
            self.history_input.addItem(
                f"{scan.created_at} · {scan.mode} · {scan.checked:,}/{scan.total:,} · {scan.status}", scan.id
            )
        index = self.history_input.findData(selected)
        self.history_input.setCurrentIndex(max(0, index) if scans else -1)
        self.history_input.blockSignals(False)
        self.select_scan()
