"""Platform-neutral location providers.

There is no dedicated GNSS receiver and no IMU in this system. Position
comes from a phone or laptop browser's Geolocation API, pushed to the
backend over HTTP, which behaves identically on Windows and macOS.
"""

from .base import (
    SOURCE_LAPTOP,
    SOURCE_MOCK,
    SOURCE_PHONE,
    InvalidLocation,
    LocationFix,
    LocationProvider,
    validate_fix,
)
from .mock import MockLocationProvider
from .normalized import normalized_gps
from .pushed import (
    BrowserLocationProvider,
    PhoneLocationProvider,
    PushedLocationProvider,
)
from .service import LocationService

__all__ = [
    "SOURCE_PHONE",
    "SOURCE_LAPTOP",
    "SOURCE_MOCK",
    "InvalidLocation",
    "LocationFix",
    "LocationProvider",
    "validate_fix",
    "normalized_gps",
    "MockLocationProvider",
    "PhoneLocationProvider",
    "BrowserLocationProvider",
    "PushedLocationProvider",
    "LocationService",
]
