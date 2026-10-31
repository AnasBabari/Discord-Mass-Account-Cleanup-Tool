from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import replace

from filelock import Timeout

from discord_cleanup.api.client import DiscordApiClient
from discord_cleanup.api.exceptions import (
    DiscordCleanupError,
    InvalidPayloadError,
    NetworkError,
    RequestCancelledError,
    RequestTimeoutError,
    UpstreamBlockedError,
)
from discord_cleanup.models.usernames import (
    PUBLIC_USERNAME_SCOPE,
    CacheFreshnessPolicy,
    ObservationOrigin,
    ProviderHealthState,
    RequestBudget,
    UsernameCheckResult,
    UsernameProgress,
    UsernameScan,
    UsernameStatus,
    format_utc_timestamp,
    utc_now,
)
from discord_cleanup.security.token_sanitizer import sanitize_token
from discord_cleanup.usernames.candidates import (
    SUPPORTED_GENERATOR_VERSIONS,
    next_generated_candidate,
    validate_username,
)
from discord_cleanup.usernames.public_client import (
    PUBLIC_CHECK_URL,
    PUBLIC_USERNAME_ENDPOINT,
    PublicUsernameClient,
)
from discord_cleanup.usernames.store import UsernameScanStore

logger = logging.getLogger(__name__)


class ScanBusyError(DiscordCleanupError):
    """Another process or worker is already running this saved scan."""
