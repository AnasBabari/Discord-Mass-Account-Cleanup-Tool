from __future__ import annotations

import threading
from PyQt5.QtCore import QThread


class CancellableTokenWorker(QThread):
    """Base QThread worker supporting cooperative cancellation and prompt token scrubbing."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Signal cooperative cancellation and immediately scrub in-memory token."""
        self._cancel_event.set()
        self.token = ""

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def scrub_token(self) -> None:
        self.token = ""
