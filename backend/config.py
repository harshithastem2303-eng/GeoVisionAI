"""
GeoVision configuration.

Every tunable lives here and every tunable is overridable from the
environment (or a local ``.env``), so the same checkout runs unchanged on
Windows and macOS. No COM ports, no absolute paths, no secrets in source.

Import this module rather than reading ``os.environ`` from feature code.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple

# --------------------------------------------------------------------------
# Paths (platform neutral)
# --------------------------------------------------------------------------

BASE_DIR: Path = Path(__file__).resolve().parent
REPO_ROOT: Path = BASE_DIR.parent


# --------------------------------------------------------------------------
# .env loading (dependency free)
# --------------------------------------------------------------------------


def _load_env_file(path: Path) -> None:
    """Populate ``os.environ`` from a ``KEY=value`` file.

    Deliberately dependency free -- adding python-dotenv would be one more
    thing to install on two different operating systems. Existing environment
    variables always win, so a real shell export overrides the file.
    """

    if not path.is_file():
        return

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")
_load_env_file(REPO_ROOT / ".env")


# --------------------------------------------------------------------------
# Typed environment readers
# --------------------------------------------------------------------------


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(env_str(name, str(default)))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list:
    raw = env_str(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOG_LEVEL: str = env_str("GEOVISION_LOG_LEVEL", "INFO").upper()

#: When false, the per-frame detection dump is suppressed. Frame-rate logging
#: is a debugging tool, not production behaviour.
VERBOSE_FRAME_LOGGING: bool = env_bool("GEOVISION_VERBOSE_FRAMES", False)


def configure_logging() -> None:
    """Idempotent root-logger setup. Safe to call from any entry point."""

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

HOST: str = env_str("GEOVISION_HOST", "0.0.0.0")
PORT: int = env_int("GEOVISION_PORT", 8000)

CORS_ORIGINS: list = env_list(
    "GEOVISION_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------

#: ``realsense`` uses pyrealsense2. ``mock`` synthesises frames so the API,
#: the tests and the frontend can run with nothing plugged in.
CAMERA_BACKEND: str = env_str("GEOVISION_CAMERA_BACKEND", "realsense").lower()

CAMERA_WIDTH: int = env_int("GEOVISION_CAMERA_WIDTH", 640)
CAMERA_HEIGHT: int = env_int("GEOVISION_CAMERA_HEIGHT", 480)
CAMERA_FPS: int = env_int("GEOVISION_CAMERA_FPS", 30)

#: Explicit RealSense serial. Startup binds this serial rather than guessing
#: when more than one camera is attached. Empty means "discover, and refuse
#: to substitute if the choice is ambiguous".
VISION_SERIAL: str = env_str("GEOVISION_VISION_SERIAL", "")

ENABLE_COLOR: bool = env_bool("GEOVISION_ENABLE_COLOR", True)
ENABLE_DEPTH: bool = env_bool("GEOVISION_ENABLE_DEPTH", True)

#: Seconds to wait for a coherent frameset before treating the read as failed.
FRAME_TIMEOUT_MS: int = env_int("GEOVISION_FRAME_TIMEOUT_MS", 5000)


# --------------------------------------------------------------------------
# Detection / tracking
# --------------------------------------------------------------------------

YOLO_MODEL: str = env_str("GEOVISION_YOLO_MODEL", "yolo11n.pt")
YOLO_CONFIDENCE: float = env_float("GEOVISION_YOLO_CONFIDENCE", 0.40)
TRACKER_CONFIG: str = env_str("GEOVISION_TRACKER", "botsort.yaml")

#: Person crops were a dataset-collection tool. Off by default -- writing a
#: JPEG per track per second into the repository is not production behaviour.
SAVE_PERSON_CROPS: bool = env_bool("GEOVISION_SAVE_PERSON_CROPS", False)
PERSON_CROP_DIR: Path = Path(
    env_str("GEOVISION_PERSON_CROP_DIR", str(BASE_DIR / "person_crops"))
)
PERSON_CROP_INTERVAL_S: float = env_float("GEOVISION_CROP_INTERVAL_S", 1.0)
PERSON_CROP_MIN_CONFIDENCE: float = env_float("GEOVISION_CROP_MIN_CONF", 0.60)
PERSON_CROP_MAX_PER_ID: int = env_int("GEOVISION_CROP_MAX_PER_ID", 50)


# --------------------------------------------------------------------------
# RFID evidence zone
# --------------------------------------------------------------------------
#
# A rectangle in *image* coordinates covering the physical RFID reader. A
# person who taps the reader necessarily occupies this region of the frame,
# which is what lets a tap be attributed to a camera track.
#
# Configure as either GEOVISION_RFID_ZONE="x1,y1,x2,y2" or the four
# individual variables. Defaults describe a centre-bottom region of a
# 640x480 frame and must be re-surveyed for the real installation.

_DEFAULT_ZONE: Tuple[int, int, int, int] = (220, 240, 420, 470)


def _parse_zone() -> Tuple[int, int, int, int]:
    combined = env_str("GEOVISION_RFID_ZONE", "")
    if combined:
        parts = [p.strip() for p in combined.split(",")]
        if len(parts) == 4:
            try:
                return (
                    int(float(parts[0])),
                    int(float(parts[1])),
                    int(float(parts[2])),
                    int(float(parts[3])),
                )
            except ValueError:
                pass

    return (
        env_int("GEOVISION_RFID_ZONE_X1", _DEFAULT_ZONE[0]),
        env_int("GEOVISION_RFID_ZONE_Y1", _DEFAULT_ZONE[1]),
        env_int("GEOVISION_RFID_ZONE_X2", _DEFAULT_ZONE[2]),
        env_int("GEOVISION_RFID_ZONE_Y2", _DEFAULT_ZONE[3]),
    )


RFID_ZONE: Tuple[int, int, int, int] = _parse_zone()

#: How far either side of the tap timestamp to look for tracked people.
RFID_MATCH_WINDOW_S: float = env_float("GEOVISION_RFID_MATCH_WINDOW_S", 2.0)

#: Minimum fraction of a person's bounding box that must fall inside the
#: evidence zone before that track is even a candidate.
RFID_MIN_OVERLAP: float = env_float("GEOVISION_RFID_MIN_OVERLAP", 0.15)

#: The best candidate must beat the runner-up by at least this much overlap,
#: otherwise the tap is reported AMBIGUOUS and nothing is bound. Used by the
#: zone fallback rule, i.e. when no depth is available at all.
RFID_AMBIGUITY_MARGIN: float = env_float("GEOVISION_RFID_AMBIGUITY_MARGIN", 0.20)

#: The person who tapped is the person closest to the camera. They must be at
#: least this many metres nearer than the next person before that is asserted;
#: two people shoulder to shoulder at the reader stay AMBIGUOUS. Widen it if
#: the demo lane is crowded, narrow it if depth is clean and people queue.
RFID_DEPTH_MARGIN_M: float = env_float("GEOVISION_RFID_DEPTH_MARGIN_M", 0.5)

#: An RC522 keeps returning the same UID while the card is held on it, and the
#: bridge polls faster than a human lifts a card. Within this many seconds of a
#: binding being made, another read of the *same* card by the *same* collector
#: is the same tap arriving twice: it returns the binding that already exists
#: and changes nothing. Beyond it, a tap from a bound collector carries its
#: normal second meaning. Keep it comfortably under the time it takes to walk
#: to a bin and decide the waste is not segregated.
RFID_BIND_ECHO_S: float = env_float("GEOVISION_RFID_BIND_ECHO_S", 2.0)


# --------------------------------------------------------------------------
# Collection episodes (mirrored from WASTRAQ)
# --------------------------------------------------------------------------
#
# GeoVision does not decide which property is being serviced and does not
# open episodes of its own. WASTRAQ pushes an open episode to
# POST /episodes/active and closes it again; this service only needs to know
# *that* a bound track has a collection in progress, so a second RFID tap has
# a subject. No property id is accepted or stored.

#: How long a pushed episode stays live without an update. If WASTRAQ stops
#: talking mid-lane the mirror must go quiet rather than sit here waiting to
#: absorb a tap an hour later and flag the wrong house.
EPISODE_MAX_AGE_S: float = env_float("GEOVISION_EPISODE_MAX_AGE_S", 180.0)

#: A card held a moment too long, a bouncing reader or a retried POST must
#: raise one trigger, not several.
NON_SEGREGATION_DEBOUNCE_S: float = env_float(
    "GEOVISION_NON_SEGREGATION_DEBOUNCE_S", 10.0
)


# --------------------------------------------------------------------------
# Worker binding lifecycle
# --------------------------------------------------------------------------

#: A binding survives this long without the track being seen. Covers walking
#: behind a vehicle; does not cover going home for the day.
BINDING_GRACE_S: float = env_float("GEOVISION_BINDING_GRACE_S", 20.0)

#: Absolute ceiling on a binding's life, seen or not.
BINDING_MAX_AGE_S: float = env_float("GEOVISION_BINDING_MAX_AGE_S", 3600.0)


# --------------------------------------------------------------------------
# Depth
# --------------------------------------------------------------------------

DEPTH_ENABLED: bool = env_bool("GEOVISION_DEPTH_ENABLED", True)

#: Half-width of the square neighbourhood sampled around the representative
#: image point. A single depth pixel is frequently a hole.
DEPTH_PATCH_RADIUS: int = env_int("GEOVISION_DEPTH_PATCH_RADIUS", 4)

DEPTH_MIN_M: float = env_float("GEOVISION_DEPTH_MIN_M", 0.2)
DEPTH_MAX_M: float = env_float("GEOVISION_DEPTH_MAX_M", 12.0)


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------

#: phone | browser | mock -- which provider /location reads from by default.
LOCATION_PROVIDER: str = env_str("GEOVISION_LOCATION_PROVIDER", "phone").lower()

#: A fix older than this is reported as stale rather than current.
LOCATION_MAX_AGE_S: float = env_float("GEOVISION_LOCATION_MAX_AGE_S", 30.0)

#: Fixes worse than this accuracy are rejected outright.
LOCATION_MAX_ACCURACY_M: float = env_float("GEOVISION_LOCATION_MAX_ACCURACY_M", 100.0)

MOCK_LOCATION_LAT: float = env_float("GEOVISION_MOCK_LAT", 12.2942090)
MOCK_LOCATION_LON: float = env_float("GEOVISION_MOCK_LON", 76.6417020)
MOCK_LOCATION_ACCURACY_M: float = env_float("GEOVISION_MOCK_ACCURACY_M", 10.0)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

DB_HOST: str = env_str("GEOVISION_DB_HOST", "localhost")
DB_PORT: int = env_int("GEOVISION_DB_PORT", 5432)
DB_NAME: str = env_str("GEOVISION_DB_NAME", "geovision")
DB_USER: str = env_str("GEOVISION_DB_USER", "geovision_user")

#: No default. An unset password fails loudly at first use rather than
#: silently shipping a credential in source control.
DB_PASSWORD: Optional[str] = os.environ.get("GEOVISION_DB_PASSWORD")


# --------------------------------------------------------------------------
# Source identity
# --------------------------------------------------------------------------
#
# Every observation GeoVision emits carries the id of the sensor that produced
# it. WASTRAQ uses these to tell one edge node from another; they are not
# secrets and they are not derived from anything, so they are plain config.

SOURCE_ID: str = env_str(
    "WASTRAQ_SOURCE_ID",
    env_str("GEOVISION_SOURCE_ID", "GEOVISION-D455-01"),
)
GPS_SOURCE_ID: str = env_str("GEOVISION_GPS_SOURCE_ID", "GEOVISION-GPS-01")
RFID_SOURCE_ID: str = env_str("GEOVISION_RFID_SOURCE_ID", "GEOVISION-RFID-01")


# --------------------------------------------------------------------------
# WASTRAQ outbound integration
# --------------------------------------------------------------------------
#
# GeoVision pushes observations to WASTRAQ; WASTRAQ decides what property they
# belong to. No IP address is ever hard-coded -- the laptop running WASTRAQ
# changes address between demos, so it is configuration, not source.

#: e.g. http://192.168.1.23:8000 -- no trailing slash needed.
WASTRAQ_BASE_URL: str = env_str("WASTRAQ_BASE_URL", "").rstrip("/")

#: Off unless explicitly turned on. A checkout with no .env sends nothing.
WASTRAQ_INTEGRATION_ENABLED: bool = env_bool("WASTRAQ_INTEGRATION_ENABLED", False)

WASTRAQ_EVENTS_PATH: str = env_str(
    "WASTRAQ_EVENTS_PATH", "/integrations/geovision/events"
)

#: Short. An unreachable WASTRAQ must never hold a sender thread for long.
WASTRAQ_TIMEOUT_S: float = env_float("WASTRAQ_TIMEOUT_S", 2.0)

#: Outbound TRACK_UPDATE rate *per track*. Camera tracking stays at full FPS;
#: only the HTTP publishing is throttled. 0 disables track publishing.
WASTRAQ_TRACK_PUBLISH_HZ: float = env_float("WASTRAQ_TRACK_PUBLISH_HZ", 5.0)

#: Bounded in-memory retry buffer. When it is full the OLDEST event is
#: dropped -- a stale position is worth less than a fresh one.
WASTRAQ_QUEUE_MAX: int = env_int("WASTRAQ_QUEUE_MAX", 500)

#: Wait this long before retrying a failed event.
WASTRAQ_RETRY_BACKOFF_S: float = env_float("WASTRAQ_RETRY_BACKOFF_S", 5.0)

#: Give up on an individual event after this many failed attempts.
WASTRAQ_MAX_ATTEMPTS: int = env_int("WASTRAQ_MAX_ATTEMPTS", 5)

#: Optional liveness beat, in seconds. 0 disables it.
WASTRAQ_HEARTBEAT_S: float = env_float("WASTRAQ_HEARTBEAT_S", 0.0)

#: Attach the current coarse fix to TRACK_UPDATE events. The fix stays in its
#: own "gps" object -- it is never merged into the tracker observation.
WASTRAQ_INCLUDE_GPS: bool = env_bool("WASTRAQ_INCLUDE_GPS", True)


# --------------------------------------------------------------------------
# Evidence clip buffer
# --------------------------------------------------------------------------
#
# A rolling buffer of recent annotated frames, kept JPEG-encoded so memory
# stays bounded and small. Raw 640x480x3 at 30 FPS for 20 s would be ~550 MB;
# JPEG at EVIDENCE_CAPTURE_HZ is a few MB.

EVIDENCE_ENABLED: bool = env_bool("GEOVISION_EVIDENCE_ENABLED", True)

EVIDENCE_DIR: Path = Path(
    env_str("GEOVISION_EVIDENCE_DIR", str(BASE_DIR / "evidence_clips"))
)

#: Rolling window retained in memory.
EVIDENCE_BUFFER_S: float = env_float("GEOVISION_EVIDENCE_BUFFER_S", 20.0)

#: Frames retained per second. Deliberately below camera FPS: the buffer is
#: evidence, not a broadcast feed, and encoding every frame would tax the
#: capture loop for no benefit.
EVIDENCE_CAPTURE_HZ: float = env_float("GEOVISION_EVIDENCE_CAPTURE_HZ", 10.0)

#: Clip window around a trigger: T-PRE .. T+POST.
EVIDENCE_PRE_S: float = env_float("GEOVISION_EVIDENCE_PRE_S", 10.0)
EVIDENCE_POST_S: float = env_float("GEOVISION_EVIDENCE_POST_S", 3.0)

EVIDENCE_JPEG_QUALITY: int = env_int("GEOVISION_EVIDENCE_JPEG_QUALITY", 70)

#: Save a clip automatically when an RFID tap resolves to a track.
EVIDENCE_AUTO_ON_RFID: bool = env_bool("GEOVISION_EVIDENCE_AUTO_ON_RFID", True)

#: Serve finished clips read-only over HTTP from EVIDENCE_DIR, at
#: ``GET /evidence/clips/{clip_id}/file``. This is how WASTRAQ turns an
#: EVIDENCE_READY reference into playable bytes. Off means the reference is
#: announced but nothing can be fetched -- the STEP 4A Mac then records the
#: clip with no media rather than failing.
EVIDENCE_SERVE_ENABLED: bool = env_bool("GEOVISION_EVIDENCE_SERVE_ENABLED", True)

#: How WASTRAQ reaches *this* machine, e.g. http://192.168.1.42:8000. Used
#: only to make the announced ``file_url`` absolute. Left empty the event
#: carries the relative endpoint instead and WASTRAQ resolves it against the
#: GeoVision base URL it already holds -- which is better than this node
#: guessing at a hostname it cannot verify. Never a credential.
EVIDENCE_PUBLIC_BASE_URL: str = env_str("GEOVISION_PUBLIC_BASE_URL", "").rstrip("/")

#: Cap on hashing work done at announce time. A clip larger than this is
#: announced with ``sha256: null`` rather than delaying the event; WASTRAQ
#: treats a null digest as "not offered", never as a mismatch.
EVIDENCE_HASH_MAX_BYTES: int = env_int(
    "GEOVISION_EVIDENCE_HASH_MAX_BYTES", 256 * 1024 * 1024
)

#: Save a clip automatically when a bound collector flags non-segregation.
#: This is the clip the dashboard shows next to a NON_SEGREGATED property, so
#: it defaults on and should stay on for the demo.
EVIDENCE_AUTO_ON_NON_SEGREGATION: bool = env_bool(
    "GEOVISION_EVIDENCE_AUTO_ON_NON_SEGREGATION", True
)


# --------------------------------------------------------------------------
# Captured frames (legacy static mount)
# --------------------------------------------------------------------------

CAPTURED_FRAMES_DIR: Path = Path(
    env_str("GEOVISION_CAPTURED_FRAMES_DIR", str(BASE_DIR / "captured_frames"))
)


def ensure_runtime_dirs() -> None:
    """Create directories the app writes to. Called from the API entry point."""

    CAPTURED_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    if SAVE_PERSON_CROPS:
        PERSON_CROP_DIR.mkdir(parents=True, exist_ok=True)
    if EVIDENCE_ENABLED:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
