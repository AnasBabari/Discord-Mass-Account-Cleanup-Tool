from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Any

from discord_cleanup.security.token_sanitizer import sanitize_token


class TokenRedactingFilter(logging.Filter):
    """Logging filter that automatically redacts tokens from log record messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_token(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(sanitize_token(str(a)) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: (sanitize_token(str(v)) if isinstance(v, str) else v) for k, v in record.args.items()}
        return True


class TokenRedactingFormatter(logging.Formatter):
    """Logging formatter that ensures formatted log output has all token patterns stripped."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return sanitize_token(formatted)


class CallbackLogHandler(logging.Handler):
    """Handler that forwards sanitized log lines to a callback (e.g. Qt Signal)."""

    def __init__(self, callback: Callable[[str, str], Any]):
        super().__init__()
        self.callback = callback
        self.addFilter(TokenRedactingFilter())
        self.setFormatter(TokenRedactingFormatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            if level == "warning":
                msg_type = "warning"
            elif level in ("error", "critical"):
                msg_type = "error"
            elif level == "debug":
                msg_type = "debug"
            else:
                msg_type = "info"
            self.callback(msg, msg_type)
        except Exception:
            self.handleError(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logger with token sanitization filters and standard console output."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Avoid duplicate handlers if reconfigured
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.addFilter(TokenRedactingFilter())
    console_handler.setFormatter(TokenRedactingFormatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

    root_logger.addHandler(console_handler)
