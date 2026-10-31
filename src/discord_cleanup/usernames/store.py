from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as LockTimeout
from platformdirs import user_data_path

from discord_cleanup.api.exceptions import RequestCancelledError
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    CandidatePreferences,
    DiagnosticsSummary,
    ObservationOrigin,
    ProviderHealth,
    ProviderHealthState,
    RateLimitMetadata,
    RequestDiagnosticEntry,
    UsernameCheckResult,
    UsernameScan,
    UsernameStatus,
    format_utc_timestamp,
    parse_utc_timestamp,
    utc_now,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.usernames.candidates import (
    CandidatePreview,
    candidate_count,
    generator_version_for_preferences,
    preview_candidates,
    rank_candidates_by_preference,
    validate_lengths,
    validate_username,
)

MAX_DIAGNOSTICS_ROWS = 500
SCHEMA_VERSION = 3
MIGRATION_BATCH_SIZE = 500
