"""REMOVED -- this opened a hardcoded ``COM9`` serial port at import time.

Serial GNSS is no longer GeoVision's location mechanism; see
``backend/location/`` and ``backend/tests/test_location.py``.

Kept as an inert placeholder only so that a stray ``pytest backend`` does not
try to open a COM port. Delete it::

    git rm backend/test_gps.py backend/test_property.py
"""
