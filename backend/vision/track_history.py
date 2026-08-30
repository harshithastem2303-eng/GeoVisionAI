"""A short rolling history of what the tracker saw, indexed by time.

An RFID tap is an event with its own clock. Resolving it against the camera
requires the frames around that instant, not merely the newest frame -- the
tap is usually already a few hundred milliseconds old by the time the HTTP
request lands.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Iterable, List, Optional, Sequence

from .types import PersonDetection, TrackSnapshot


class TrackHistory:
    """Thread-safe bounded buffer of :class:`TrackSnapshot`.

    The camera thread appends; API request threads read. Bounded by frame
    count so memory is constant regardless of how long the run lasts.
    """

    def __init__(self, maxlen: int = 300) -> None:
        self._buffer: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(
        self,
        timestamp: float,
        detections: Iterable[PersonDetection],
    ) -> TrackSnapshot:
        snapshot = TrackSnapshot(
            timestamp=timestamp,
            detections=tuple(detections),
        )
        with self._lock:
            self._buffer.append(snapshot)
        return snapshot

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def latest(self) -> Optional[TrackSnapshot]:
        with self._lock:
            return self._buffer[-1] if self._buffer else None

    def window(self, timestamp: float, half_width: float) -> List[TrackSnapshot]:
        """Snapshots within ``+/- half_width`` seconds of ``timestamp``."""

        low = timestamp - half_width
        high = timestamp + half_width
        with self._lock:
            return [s for s in self._buffer if low <= s.timestamp <= high]

    def snapshots(self) -> Sequence[TrackSnapshot]:
        with self._lock:
            return tuple(self._buffer)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)
