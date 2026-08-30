"""FastAPI routers.

``vision``, ``rfid``, ``episodes``, ``location`` and ``integration`` are the
upgraded subsystems. ``episodes`` is an inbound mirror written only by
WASTRAQ -- it holds no property data and makes no association decision. ``collectors`` is preserved CRUD. ``legacy`` is quarantined
demo-only proximity matching that nothing new should depend on -- and that is
never included in a WASTRAQ event.

``evidence`` is read-only clip retrieval by clip id -- the fetch half of the
EVIDENCE_READY reference WASTRAQ receives.
"""

from . import (
    collectors,
    episodes,
    evidence,
    integration,
    legacy,
    location,
    rfid,
    vision,
)

__all__ = [
    "vision",
    "rfid",
    "episodes",
    "location",
    "integration",
    "evidence",
    "collectors",
    "legacy",
]
