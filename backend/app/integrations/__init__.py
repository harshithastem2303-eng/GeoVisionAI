"""Inbound integrations with edge devices.

Currently one: the GeoVision RealSense/RFID edge laptop. Everything here is
an OBSERVATION intake. Property association stays in `app.gis`.
"""

from .geovision import router  # noqa: F401

__all__ = ["router"]
