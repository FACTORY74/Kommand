"""Thread-safe rolling request-body bandwidth measurement."""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Callable


class RollingBandwidth:
    def __init__(
        self, window_seconds: float = 5.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("Bandwidth window must be positive.")
        self.window_seconds = window_seconds
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()
        self._bytes_in_window = 0
        self._lock = threading.Lock()

    def record(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("Byte count cannot be negative.")
        if byte_count == 0:
            return
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._samples.append((now, byte_count))
            self._bytes_in_window += byte_count

    @property
    def bytes_per_second(self) -> float:
        with self._lock:
            self._expire(self._clock())
            return self._bytes_in_window / self.window_seconds

    @property
    def bytes_in_window(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return self._bytes_in_window

    def _expire(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] <= cutoff:
            _, byte_count = self._samples.popleft()
            self._bytes_in_window -= byte_count
