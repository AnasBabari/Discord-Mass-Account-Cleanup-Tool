import threading
import time

from discord_cleanup.api.rate_limiter import RequestCoordinator


class TestRequestCoordinator:
    def test_wait_spacing(self):
        coordinator = RequestCoordinator(min_interval=0.1)
        start = time.monotonic()
        assert coordinator.wait() is True
        assert coordinator.wait() is True
        elapsed = time.monotonic() - start
        assert elapsed >= 0.08

    def test_wait_cancellation(self):
        coordinator = RequestCoordinator(min_interval=1.0)
        coordinator.backoff(10.0)
        cancel_event = threading.Event()

        def cancel_after_brief():
            time.sleep(0.05)
            cancel_event.set()

        t = threading.Thread(target=cancel_after_brief)
        t.start()
        res = coordinator.wait(cancel_event=cancel_event)
        t.join()
        assert res is False

    def test_delay_cancellation(self):
        coordinator = RequestCoordinator()
        cancel_event = threading.Event()

        def cancel_after_brief():
            time.sleep(0.05)
            cancel_event.set()

        t = threading.Thread(target=cancel_after_brief)
        t.start()
        res = coordinator.delay(10.0, cancel_event=cancel_event)
        t.join()
        assert res is False

    def test_backoff_monotonic_maximum(self):
        coordinator = RequestCoordinator()
        coordinator.backoff(5.0)
        now = time.monotonic()
        assert coordinator._backoff_until >= now + 4.8

        # Calling with smaller value should not reduce the target
        first_target = coordinator._backoff_until
        coordinator.backoff(1.0)
        assert coordinator._backoff_until >= first_target

    def test_reset(self):
        coordinator = RequestCoordinator()
        coordinator.backoff(10.0)
        coordinator.reset()
        assert coordinator._backoff_until == 0.0
        assert coordinator._next_request_time == 0.0
