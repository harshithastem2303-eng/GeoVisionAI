"""Application singletons, wired in one place.

Routers import from here rather than constructing their own instances, and
nothing here imports a router, so the dependency graph stays acyclic.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import config
from database import collector_for_rfid
from evidence import RollingClipBuffer, retrieval_metadata
from integration import EventPublisher, WastraqClient, evidence_ready_event, iso_utc
from location import LocationService, MockLocationProvider
from vision import (
    EpisodeRegistry,
    RFIDBindingService,
    RFIDEvidenceZone,
    TrackHistory,
    WorkerRegistry,
)
from vision.pipeline import VisionPipeline

logger = logging.getLogger(__name__)

#: There is no RFID reader driver in this repository. Taps arrive over HTTP
#: from whatever bridges the physical reader (an ESP32, a serial daemon, the
#: dashboard's test button). Reported honestly through /integration/status so
#: nobody mistakes an API-only path for hardware acquisition.
RFID_HARDWARE_AVAILABLE = False
RFID_MODE = "API_INGEST_ONLY"

# --- identity -------------------------------------------------------------

worker_registry = WorkerRegistry(
    grace_s=config.BINDING_GRACE_S,
    max_age_s=config.BINDING_MAX_AGE_S,
)

track_history = TrackHistory(maxlen=300)

rfid_zone = RFIDEvidenceZone.from_tuple(config.RFID_ZONE)

#: A mirror of the episodes WASTRAQ has open, pushed here over
#: POST /episodes/active. GeoVision opens nothing itself and stores no
#: property; this exists only so a second tap from a bound collector has an
#: unambiguous subject.
episode_registry = EpisodeRegistry(max_age_s=config.EPISODE_MAX_AGE_S)

rfid_service = RFIDBindingService(
    zone=rfid_zone,
    history=track_history,
    registry=worker_registry,
    # Injected: the vision layer never imports the database.
    collector_lookup=collector_for_rfid,
    match_window_s=config.RFID_MATCH_WINDOW_S,
    min_overlap=config.RFID_MIN_OVERLAP,
    ambiguity_margin=config.RFID_AMBIGUITY_MARGIN,
    depth_margin_m=config.RFID_DEPTH_MARGIN_M,
    episodes=episode_registry,
    retap_debounce_s=config.NON_SEGREGATION_DEBOUNCE_S,
    # One physical tap is several reads. Within this window a repeat read of
    # the same card returns the binding it already made instead of being
    # mistaken for the collector's next instruction.
    bind_echo_s=config.RFID_BIND_ECHO_S,
)

# --- location -------------------------------------------------------------

location_service = LocationService(
    preferred=config.LOCATION_PROVIDER,
    max_age_s=config.LOCATION_MAX_AGE_S,
    max_accuracy_m=config.LOCATION_MAX_ACCURACY_M,
    mock_provider=MockLocationProvider(
        latitude=config.MOCK_LOCATION_LAT,
        longitude=config.MOCK_LOCATION_LON,
        accuracy_m=config.MOCK_LOCATION_ACCURACY_M,
    ),
)

# --- WASTRAQ integration --------------------------------------------------

wastraq_client = WastraqClient(
    base_url=config.WASTRAQ_BASE_URL,
    events_path=config.WASTRAQ_EVENTS_PATH,
    enabled=config.WASTRAQ_INTEGRATION_ENABLED,
    timeout_s=config.WASTRAQ_TIMEOUT_S,
)

publisher = EventPublisher(
    client=wastraq_client,
    queue_max=config.WASTRAQ_QUEUE_MAX,
    retry_backoff_s=config.WASTRAQ_RETRY_BACKOFF_S,
    max_attempts=config.WASTRAQ_MAX_ATTEMPTS,
    track_publish_hz=config.WASTRAQ_TRACK_PUBLISH_HZ,
    heartbeat_s=config.WASTRAQ_HEARTBEAT_S,
    source_id=config.SOURCE_ID,
)

# --- evidence -------------------------------------------------------------

clip_buffer = RollingClipBuffer(
    directory=config.EVIDENCE_DIR,
    buffer_s=config.EVIDENCE_BUFFER_S,
    capture_hz=config.EVIDENCE_CAPTURE_HZ,
    pre_s=config.EVIDENCE_PRE_S,
    post_s=config.EVIDENCE_POST_S,
    jpeg_quality=config.EVIDENCE_JPEG_QUALITY,
    enabled=config.EVIDENCE_ENABLED,
)


def evidence_retrieval_fields(clip) -> dict:
    """How WASTRAQ fetches this clip: ``file_url`` and what it will get.

    Empty when serving is disabled or the file cannot be resolved back inside
    the evidence directory. Announcing a URL that this node will answer 404
    for is worse than announcing none: the Mac would keep a broken promise on
    record instead of a clip with no media.
    """

    if not config.EVIDENCE_SERVE_ENABLED:
        return {}
    return retrieval_metadata(
        clip.clip_id,
        config.EVIDENCE_DIR,
        hint=clip.file_path,
        base_url=config.EVIDENCE_PUBLIC_BASE_URL,
        hash_max_bytes=config.EVIDENCE_HASH_MAX_BYTES,
    )


def publish_evidence_ready(clip) -> Optional[dict]:
    """Announce a finished clip to WASTRAQ. Returns the event, or ``None``.

    The single place EVIDENCE_READY is built, so the manual endpoint and the
    two RFID-triggered captures cannot drift into announcing different
    shapes for the same thing.

    Called from the evidence worker thread once the file is closed and
    renamed into place -- never before, because the URL in this event is live
    the moment WASTRAQ reads it. A clip that ended in any state other than
    READY is logged and not announced: there is nothing to fetch.
    """

    if clip.status != "READY" or not clip.file_path:
        logger.info(
            "Evidence clip %s finished as %s; nothing published",
            clip.clip_id,
            clip.status,
        )
        return None

    event = evidence_ready_event(
        source_id=config.SOURCE_ID,
        clip_id=clip.clip_id,
        file_path=clip.file_path,
        start_time=clip.start_time,
        end_time=clip.end_time,
        track_id=clip.track_id,
        rfid_event_id=clip.rfid_event_id,
        episode_id=getattr(clip, "episode_id", None),
        frame_count=clip.frame_count,
        session_id=clip.session_id,
        **evidence_retrieval_fields(clip),
    )
    publisher.publish(event)
    return event


# --- vision ---------------------------------------------------------------

pipeline = VisionPipeline(
    settings=config,
    registry=worker_registry,
    history=track_history,
    location_service=location_service,
    publisher=publisher,
    clip_buffer=clip_buffer,
)


# --- live integration state ----------------------------------------------


class IntegrationState:
    """The few facts /integration/status needs that nothing else owns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.last_rfid_event_at: Optional[float] = None
        self.last_rfid_event_id: Optional[str] = None
        self.last_rfid_status: Optional[str] = None

    def record_rfid(self, event_id: str, status: str, when: Optional[float] = None):
        with self._lock:
            self.last_rfid_event_at = time.time() if when is None else when
            self.last_rfid_event_id = event_id
            self.last_rfid_status = status


integration_state = IntegrationState()


def _iso_or_none(timestamp: Optional[float]) -> Optional[str]:
    return iso_utc(timestamp) if timestamp else None


def integration_status() -> dict:
    """Everything a field tester needs on one screen.

    Nothing here performs I/O against WASTRAQ: reachability is the outcome of
    the last real delivery. A status endpoint that blocks on an unreachable
    host is exactly the wrong tool during an outage.
    """

    camera = pipeline.describe_camera()
    fix = location_service.current_fix()
    gps_valid = bool(fix is not None and not fix.is_stale(location_service.max_age_s))

    return {
        "source_id": config.SOURCE_ID,
        "timestamp": iso_utc(),
        "session_id": worker_registry.session_id,
        "realsense_connected": bool(
            pipeline.camera_connected and camera.get("backend") == "realsense"
        ),
        "camera_backend": camera.get("backend"),
        "camera_running": pipeline.running,
        "tracking_active": bool(pipeline.running and pipeline.detector.loaded),
        "depth_available": pipeline.depth_available,
        "gps_valid": gps_valid,
        "gps_source": fix.source if fix else None,
        # No reader driver exists here; taps are ingested over HTTP.
        "rfid_available": RFID_HARDWARE_AVAILABLE,
        "rfid_mode": RFID_MODE,
        "wastraq_enabled": wastraq_client.configured,
        "wastraq_reachable": wastraq_client.reachable,
        "pending_events": publisher.pending(),
        "last_track_sent_at": _iso_or_none(publisher.last_track_sent_at),
        "last_event_sent_at": _iso_or_none(publisher.last_sent_at),
        "last_rfid_event_at": _iso_or_none(integration_state.last_rfid_event_at),
        "last_rfid_event_id": integration_state.last_rfid_event_id,
        "last_rfid_status": integration_state.last_rfid_status,
        "worker_bindings": len(worker_registry.bindings()),
        "active_picker_tracks": worker_registry.active_track_ids(),
        # Mirrored from WASTRAQ; zero here means WASTRAQ has not pushed any,
        # not that no collection is happening.
        "open_episodes": len(episode_registry.episodes()),
        "publisher": publisher.stats(),
        "wastraq": wastraq_client.describe(),
        "evidence": clip_buffer.stats(),
    }


publisher.status_provider = integration_status
