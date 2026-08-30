"""GeoVision API.

Picker perception and worker identity for WASTRAQ::

    camera -> YOLO detects every person
           -> BoT-SORT tracks every person
           -> the first RFID tap identifies a collector
           -> reader zone, then closest valid depth, identifies which track
           -> that track is locked and followed exclusively
           -> a later tap from the same collector flags non-segregation
           -> optional RealSense depth gives relative XYZ
           -> phone/laptop browser gives a coarse location
           -> clean worker observations for WASTRAQ to ingest
           -> rate-limited outbound events pushed to WASTRAQ

Property association is *not* done here. WASTRAQ owns the Property Master,
the surveyed PostGIS geometry and the final matching logic. No event this
service emits contains a property id.

Runs with no camera, no RFID reader and no database attached: subsystems
report their state through ``/health`` instead of preventing startup.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import config
from routes import (
    collectors,
    episodes,
    evidence,
    integration,
    legacy,
    location,
    rfid,
    vision,
)
from services import (
    clip_buffer,
    episode_registry,
    location_service,
    pipeline,
    publisher,
    rfid_service,
    wastraq_client,
    worker_registry,
)

config.configure_logging()
config.ensure_runtime_dirs()

logger = logging.getLogger("geovision")

app = FastAPI(
    title="GeoVision",
    description=(
        "Picker perception and RFID worker identity for WASTRAQ. "
        "Emits worker observations; does not perform property association."
    ),
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/captured_frames",
    StaticFiles(directory=str(config.CAPTURED_FRAMES_DIR)),
    name="captured_frames",
)

app.include_router(vision.router)
app.include_router(rfid.router)
app.include_router(episodes.router)
app.include_router(location.router)
app.include_router(integration.router)
app.include_router(evidence.router)
app.include_router(collectors.router)
app.include_router(legacy.router)


@app.get("/", tags=["health"])
def home():
    return {
        "status": "GeoVision Backend Running",
        "version": app.version,
        "source_id": config.SOURCE_ID,
        "session_id": worker_registry.session_id,
    }


@app.get("/health", tags=["health"])
def health():
    """Per-subsystem state. Nothing here raises; everything reports."""

    from database import check_connection

    return {
        "camera": pipeline.describe_camera(),
        "detector": {
            "loaded": pipeline.detector.loaded,
            "error": pipeline.detector.load_error,
            "model": config.YOLO_MODEL,
        },
        "identity": {
            "session_id": worker_registry.session_id,
            "bindings": len(worker_registry.bindings()),
            "active_picker_tracks": worker_registry.active_track_ids(),
            "zone": rfid_service.zone.to_dict(),
            "zone_configured": rfid_service.zone.is_valid(),
            "depth_margin_m": rfid_service.depth_margin_m,
        },
        "episodes": {
            # Mirrored from WASTRAQ. GeoVision opens none of its own.
            "source": "WASTRAQ",
            "open": len(episode_registry.episodes()),
            "max_age_s": episode_registry.max_age_s,
        },
        "location": location_service.describe(),
        "wastraq": wastraq_client.describe(),
        "publisher": publisher.stats(),
        "evidence": clip_buffer.stats(),
        "database": check_connection(),
        "capabilities": {
            "depth": pipeline.depth_available,
            # Stated explicitly so no consumer assumes these exist.
            "imu": False,
            "gnss_receiver": False,
            "heading": False,
            "face_recognition": False,
            # No reader driver in this repo; taps arrive over HTTP.
            "rfid_hardware": False,
        },
    }


@app.on_event("startup")
def _startup() -> None:
    """Bring up the outbound sender.

    Started unconditionally: the publisher is a no-op when the integration is
    disabled, and having the thread already running means enabling WASTRAQ is
    a restart rather than a code path nobody has exercised.
    """

    publisher.start()
    logger.info(
        "GeoVision %s ready as %s (WASTRAQ %s)",
        app.version,
        config.SOURCE_ID,
        wastraq_client.endpoint if wastraq_client.configured else "disabled",
    )


@app.on_event("shutdown")
def _shutdown() -> None:
    logger.info("Shutting down; releasing camera and outbound queue")
    clip_buffer.stop()
    publisher.stop()
    pipeline.disconnect()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
