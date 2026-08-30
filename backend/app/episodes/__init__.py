"""Collection episodes: WASTRAQ's authoritative "which house was serviced".

The engine turns a bound camera track plus surveyed service zones into an
episode, and an episode into a collection event with a segregation status.
GeoVision contributes identity, position and signals; it never contributes a
property.

    engine.py     the state machine (bind -> dwell -> episode -> close)
    transform.py  camera metres -> WGS84, through a surveyed camera pose
    store.py      the SQL
    mirror.py     the advisory episode mirror pushed to the Windows edge
    api.py        /episodes
"""

from .api import router  # noqa: F401

__all__ = ["router"]
