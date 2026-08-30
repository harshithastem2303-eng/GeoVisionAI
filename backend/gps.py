"""REMOVED -- serial GNSS is no longer GeoVision's location mechanism.

The previous implementation opened a hardcoded ``COM9`` serial port and
parsed a custom ``LAT:...,LON:...`` sentence. That could not run on macOS at
all, and this demo has no dedicated GNSS receiver and no IMU.

Location now comes from :mod:`location`: a phone or laptop browser's
Geolocation API, pushed to ``POST /location`` and read back from
``GET /location``. Same code path on Windows and macOS.

This file remains only so that a stale import fails with an explanation
instead of a bare ``ModuleNotFoundError``. Delete it once nothing references
it::

    git rm backend/gps.py backend/test_gps.py
"""

from __future__ import annotations

_MESSAGE = (
    "backend.gps has been removed. Serial GNSS is not GeoVision's location "
    "mechanism. Use the `location` package: POST /location from a phone or "
    "laptop browser, GET /location to read the current fix."
)


def __getattr__(name: str):
    raise ImportError(f"{_MESSAGE} (tried to import {name!r})")
