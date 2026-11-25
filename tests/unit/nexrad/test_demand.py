import threading
import time

from services.nexrad.demand import DemandTracker


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_tracker_is_idle_until_the_first_ping() -> None:
    tracker = DemandTracker(900, clock=Clock())
    assert not tracker.is_active()


def test_tracker_goes_idle_after_the_window() -> None:
    clock = Clock()
    tracker = DemandTracker(900, clock=clock)
    tracker.record()
    clock.now += 899
    assert tracker.is_active()
    clock.now += 2
    assert not tracker.is_active()


def test_a_ping_wakes_an_idle_wait() -> None:
    tracker = DemandTracker(900)
    threading.Timer(0.05, tracker.record).start()
    started = time.monotonic()
    assert tracker.wait(5)
    assert time.monotonic() - started < 2


def test_pings_while_active_do_not_wake() -> None:
    tracker = DemandTracker(900)
    tracker.record()
    tracker.reset()
    tracker.record()
    assert not tracker.wait(0.01)
