"""Shared test doubles: HTTP, clock, and executors. No network, no real sleeps."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, List, Optional, Tuple

from backend.http_client import HTTPClient


class FakeHTTP(HTTPClient):
    def __init__(self, responder):
        super().__init__()
        self._responder = responder

    def request(self, method, url, **kwargs):
        return self._responder(method, url, kwargs)


class FakeClock:
    """Manual clock: `wait` advances time and records the seconds requested.

    `stop_after_waits=n` makes the n-th `wait` call report a stop request.
    """

    def __init__(self, now: datetime, *, stop_after_waits: Optional[int] = None) -> None:
        self._now = now
        self.waits: List[float] = []
        self._stop_after = stop_after_waits
        self._stopped = False

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.advance(seconds)
        if self._stop_after is not None and len(self.waits) >= self._stop_after:
            self._stopped = True
        return self._stopped

    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True


class InlineExecutor:
    """Runs submitted work immediately on the calling thread."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Callable[..., Any], tuple]] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        return fn(*args)

    def shutdown(self, wait: bool = False) -> None:
        pass


class RecordingExecutor:
    """Records submitted work without running it."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Callable[..., Any], tuple]] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        return None

    def shutdown(self, wait: bool = False) -> None:
        pass
