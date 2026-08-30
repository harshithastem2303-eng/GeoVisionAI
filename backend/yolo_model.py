"""REPLACED -- see :mod:`vision.detector`.

The previous implementation selected the *first tracked person* as the
garbage collector::

    if self.target_id is None and track_id is not None:
        self.target_id = track_id

That is the architectural error this upgrade exists to remove. YOLO answers
"this is a person" and nothing more; worker identity comes from an RFID tap
attributed to a camera track by :mod:`vision.rfid_binding`.

It also held a single global ``TARGET_ID``, which cannot represent the two or
three collectors who work a lane at once. Concurrent bindings now live in
:class:`vision.worker_registry.WorkerRegistry`.

Person detection, BoT-SORT tracking and the person-crop writer were preserved
and moved to ``backend/vision/detector.py``.

Delete this file once nothing references it::

    git rm backend/yolo_model.py backend/camera.py \
           backend/test_gps.py backend/test_property.py
"""

from __future__ import annotations

_MESSAGE = (
    "backend.yolo_model has been replaced. Person detection and tracking are "
    "in vision.detector; worker identity is in vision.rfid_binding and "
    "vision.worker_registry. There is no TARGET_ID."
)


def __getattr__(name: str):
    raise ImportError(f"{_MESSAGE} (tried to import {name!r})")
