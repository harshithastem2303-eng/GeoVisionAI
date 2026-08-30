"""Deterministic location source for development and automated testing.

No hardware, no network, no clock surprises: given the same construction
arguments it walks the same points in the same order, so a test asserting on
position is stable.
"""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from .base import SOURCE_MOCK, LocationFix, LocationProvider


class MockLocationProvider(LocationProvider):
    """Cycles through a fixed list of coordinates."""

    source = SOURCE_MOCK

    def __init__(
        self,
        latitude: float = 12.2942090,
        longitude: float = 76.6417020,
        accuracy_m: float = 10.0,
        track: Optional[Sequence[Tuple[float, float]]] = None,
        step_deg: float = 0.00005,
        steps: int = 10,
    ) -> None:
        self.accuracy_m = accuracy_m
        if track:
            self._track: List[Tuple[float, float]] = list(track)
        else:
            # A short synthetic walk north-east from the start point.
            self._track = [
                (latitude + i * step_deg, longitude + i * step_deg)
                for i in range(max(1, steps))
            ]
        self._index = 0

    def reset(self) -> None:
        self._index = 0

    def advance(self) -> None:
        self._index = (self._index + 1) % len(self._track)

    def get_fix(self, now: Optional[float] = None) -> Optional[LocationFix]:
        latitude, longitude = self._track[self._index]
        return LocationFix(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=self.accuracy_m,
            source=SOURCE_MOCK,
            timestamp=time.time() if now is None else now,
        )
