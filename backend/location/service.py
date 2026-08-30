"""One entry point the rest of GeoVision uses to ask "where are we?".

Holds the three providers and applies a fixed preference order:

    PHONE -> LAPTOP -> MOCK

A phone held by the picker is closer to the collection point than a laptop
in the vehicle, and a wifi-derived laptop fix is still better than a
fabricated one. Whichever answers, the caller gets the same normalised
:class:`LocationFix` with its source and accuracy attached, so uncertainty
stays visible rather than being smoothed away.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import (
    SOURCE_LAPTOP,
    SOURCE_MOCK,
    SOURCE_PHONE,
    InvalidLocation,
    LocationFix,
    validate_fix,
)
from .mock import MockLocationProvider
from .pushed import BrowserLocationProvider, PhoneLocationProvider

logger = logging.getLogger(__name__)


class LocationService:
    """Routes submissions to a provider and resolves the current best fix."""

    def __init__(
        self,
        preferred: str = "phone",
        max_age_s: float = 30.0,
        max_accuracy_m: float = 100.0,
        mock_provider: Optional[MockLocationProvider] = None,
    ) -> None:
        self.max_age_s = max_age_s
        self.max_accuracy_m = max_accuracy_m

        self.phone = PhoneLocationProvider()
        self.browser = BrowserLocationProvider()
        self.mock = mock_provider or MockLocationProvider()

        self.preferred = (preferred or "phone").lower()

    # -- ingestion --------------------------------------------------------

    def submit(
        self,
        latitude,
        longitude,
        accuracy_m=None,
        source: str = SOURCE_PHONE,
        timestamp=None,
    ) -> LocationFix:
        """Validate and store one pushed fix.

        Raises :class:`InvalidLocation` if the payload is not plausible; the
        caller turns that into an HTTP 422.
        """

        fix = validate_fix(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
            source=source,
            timestamp=timestamp,
            max_accuracy_m=self.max_accuracy_m,
        )

        if fix.source == SOURCE_PHONE:
            return self.phone.submit(fix)
        if fix.source == SOURCE_LAPTOP:
            return self.browser.submit(fix)

        raise InvalidLocation(
            f"{fix.source} fixes are generated locally and cannot be submitted."
        )

    # -- resolution -------------------------------------------------------

    def _ordered_providers(self):
        if self.preferred == "mock":
            return [self.mock, self.phone, self.browser]
        if self.preferred == "browser":
            return [self.browser, self.phone, self.mock]
        return [self.phone, self.browser, self.mock]

    def current_fix(self, allow_stale: bool = True) -> Optional[LocationFix]:
        """Best available fix under the preference order.

        A fresh fix always wins. If nothing is fresh and ``allow_stale`` is
        set, the newest stale fix is returned -- flagged as stale in
        ``to_dict`` -- because "20 seconds old" is information and silence is
        not.
        """

        providers = self._ordered_providers()

        for provider in providers:
            fix = provider.get_fix()
            if fix is not None and not fix.is_stale(self.max_age_s):
                return fix

        if not allow_stale:
            return None

        newest: Optional[LocationFix] = None
        for provider in providers:
            fix = provider.get_fix()
            if fix is None:
                continue
            if newest is None or fix.timestamp > newest.timestamp:
                newest = fix
        return newest

    def describe(self) -> dict:
        fix = self.current_fix()
        return {
            "preferred": self.preferred,
            "max_age_s": self.max_age_s,
            "max_accuracy_m": self.max_accuracy_m,
            "current": fix.to_dict(self.max_age_s) if fix else None,
            "providers": {
                "phone": self.phone.describe(),
                "browser": self.browser.describe(),
                "mock": {"source": SOURCE_MOCK, "has_fix": True},
            },
            # Stated explicitly so no consumer assumes otherwise.
            "heading_available": False,
            "imu_available": False,
            "gnss_receiver": None,
        }
