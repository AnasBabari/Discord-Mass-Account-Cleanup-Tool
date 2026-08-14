from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_REQUEST_DELAY = 0.6  # 600ms polite spacing between calls


class RequestCoordinator:
    """Coordinate request spacing and Discord backoff across all worker threads.

    Ensures that concurrent threads never burst the API simultaneously and
    honors global/route Retry-After headers cooperatively.
    """

    def __init__(self, min_interval: float = DEFAULT_REQUEST_DELAY):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_request_time = 0.0
        self._backoff_until = 0.0

    def wait(self, cancel_event: threading.Event | None = None) -> bool:
        """Wait until rate-limit backoff and minimum request spacing have elapsed.

        Returns True if ok to proceed, or False if cancelled.
        """
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False

            now = time.monotonic()
            with self._lock:
                target = max(self._next_request_time, self._backoff_until)
                if now >= target:
                    self._next_request_time = now + self.min_interval
                    return True
                wait_for = target - now

            sleep_time = min(wait_for, 0.05)
            if cancel_event is not None:
                if cancel_event.wait(timeout=sleep_time):
                    return False
            else:
                time.sleep(sleep_time)

    def delay(self, seconds: float, cancel_event: threading.Event | None = None) -> bool:
        """Sleep for a specified duration while checking for cancellation."""
        if seconds <= 0:
            return True
        deadline = time.monotonic() + seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            sleep_step = min(remaining, 0.05)
            if cancel_event is not None:
                if cancel_event.wait(timeout=sleep_step):
                    return False
            else:
                time.sleep(sleep_step)

    def backoff(self, seconds: float) -> None:
        """Apply a rate-limit backoff across all workers without decreasing an existing longer backoff."""
        with self._lock:
            target = time.monotonic() + max(0.0, float(seconds))
            self._backoff_until = max(self._backoff_until, target)

    def reset(self) -> None:
        """Reset internal rate-limiting timestamps (useful for isolated unit tests)."""
        with self._lock:
            self._next_request_time = 0.0
            self._backoff_until = 0.0


GLOBAL_RATE_LIMITER = RequestCoordinator()
