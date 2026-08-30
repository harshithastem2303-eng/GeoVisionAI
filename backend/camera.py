"""REPLACED -- see :mod:`vision.camera` and :mod:`vision.pipeline`.

Frame acquisition moved to ``backend/vision/camera.py``, which imports
pyrealsense2 lazily, binds an explicit serial, and offers a mock source so
the API and the tests run with no hardware. The capture loop moved to
``backend/vision/pipeline.py``.

The old module imported the serial GPS manager at module scope and printed
every detection on every frame; neither survives.

Delete this file once nothing references it::

    git rm backend/yolo_model.py backend/camera.py \
           backend/test_gps.py backend/test_property.py
"""

from __future__ import annotations

_MESSAGE = (
    "backend.camera has been replaced. Use vision.camera for frame sources "
    "and vision.pipeline for the capture loop (services.pipeline is the "
    "wired instance)."
)


def __getattr__(name: str):
    raise ImportError(f"{_MESSAGE} (tried to import {name!r})")
