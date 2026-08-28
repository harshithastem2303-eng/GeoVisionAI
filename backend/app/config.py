"""Demo configuration. Everything is env-overridable, nothing is secret."""

import os

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {
        "1", "true", "yes", "on"}


class Settings:
    # --- database ---------------------------------------------------------
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "5432"))
    DB_NAME = os.getenv("DB_NAME", "wastraq_demo")
    DB_USER = os.getenv("DB_USER", os.getenv("USER", "postgres"))
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    # --- GIS association tuning (metres) ----------------------------------
    # How far around a picker position we look for candidate service zones.
    SEARCH_RADIUS_M = _f("SEARCH_RADIUS_M", 15.0)
    # A point OUTSIDE every zone may still auto-associate, but only if the
    # nearest zone is this close.
    AUTO_MAX_DISTANCE_M = _f("AUTO_MAX_DISTANCE_M", 3.0)
    # ...and only if the second-nearest zone is at least this much further.
    AMBIGUITY_MARGIN_M = _f("AMBIGUITY_MARGIN_M", 2.0)
    # Confidence floor below which we refuse to auto-associate.
    MIN_AUTO_CONFIDENCE = _f("MIN_AUTO_CONFIDENCE", 0.70)

    # Projected CRS used for metre maths when geography casting is not used.
    # 32643 = WGS84 / UTM zone 43N (covers the demo lane's longitude).
    METRIC_SRID = int(os.getenv("METRIC_SRID", "32643"))

    # Where the surveyed frontage photos live (PROP-001.jpg ... PROP-016.jpg).
    # Used only for survey QA / human verification - never for live association.
    PHOTO_DIR = os.path.expanduser(os.getenv("PHOTO_DIR", "~/properties"))
    # Photos captured through the survey UI land here.
    SURVEY_UPLOAD_DIR = os.path.expanduser(
        os.getenv("SURVEY_UPLOAD_DIR", os.path.join(PHOTO_DIR, "survey-uploads")))

    # The operations dashboard is scoped to one collection route. The city
    # survey module deliberately is not - it spans the whole authority.
    DEMO_ROUTE_ID = os.getenv("DEMO_ROUTE_ID", "ROUTE-DEMO-01")

    # A device fix worse than this is not treated as verified geometry.
    GNSS_ACCURACY_WARN_M = _f("GNSS_ACCURACY_WARN_M", 10.0)

    # How far the entrance may sit from its own frontage / service zone before
    # the survey is flagged. Deliberately generous: a wide plot with a set-back
    # gate is normal, a 60 m gap means someone mapped the wrong building. This
    # flags, it does not silently accept and it does not silently reject.
    ENTRANCE_PROXIMITY_MAX_M = _f("ENTRANCE_PROXIMITY_MAX_M", 20.0)
    # Below this the geometry is unambiguously fine; between the two the
    # surveyor gets a warning but can still submit.
    ENTRANCE_PROXIMITY_OK_M = _f("ENTRANCE_PROXIMITY_OK_M", 5.0)

    # Property Master duplicate detection. How close a new registration's
    # reference fix has to be to an existing property before the clerk is
    # asked "is this the same building?". Generous on purpose: this warns,
    # it never blocks, and it never merges.
    DUPLICATE_RADIUS_M = _f("DUPLICATE_RADIUS_M", 30.0)
    # A registration fix worse than this is flagged as needing field
    # correction. Registration still succeeds - the survey fixes it.
    REGISTRATION_ACCURACY_WARN_M = _f("REGISTRATION_ACCURACY_WARN_M", 25.0)

    # A service zone is an operational standing area, not the whole plot.
    MIN_SERVICE_ZONE_AREA_M2 = _f("MIN_SERVICE_ZONE_AREA_M2", 1.0)
    MAX_SERVICE_ZONE_AREA_M2 = _f("MAX_SERVICE_ZONE_AREA_M2", 400.0)

    # --- vision / RealSense picker tracking (phase 1) ----------------------
    # Camera-LOCAL perception only: no GNSS, no vehicle pose, no property
    # association. Every one of these is env-overridable so the demo can be
    # tuned on the day without editing code.

    # The camera thread never starts on its own. The backend also serves the
    # frozen property system; grabbing a USB camera on every `run_backend.sh`
    # would be a surprise. The demo page (or POST /vision/start) starts it.
    VISION_AUTOSTART = _b("VISION_AUTOSTART", False)

    # stream
    VISION_WIDTH = _i("VISION_WIDTH", 640)
    VISION_HEIGHT = _i("VISION_HEIGHT", 480)
    VISION_FPS = _i("VISION_FPS", 30)
    # Steady-state frame wait. At 15 fps a frame is due every 67 ms, so 2 s is
    # already generous once the stream is actually running.
    VISION_FRAME_TIMEOUT_MS = _i("VISION_FRAME_TIMEOUT_MS", 2000)
    # The FIRST frameset after pipeline.start() is a different animal: sensor
    # power-up, auto-exposure convergence and - on macOS - a UVC negotiation.
    # Seconds, not milliseconds. Sharing the 2 s runtime timeout with the
    # startup wait is exactly how a slow start becomes a permanent
    # "Frame didn't arrive within 2000".
    VISION_STARTUP_TIMEOUT_MS = _i("VISION_STARTUP_TIMEOUT_MS", 10000)
    # Framesets discarded after start before anything looks at one. The first
    # few are dark, half-exposed, and sometimes missing a stream.
    VISION_WARMUP_FRAMES = _i("VISION_WARMUP_FRAMES", 10)
    # The conservative rate the staged hardware diagnostic starts from. Prove
    # 15 fps works on this Mac before assuming 30 does.
    VISION_DIAG_FPS = _i("VISION_DIAG_FPS", 15)
    VISION_RECONNECT_S = _f("VISION_RECONNECT_S", 3.0)
    VISION_RECONNECT_MAX_S = _f("VISION_RECONNECT_MAX_S", 15.0)

    # --- hardware-proven startup (Mac / D455) -----------------------------
    # scripts/diag_realsense_startup.py established on real hardware that the
    # only sequence that delivers frames here is:
    #     enable_device(serial) + depth 640x480 Z16 @15 + pipeline.start(cfg)
    # with no can_resolve() probe and no sensor/profile sweep before it.
    #
    # Bind the camera explicitly. Empty = discover, and refuse to guess when
    # more than one RealSense is attached.
    VISION_SERIAL = os.getenv("VISION_SERIAL", "").strip()
    # Depth-only until the depth smoke test passes on the Mac. Colour is a
    # second stream on the same USB link and a second thing that can fail; it
    # is not part of this test.
    VISION_ENABLE_COLOR = _b("VISION_ENABLE_COLOR", False)
    # The rate the diagnostic actually started and streamed at.
    VISION_DEPTH_FPS = _i("VISION_DEPTH_FPS", 15)

    # detector + tracker
    #
    # Weights live in <repo>/models so the installer can fetch them once, up
    # front, over a connection that works - rather than having ultralytics
    # try to download mid-demo, relative to whatever the working directory
    # happens to be. A bare name here is resolved against that directory
    # first (see vision/pipeline.py) and falls back to ultralytics' own
    # lookup if it is not there.
    VISION_MODEL_DIR = os.path.expanduser(os.getenv(
        "VISION_MODEL_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "models")))
    VISION_MODEL = os.getenv("VISION_MODEL", "yolov8n.pt")
    VISION_CONF = _f("VISION_CONF", 0.35)
    VISION_IMGSZ = _i("VISION_IMGSZ", 640)
    # bytetrack.yaml or botsort.yaml - both ship with ultralytics.
    VISION_TRACKER = os.getenv("VISION_TRACKER", "bytetrack.yaml")
    # "" lets ultralytics choose (mps on Apple Silicon, else cpu).
    VISION_DEVICE = os.getenv("VISION_DEVICE", "")

    # depth sampling - see backend/app/vision/geometry.py for the method
    VISION_DEPTH_WINDOW = _i("VISION_DEPTH_WINDOW", 9)
    VISION_DEPTH_MIN_VALID = _i("VISION_DEPTH_MIN_VALID", 6)
    VISION_DEPTH_CLUSTER_TOL_M = _f("VISION_DEPTH_CLUSTER_TOL_M", 0.30)
    VISION_DEPTH_MIN_M = _f("VISION_DEPTH_MIN_M", 0.3)
    VISION_DEPTH_MAX_M = _f("VISION_DEPTH_MAX_M", 10.0)
    # How far above the bottom edge of the box the ground anchor sits, as a
    # fraction of box height.
    VISION_ANCHOR_INSET = _f("VISION_ANCHOR_INSET", 0.06)

    # smoothing - low lag beats low jitter for a demo you walk around in
    VISION_SMOOTH_ALPHA = _f("VISION_SMOOTH_ALPHA", 0.4)
    VISION_SMOOTH_MAX_JUMP_M = _f("VISION_SMOOTH_MAX_JUMP_M", 1.2)

    # track lifetime + trajectory buffer (seconds)
    VISION_TRAJECTORY_S = _f("VISION_TRAJECTORY_S", 12.0)
    VISION_TRACK_TTL_S = _f("VISION_TRACK_TTL_S", 1.5)
    VISION_TRACK_RETIRE_S = _f("VISION_TRACK_RETIRE_S", 6.0)

    # annotated MJPEG feed
    VISION_STREAM_ENABLED = _b("VISION_STREAM_ENABLED", True)
    VISION_JPEG_QUALITY = _i("VISION_JPEG_QUALITY", 70)

    # --- GeoVision edge ingestion -----------------------------------------
    # The Windows RealSense/RFID laptop POSTs observations to
    # /integrations/geovision/events. Perception only: these settings tune
    # how the status endpoint reads, never how a property is decided.
    GEOVISION_ENABLED = _b("GEOVISION_ENABLED", True)
    # A camera track older than this is no longer "active". The edge
    # publishes at ~5 Hz per track, so anything beyond a few seconds of
    # silence means the person left frame or the stream stopped.
    GEOVISION_TRACK_STALE_S = _f("GEOVISION_TRACK_STALE_S", 15.0)
    # A source that has said nothing for this long is treated as offline.
    # Generous: HEARTBEAT is optional and off by default on the edge, so a
    # busy device may only be heard from via TRACK_UPDATE.
    GEOVISION_DEVICE_STALE_S = _f("GEOVISION_DEVICE_STALE_S", 60.0)

    # --- LangSmith tracing (optional, OFF by default) ----------------------
    # Records the association decision tree - request -> lookup -> the
    # individual PostGIS queries - as one run per request. See
    # backend/app/tracing.py and docs/TRACING.md.
    #
    # Off unless explicitly switched on. A checkout with no LangSmith account
    # behaves exactly as it did before this existed.
    LANGSMITH_TRACING = _b("LANGSMITH_TRACING", False)
    LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "wastraq-demo")
    # Blank means the SDK's default (https://api.smith.langchain.com). Set it
    # for the EU region or a self-hosted instance.
    LANGSMITH_ENDPOINT = os.getenv("LANGSMITH_ENDPOINT", "")
    # One child span per database round trip. The statement and a row COUNT,
    # never the rows themselves.
    LANGSMITH_TRACE_SQL = _b("LANGSMITH_TRACE_SQL", True)
    # Request paths that are never traced. These are polled by the dashboards
    # or stream continuously; tracing them buries the collection events under
    # identical polling spans.
    LANGSMITH_TRACE_EXCLUDE = tuple(
        s.strip() for s in os.getenv(
            "LANGSMITH_TRACE_EXCLUDE",
            "/assets,/vision/stream,/vision/tracks,/vision/status,/health,/favicon.ico,/integrations/geovision/events",
        ).split(",") if s.strip()
    )

    API_HOST = os.getenv("API_HOST", "127.0.0.1")
    API_PORT = int(os.getenv("API_PORT", "8000"))

    @property
    def dsn(self) -> str:
        pw = f" password={self.DB_PASSWORD}" if self.DB_PASSWORD else ""
        return (
            f"host={self.DB_HOST} port={self.DB_PORT} "
            f"dbname={self.DB_NAME} user={self.DB_USER}{pw}"
        )


settings = Settings()
