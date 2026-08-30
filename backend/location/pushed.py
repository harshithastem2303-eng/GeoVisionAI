"""Providers fed by HTTP pushes from a browser's Geolocation API.

Both the phone and the laptop paths are the same mechanism -- a browser
calls ``navigator.geolocation.watchPosition`` and POSTs each fix to
``/location`` -- so they share one implementation and differ only in the
source label they accept.

This is deliberately not a native mobile app and deliberately not an
OS-specific location call. Browser geolocation works identically on
Darshan's Windows machine and on macOS, which is the whole requirement.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .base import (
    SOURCE_LAPTOP,
    SOURCE_PHONE,
    LocationFix,
    LocationProvider,
)

logger = logging.getLogger(__name__)


class PushedLocationProvider(LocationProvider):
    """Holds the most recent fix pushed in over HTTP."""

    def __init__(self, source: str) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._fix: Optional[LocationFix] = None
        self._updates = 0

    def submit(self, fix: LocationFix) -> LocationFix:
        """Store a validated fix. Validation happens before this is called."""

        with self._lock:
            self._fix = fix
            self._updates += 1
        logger.debug(
            "%s fix %.6f, %.6f (+/- %s m)",
            self.source,
            fix.latitude,
            fix.longitude,
            fix.accuracy_m,
        )
        return fix

    def get_fix(self) -> Optional[LocationFix]:
        with self._lock:
            return self._fix

    def clear(self) -> None:
        with self._lock:
            self._fix = None

    @property
    def updates(self) -> int:
        with self._lock:
            return self._updates

    def describe(self) -> dict:
        payload = super().describe()
        payload["updates"] = self.updates
        return payload


class PhoneLocationProvider(PushedLocationProvider):
    """Preferred demo path: a phone browser posting its GPS fixes."""

    def __init__(self) -> None:
        super().__init__(SOURCE_PHONE)


class BrowserLocationProvider(PushedLocationProvider):
    """Fallback: the laptop's own browser geolocation (usually wifi-derived)."""

    def __init__(self) -> None:
        super().__init__(SOURCE_LAPTOP)
