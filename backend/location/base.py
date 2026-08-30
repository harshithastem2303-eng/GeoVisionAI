"""Normalised location, and the provider interface every source implements.

The rest of GeoVision consumes a :class:`LocationFix` and does not care
whether it came from a phone browser, a laptop browser or a mock.

What this module deliberately does **not** produce: heading, bearing, dead
reckoning, or a world trajectory. There is no IMU in this demo and no
dedicated GNSS receiver. A coarse latitude/longitude with an accuracy radius
is the honest limit of what is known, and downstream property association is
WASTRAQ's job to do conservatively with that uncertainty visible.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class InvalidLocation(ValueError):
    """A submitted fix failed validation and was not stored."""


#: Where a fix came from. There is intentionally no GNSS/IMU source.
SOURCE_PHONE = "PHONE"
SOURCE_LAPTOP = "LAPTOP"
SOURCE_MOCK = "MOCK"

VALID_SOURCES = {SOURCE_PHONE, SOURCE_LAPTOP, SOURCE_MOCK}


@dataclass(frozen=True)
class LocationFix:
    """One coarse position estimate."""

    latitude: float
    longitude: float
    accuracy_m: Optional[float]
    source: str
    timestamp: float

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.timestamp)

    def is_stale(self, max_age_s: float) -> bool:
        return self.age_s > max_age_s

    def to_dict(self, max_age_s: Optional[float] = None) -> dict:
        payload = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "accuracy_m": self.accuracy_m,
            "source": self.source,
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "age_s": round(self.age_s, 2),
            # No heading. No IMU exists in this demo; inventing one would be
            # a fabricated measurement.
            "heading_deg": None,
        }
        if max_age_s is not None:
            payload["stale"] = self.is_stale(max_age_s)
        return payload


def parse_timestamp(raw) -> float:
    """Accept epoch seconds, epoch milliseconds, or an ISO-8601 string."""

    if raw is None or raw == "":
        return time.time()

    if isinstance(raw, (int, float)):
        value = float(raw)
        # Browser Geolocation reports milliseconds; epoch seconds will not
        # plausibly exceed 1e11 until the year 5138.
        return value / 1000.0 if value > 1e11 else value

    if isinstance(raw, str):
        text = raw.strip()
        try:
            return float(text) if text.replace(".", "", 1).isdigit() else _iso(text)
        except (ValueError, TypeError) as exc:
            raise InvalidLocation(f"Unparseable timestamp: {raw!r}") from exc

    raise InvalidLocation(f"Unparseable timestamp: {raw!r}")


def _iso(text: str) -> float:
    normalised = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def validate_fix(
    latitude,
    longitude,
    accuracy_m=None,
    source: str = SOURCE_PHONE,
    timestamp=None,
    max_accuracy_m: Optional[float] = None,
) -> LocationFix:
    """Build a :class:`LocationFix`, rejecting anything implausible.

    Raises :class:`InvalidLocation` rather than storing a bad fix. A wrong
    position is worse than no position -- downstream association would treat
    it as real.
    """

    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise InvalidLocation("latitude and longitude must be numbers") from exc

    if lat != lat or lon != lon:  # NaN
        raise InvalidLocation("latitude/longitude must not be NaN")
    if not (-90.0 <= lat <= 90.0):
        raise InvalidLocation(f"latitude {lat} is out of range [-90, 90]")
    if not (-180.0 <= lon <= 180.0):
        raise InvalidLocation(f"longitude {lon} is out of range [-180, 180]")

    accuracy: Optional[float] = None
    if accuracy_m is not None:
        try:
            accuracy = float(accuracy_m)
        except (TypeError, ValueError) as exc:
            raise InvalidLocation("accuracy_m must be a number") from exc
        if accuracy < 0:
            raise InvalidLocation("accuracy_m must not be negative")
        if max_accuracy_m is not None and accuracy > max_accuracy_m:
            raise InvalidLocation(
                f"accuracy {accuracy} m is worse than the "
                f"{max_accuracy_m} m limit"
            )

    normalised_source = str(source or SOURCE_PHONE).upper()
    if normalised_source not in VALID_SOURCES:
        raise InvalidLocation(
            f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}"
        )

    return LocationFix(
        latitude=lat,
        longitude=lon,
        accuracy_m=accuracy,
        source=normalised_source,
        timestamp=parse_timestamp(timestamp),
    )


class LocationProvider(ABC):
    """A source of :class:`LocationFix` values."""

    #: One of the ``SOURCE_*`` constants.
    source: str = SOURCE_MOCK

    @abstractmethod
    def get_fix(self) -> Optional[LocationFix]:
        """Latest known fix, or ``None`` if nothing usable is available."""

    def describe(self) -> dict:
        fix = self.get_fix()
        return {
            "source": self.source,
            "has_fix": fix is not None,
            "fix": fix.to_dict() if fix else None,
        }
