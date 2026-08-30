"""Wastraq Source Segregation Evidence Engine - demo backend.

Proves the chain:
    picker coordinate -> PostGIS service-zone association -> collection event
    -> segregation status -> evidence -> dashboard
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .database import close_pool, fetch_all, fetch_one, init_pool
from .episodes import router as episodes_router
from .integrations import router as geovision_router
from .routes import (collection_events, evidence, evidence_clips, gis_routes,
                     live_state, properties, property_registry)
from .survey import router as survey_router
from .tracing import LangSmithTraceMiddleware, tracing_enabled, tracing_status

STATIC_DIR = Path(__file__).parent / "static"

# Camera perception (phase 1: RealSense picker tracking).
#
# Imported defensively on purpose. This process also serves the property
# master, the survey module and the operations dashboard - frozen, working
# infrastructure. A vision dependency that will not import on this machine
# (pyrealsense2 has no Apple-Silicon wheel) must cost us the /vision routes
# and nothing else. The package itself imports no hardware library; this
# guard covers the case where that stops being true.
try:
    from .vision import router as vision_router
    VISION_IMPORT_ERROR: str | None = None
except Exception as _exc:  # noqa: BLE001  pragma: no cover
    vision_router = None  # type: ignore[assignment]
    VISION_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    if tracing_enabled():
        # One line, at start-up, so nobody has to guess whether the run they
        # are looking for was ever recorded.
        _t = tracing_status()
        print(
            f"[tracing] LangSmith project={_t['project']!r} "
            f"sending={_t['enabled']} sql_spans={_t['sql_spans']}"
            + ("" if _t["enabled"] else f" - {_t['reason']}")
        )
    if vision_router is not None:
        # No-op unless VISION_AUTOSTART is set. Off by default: the camera is
        # started by the picker-tracking page, not by running the backend.
        from .vision.pipeline import maybe_autostart
        maybe_autostart()

    # The episode sweeper closes an episode whose track simply stopped
    # reporting - the collector walked out of frame and the edge went quiet.
    # Without it, "leave" would depend on receiving an event that says the
    # collector left, and no such event exists.
    try:
        from .episodes.engine import get_engine
        _engine = get_engine()
        _engine.start_sweeper()
        if _engine.config.enabled and not _engine.config.camera_configured:
            print("[episodes] engine ON but CAMERA_ORIGIN_LAT/LON are unset - "
                  "no episodes will be created. Set them in backend/.env.")
    except Exception as _episode_error:  # noqa: BLE001
        print("!! Episode engine not started:", repr(_episode_error))

    yield

    try:
        from .episodes.engine import get_engine as _ge
        _ge().stop_sweeper()
        from .episodes.mirror import get_mirror as _gm
        _gm().stop()
    except Exception:  # noqa: BLE001
        pass
    if vision_router is not None:
        # Release the camera before the process goes away, so the next run
        # does not find the device busy.
        from .vision.pipeline import pipeline as _vision_pipeline
        if _vision_pipeline.running:
            _vision_pipeline.stop(timeout=3.0)
    close_pool()


app = FastAPI(
    title="Wastraq Demo Backend",
    description="Source Segregation Evidence Engine - one-lane demo",
    version=__version__,
    lifespan=lifespan,
)

# LangSmith request tracing. Added BEFORE the CORS middleware on purpose:
# Starlette applies the last-added middleware outermost, so this ordering
# leaves CORS on the outside, where it answers preflight OPTIONS itself and
# those never reach the tracer. Registered unconditionally - the middleware
# checks `tracing_enabled()` per request and, when tracing is off, passes the
# call straight through with one boolean test.
app.add_middleware(LangSmithTraceMiddleware)

# Demo only: the dashboard is served from the same origin, but this keeps
# local tooling (QGIS plugins, a separate front-end port) painless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# property_registry FIRST: both routers live on /properties, and FastAPI
# resolves in registration order. Included the other way round, the literal
# /properties/master would be swallowed by /properties/{property_id}.
app.include_router(property_registry.router)
app.include_router(properties.router)
app.include_router(gis_routes.router)
app.include_router(collection_events.router)
app.include_router(evidence.router)
app.include_router(evidence_clips.router)
app.include_router(survey_router)
# Inbound edge observations. Registered after the property/GIS routers on
# purpose: it owns its own /integrations prefix and must never shadow them.
app.include_router(geovision_router)
# WASTRAQ's own episode engine: bound track + service zone + dwell ->
# collection event. Registered after the GeoVision receiver, which feeds it.
app.include_router(episodes_router)
# Read-only live state for the dashboard's Live panel: the episode engine's
# own snapshot plus a proxied read of the GeoVision edge. Registered last of
# the API routers because it owns a fresh /live prefix and reads only what
# the routers above already expose.
app.include_router(live_state.router)
if vision_router is not None:
    app.include_router(vision_router)

# shared css/js for both dashboards
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

# Rolling video-evidence clips. Mounted at /evidence-files, not /evidence:
# fake_evidence_path() already produces /evidence/... strings for the demo
# rows, and GET /evidence is the database listing.
try:
    from .evidence_buffer import EVIDENCE_DIR as _EVIDENCE_DIR

    Path(_EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
    app.mount("/evidence-files", StaticFiles(directory=_EVIDENCE_DIR),
              name="evidence_files")
except Exception as _evidence_error:  # noqa: BLE001
    print("!! Evidence clip directory not mounted:", repr(_evidence_error))


@app.get("/", tags=["health"])
def health():
    # `version` lets the setup scripts tell a freshly started backend from a
    # stale process that never released port 8000.
    return {"status": "Wastraq Demo Backend Running", "version": __version__}


@app.get("/health/vision", tags=["health"])
def health_vision():
    """Whether the camera pipeline is importable and, if so, what it is doing.

    Separate from /health/db because they fail for completely unrelated
    reasons: the database being down is an outage, the camera being unplugged
    is a Tuesday.
    """
    if vision_router is None:
        return {"status": "unavailable", "import_error": VISION_IMPORT_ERROR}
    from .vision.pipeline import pipeline as _p
    return {"status": "ok", **_p.status()}


@app.get("/health/episodes", tags=["health"])
def health_episodes():
    """Is the episode engine armed, and does it know where the camera is.

    Its own health route for the same reason as /health/vision: an engine
    that is switched on but has no camera pose is not "working", it is
    silently producing nothing, and that must be visible without reading
    logs.
    """
    try:
        from .episodes.engine import get_engine
        from .episodes.mirror import get_mirror
        engine = get_engine()
        return {
            "status": "ok" if (engine.config.enabled
                               and engine.config.camera_configured) else "idle",
            "enabled": engine.config.enabled,
            "camera_configured": engine.config.camera_configured,
            "active_episodes": len(engine.snapshot()["active_episodes"]),
            "bindings": len(engine.snapshot()["bindings"]),
            "mirror": get_mirror().status(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "error": repr(exc)}


@app.get("/health/tracing", tags=["health"])
def health_tracing():
    """Whether LangSmith tracing is on, and if not, why not.

    Same reasoning as /health/vision: "off" and "misconfigured" look identical
    from the outside otherwise, and an observability layer that silently
    records nothing is worse than one that is plainly switched off.
    """
    return tracing_status()


@app.get("/health/db", tags=["health"])
def health_db():
    row = fetch_one("SELECT PostGIS_Version() AS postgis, current_database() AS db")
    counts = fetch_one(
        """
        SELECT
          (SELECT COUNT(*) FROM properties)            AS properties,
          (SELECT COUNT(*) FROM property_service_zones) AS service_zones,
          (SELECT COUNT(*) FROM pickers)                AS pickers,
          (SELECT COUNT(*) FROM collection_events)      AS collection_events,
          (SELECT COUNT(*) FROM evidence)               AS evidence,
          (SELECT COUNT(*) FROM property_photos
            WHERE photo_type = 'FRONTAGE')              AS frontage_photos
        """
    )
    return {"status": "ok", **(row or {}), "counts": counts}


@app.get("/pickers", tags=["pickers"])
def list_pickers():
    return fetch_all("SELECT * FROM pickers ORDER BY picker_id")


@app.get("/routes", tags=["dashboard"])
def list_routes():
    """Collection routes, for the route selector in the dashboard header."""
    return fetch_all(
        """
        SELECT p.route_id,
               count(*) AS properties,
               max(au.name) FILTER (WHERE au.unit_type = 'ROUTE_AREA') AS area_name,
               (SELECT count(*) FROM collection_events ce
                 JOIN properties p2 ON p2.property_id = ce.property_id
                WHERE p2.route_id = p.route_id) AS events
        FROM properties p
        LEFT JOIN administrative_units au ON au.admin_unit_id = p.admin_unit_id
        WHERE p.route_id IS NOT NULL
        GROUP BY p.route_id
        ORDER BY events DESC, p.route_id
        """
    )


@app.get("/summary", tags=["dashboard"])
def summary(route_id: str | None = None):
    """Everything the operations dashboard needs in one call.

    Scoped to ONE collection route (the demo lane by default). The property
    master also holds the city-wide survey properties; counting those here
    would make the operations KPIs meaningless.
    """
    rid = route_id or settings.DEMO_ROUTE_ID
    p = {"rid": rid}
    return {
        "route_id": rid,
        "totals": fetch_one(
            """
            SELECT
              (SELECT COUNT(*) FROM properties WHERE route_id = %(rid)s) AS properties,
              (SELECT COUNT(*) FROM properties WHERE route_id = %(rid)s
                 AND verification_status IN ('FIELD_VERIFIED','VERIFIED_FOR_OPERATION')) AS verified_properties,
              (SELECT COUNT(*) FROM property_service_zones z JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s) AS service_zones,
              (SELECT COUNT(*) FROM property_photos ph JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s AND ph.photo_type = 'FRONTAGE') AS frontage_photos,
              (SELECT COUNT(*) FROM collection_events ce JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s) AS events,
              (SELECT COUNT(DISTINCT ce.property_id) FROM collection_events ce
                 JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s
                  AND ce.collection_time >= date_trunc('day', now())) AS serviced_today,
              (SELECT COUNT(*) FROM collection_events ce JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s AND ce.segregation_status = 'SEGREGATED') AS segregated,
              (SELECT COUNT(*) FROM collection_events ce JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s AND ce.segregation_status = 'NOT_SEGREGATED') AS not_segregated,
              (SELECT COUNT(*) FROM collection_events ce JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s AND ce.review_status = 'NEEDS_REVIEW') AS needs_review,
              (SELECT COUNT(*) FROM evidence e JOIN collection_events ce USING (event_id)
                 JOIN properties p2 ON p2.property_id = ce.property_id
                WHERE p2.route_id = %(rid)s) AS evidence,
              (SELECT COUNT(*) FROM pickers WHERE active) AS active_pickers,
              (SELECT COUNT(*) FROM property_qa_issues q JOIN properties p2 USING (property_id)
                WHERE p2.route_id = %(rid)s AND q.status = 'OPEN') AS open_gis_issues
            """,
            p,
        ),
        "events": fetch_all(
            """
            SELECT v.* FROM v_collection_summary v
            JOIN properties p ON p.property_id = v.property_id
            WHERE p.route_id = %(rid)s
            ORDER BY v.collection_time DESC LIMIT 100
            """,
            p,
        ),
    }


@app.get("/analytics/operations", tags=["dashboard"])
def analytics_operations(route_id: str | None = None):
    """Operational analytics for one route - every figure is computed in SQL."""
    rid = route_id or settings.DEMO_ROUTE_ID
    p = {"rid": rid}
    core = fetch_one(
        """
        WITH scope AS (SELECT property_id FROM properties WHERE route_id = %(rid)s),
        ev AS (SELECT ce.* FROM collection_events ce JOIN scope s USING (property_id))
        SELECT
          (SELECT count(*) FROM scope)                                          AS properties,
          (SELECT count(*) FROM ev)                                             AS events,
          (SELECT count(DISTINCT property_id) FROM ev)                          AS properties_with_events,
          (SELECT count(*) FROM ev WHERE segregation_status = 'SEGREGATED')      AS segregated,
          (SELECT count(*) FROM ev WHERE segregation_status = 'NOT_SEGREGATED')  AS not_segregated,
          (SELECT count(*) FROM ev WHERE review_status = 'NEEDS_REVIEW')         AS needs_review,
          (SELECT count(*) FROM ev WHERE rfid_triggered)                         AS rfid_triggered,
          (SELECT round(avg(association_confidence), 3) FROM ev)                 AS avg_confidence,
          (SELECT count(*) FROM ev WHERE association_confidence < 0.85)          AS low_confidence_events,
          (SELECT count(*) FROM property_service_zones z JOIN scope s USING (property_id)) AS mapped_zones,
          (SELECT count(*) FROM property_qa_issues q JOIN scope s USING (property_id)
            WHERE q.status = 'OPEN')                                             AS open_gis_issues
        """,
        p,
    ) or {}
    ev = max(int(core.get("events") or 0), 1)
    props = max(int(core.get("properties") or 0), 1)
    core["segregation_compliance_pct"] = round(100.0 * int(core.get("segregated") or 0) / ev, 1)
    core["completion_pct"] = round(100.0 * int(core.get("properties_with_events") or 0) / props, 1)
    core["mapping_coverage_pct"] = round(100.0 * int(core.get("mapped_zones") or 0) / props, 1)
    return {
        "route_id": rid,
        "totals": core,
        "by_picker": fetch_all(
            """
            SELECT pk.picker_id, pk.picker_name, pk.active,
                   count(ce.event_id) AS events,
                   count(*) FILTER (WHERE ce.segregation_status = 'NOT_SEGREGATED') AS not_segregated,
                   round(avg(ce.association_confidence), 3) AS avg_confidence,
                   max(ce.collection_time) AS last_event
            FROM pickers pk
            LEFT JOIN collection_events ce ON ce.picker_id = pk.picker_id
            LEFT JOIN properties p ON p.property_id = ce.property_id AND p.route_id = %(rid)s
            GROUP BY pk.picker_id, pk.picker_name, pk.active
            ORDER BY events DESC
            """,
            p,
        ),
        "by_property": fetch_all(
            """
            SELECT p.property_id, p.house_number, p.owner_name,
                   count(ce.event_id) AS events,
                   count(*) FILTER (WHERE ce.segregation_status = 'NOT_SEGREGATED') AS not_segregated,
                   max(ce.collection_time) AS last_event
            FROM properties p
            LEFT JOIN collection_events ce ON ce.property_id = p.property_id
            WHERE p.route_id = %(rid)s
            GROUP BY p.property_id, p.house_number, p.owner_name
            ORDER BY p.property_id
            """,
            p,
        ),
    }


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    """Operations dashboard - the live collection route."""
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/picker-tracking", include_in_schema=False)
def picker_tracking():
    """Live RealSense picker tracking - camera-local coordinates.

    Deliberately NOT under /vision: that prefix is the JSON API, and a page
    route there would sit alongside /vision/tracks and /vision/status for no
    reason. Same rule as /property-registration vs /properties.
    """
    return FileResponse(STATIC_DIR / "picker-tracking.html")


@app.get("/property-registration", include_in_schema=False)
def property_registration():
    """Property Master - the administrative property record.

    Deliberately NOT under /properties: that prefix is the JSON API, and
    /properties/manage would have collided with GET /properties/{id}.
    """
    return FileResponse(STATIC_DIR / "property-registration.html")


# ---------------------------------------------------------------------------
# City survey module pages. Deliberately a separate URL space from /dashboard
# so the live demo and the city-scale module can never be confused.
# ---------------------------------------------------------------------------
# The three primary views are index / field / review. map, assignments and qa
# remain served because they are real, working tools - they are simply not in
# the primary navigation. The surveyor-performance page was removed: it showed
# throughput charts that only meant anything against the synthetic city data.
# GET /survey/api/analytics/surveyors still exists for when there is a real
# team to measure.
_SURVEY_PAGES = {
    "": "index.html",
    "field": "field.html",
    "review": "review.html",
    "map": "map.html",
    "assignments": "assignments.html",
    "qa": "qa.html",
}


@app.get("/survey", include_in_schema=False)
def survey_home():
    return FileResponse(STATIC_DIR / "survey" / "index.html")


@app.get("/survey/{page}", include_in_schema=False)
def survey_page(page: str):
    name = _SURVEY_PAGES.get(page)
    if not name:
        raise HTTPException(status_code=404, detail=f"No survey page {page!r}")
    return FileResponse(STATIC_DIR / "survey" / name)
