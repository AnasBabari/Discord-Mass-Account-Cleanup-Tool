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
