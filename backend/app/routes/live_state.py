"""Live collection state for the operations dashboard.

    GET /live/state                     one call, everything the panel shows
    GET /live/geovision/tracks          read-only proxy of the edge's /tracks
    GET /live/geovision/worker-bindings read-only proxy of the edge's bindings

Why this module exists
----------------------
The dashboard needs three things at once during the demo: who the camera is
currently tracking (GeoVision, on Windows), whether that track is bound to a
collector (GeoVision), and what WASTRAQ has decided about it (the episode
engine, here). Two of those live on another machine.

It also needs to know whether there is footage for the event that came out
of all that - and must not grow a second video player to say so. The
evidence block here counts what the EXISTING evidence modal would show, read
through the same `evidence_media` helper that modal reads through, and hands
the panel an event id to open it with.

The browser is deliberately kept out of that: a page served from the Mac
calling http://<windows>:8000 directly is one CORS header, one sleeping
laptop or one changed IP away from a dashboard that looks broken on stage.
So the Mac asks, on the browser's behalf, with a short timeout and a small
cache, and always answers - an unreachable edge is a FIELD in the response,
not a failed request.

Three rules
-----------
1. **Read only.** Nothing here writes to the database, creates an episode,
   binds a track, or POSTs to Windows. It is a window, not a control.

2. **No property authority moves.** The property on the panel comes from
   WASTRAQ's own episode engine (PostGIS service-zone association) and from
   nowhere else. Anything a `property_id`-shaped field on the edge payload
   might say is dropped on arrival, exactly as the ingestion side already
   does.

3. **No second media path.** The evidence block carries counts, states and
   ids. It never carries a media URL, so there is exactly one thing in this
   dashboard that fetches `/evidence/{id}/media`, and it is the modal.

4. **No Windows filesystem path is ever rendered.** `_sanitize()` strips
   path-shaped keys and drive-letter/UNC-shaped values out of everything
   that came from the edge, before it can reach a response.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Query

from ..config import settings

router = APIRouter(prefix="/live", tags=["live"])

#: The edge paths this panel reads. Read-only GETs, both of them.
EDGE_TRACKS_PATH = "/tracks"
EDGE_BINDINGS_PATH = "/worker-bindings"

#: The dashboard polls at 1-2 s. One cached edge answer is shared by every
#: request inside this window, so a faster poll cannot turn into a faster
#: hammering of the Windows laptop.
EDGE_CACHE_TTL_S = 0.8
#: After a failure, do not dial again for this long. Without it, an edge that
#: is switched off costs one full timeout on every single poll.
EDGE_FAIL_BACKOFF_S = 3.0

# --- path safety -------------------------------------------------------------
#: Keys that carry a location on disk somewhere. Dropped wholesale.
_PATH_KEYS = {
    "file_path", "filepath", "path", "local_path", "output_path", "dir",
    "directory", "folder", "clip_dir", "clip_path", "output_dir", "save_dir",
    "video_path", "frame_dir", "evidence_dir", "log_path", "db_path",
}
#: Property authority. Never accepted from the edge - same list as the
#: ingestion guard, for the same reason.
_FORBIDDEN_KEYS = {
    "property_id", "house_number", "owner_name", "formatted_address",
    "segregation_status", "service_zone_id", "zone_id",
}
#: C:\..., \\server\share, file:// - a Windows path in any of its hats.
_PATHY = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|file://)")


def _sanitize(value: Any, _depth: int = 0) -> Any:
    """Drop path-shaped and authority-shaped material from edge payloads."""
    if _depth > 8:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            low = key.lower()
            if low in _PATH_KEYS or low in _FORBIDDEN_KEYS:
                continue
            clean = _sanitize(v, _depth + 1)
            if isinstance(clean, str) and _PATHY.match(clean):
                continue
            out[key] = clean
        return out
    if isinstance(value, list):
        return [_sanitize(v, _depth + 1) for v in value
                if not (isinstance(v, str) and _PATHY.match(v))]
    if isinstance(value, str) and _PATHY.match(value):
        return None
    return value


# --- the edge reader ---------------------------------------------------------
Transport = Callable[[str, float], tuple[int, Any]]


def urllib_get(url: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json",
                 "User-Agent": "WASTRAQ-LivePanel/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read() or b"null"
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, None


class EdgeReader:
    """Cached, backing-off, never-raising reader for one GeoVision endpoint.

    ``read()`` answers ``(payload, error)``. One of them is always None, and
    the caller never waits longer than ``timeout_s`` for either.
    """

    def __init__(self, base_url: str = "", *, timeout_s: float = 1.5,
                 ttl_s: float = EDGE_CACHE_TTL_S,
                 backoff_s: float = EDGE_FAIL_BACKOFF_S,
                 transport: Transport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        self.ttl_s = ttl_s
        self.backoff_s = backoff_s
        self.transport = transport or urllib_get
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any, str | None]] = {}
        self.last_ok_at: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def read(self, path: str) -> tuple[Any, str | None]:
        if not self.configured:
            return None, "GEOVISION_EDGE_BASE_URL is not set"
        now = self.clock()
        with self._lock:
            hit = self._cache.get(path)
        if hit and (now - hit[0]) < (self.backoff_s if hit[2] else self.ttl_s):
            return hit[1], hit[2]

        payload: Any = None
        error: str | None = None
        try:
            status, body = self.transport(self.base_url + path, self.timeout_s)
            if 200 <= status < 300:
                payload = _sanitize(body)
            else:
                error = f"edge answered HTTP {status}"
        except urllib.error.HTTPError as exc:            # pragma: no cover
            error = f"edge answered HTTP {exc.code}"
        except Exception as exc:                          # noqa: BLE001
            # A sleeping laptop is a normal state of the world, not a 500.
            error = f"{type(exc).__name__}: {exc}"

        with self._lock:
            self._cache[path] = (now, payload, error)
            if error is None:
                self.last_ok_at = now
        return payload, error

    def last_ok_age_s(self) -> float | None:
        if self.last_ok_at is None:
            return None
        return round(self.clock() - self.last_ok_at, 2)


_reader: EdgeReader | None = None
_reader_lock = threading.Lock()


def get_reader() -> EdgeReader:
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = EdgeReader(
                settings.GEOVISION_EDGE_BASE_URL,
                # A shade under the mirror's 2 s: this one sits in front of a
                # 1-2 s dashboard poll and must not stack up behind itself.
                timeout_s=min(float(settings.GEOVISION_EDGE_TIMEOUT_S), 1.5),
            )
        return _reader


def set_reader(reader: EdgeReader | None) -> None:
    """Replace the process reader. Tests only."""
    global _reader
    with _reader_lock:
        _reader = reader


# --- normalisation -----------------------------------------------------------
def _pick(row: Any, *names: str, default: Any = None) -> Any:
    """First present, non-null value among ``names``. Edge builds differ in
    what they call the same number; this is cheaper than pinning a version."""
    if not isinstance(row, dict):
        return default
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return default


def _as_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    """Accept a bare list or the usual ``{"tracks": [...]}`` envelope."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _track_id_of(row: dict[str, Any]) -> int | None:
    v = _pick(row, "track_id", "id", "tid")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v: Any, digits: int = 3) -> float | None:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _normalise_track(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "track_id": _track_id_of(row),
        "collector_id": _pick(row, "collector_id", "worker_id", "bound_collector_id"),
        "collector_name": _pick(row, "collector_name", "worker_name", "name"),
        "rfid_uid": _pick(row, "rfid_uid", "rfid_id", "rfid"),
        "depth_m": _num(_pick(row, "depth_m", "depth")),
        "depth_valid": _pick(row, "depth_valid"),
        "depth_status": _pick(row, "depth_status"),
        "authorized": bool(_pick(row, "is_authorized_picker", "authorized",
                                 "is_authorised_picker", default=False)),
        "identity_confidence": _num(_pick(row, "identity_confidence",
                                          "binding_confidence")),
        "confidence": _num(_pick(row, "confidence", "score")),
        "age_s": _num(_pick(row, "age_s", "seconds_since_seen"), 2),
        "last_seen": _pick(row, "last_seen", "last_seen_at", "timestamp", "ts"),
        "source_id": _pick(row, "source_id", "device_id"),
        "session_id": _pick(row, "session_id"),
    }


def _normalise_binding(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "collector_id": _pick(row, "collector_id", "worker_id"),
        "collector_name": _pick(row, "collector_name", "worker_name", "name"),
        "track_id": _track_id_of(row),
        "rfid_uid": _pick(row, "rfid_uid", "rfid_id", "rfid"),
        "session_id": _pick(row, "session_id"),
        "bound_at": _pick(row, "bound_at", "created_at", "event_time"),
        "last_seen": _pick(row, "last_seen", "last_seen_at"),
        "locked": _pick(row, "locked", "is_locked"),
        "identity_confidence": _num(_pick(row, "confidence",
                                          "identity_confidence",
                                          "binding_confidence")),
        "selection_rule": _pick(row, "selection_rule", "rule", "reason"),
        "status": _pick(row, "status", "binding_status"),
    }


# --- assembly ----------------------------------------------------------------
def build_live_state(
    *,
    engine: dict[str, Any] | None,
    edge_tracks: Any = None,
    edge_bindings: Any = None,
    edge_error: str | None = None,
    edge_configured: bool = True,
    edge_last_ok_age_s: float | None = None,
    ingest_tracks: Iterable[dict[str, Any]] | None = None,
    db_active_episodes: Iterable[dict[str, Any]] | None = None,
    last_episode: dict[str, Any] | None = None,
    collector_names: dict[str, str] | None = None,
    mirror: dict[str, Any] | None = None,
    evidence_for_event: Callable[[str], list[dict[str, Any]]] | None = None,
    fallback_event_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the Live panel renders, from state that is already known.

    Pure on purpose: no DB handle, no socket, no clock of its own beyond the
    timestamp. Every branch the panel has to survive on stage - offline edge,
    no picker, no binding, no episode - is reachable from this signature, so
    it can be tested without any of the hardware being present.
    """
    engine = engine or {}
    names = collector_names or {}
    tracks = [_normalise_track(r) for r in _as_list(edge_tracks, "tracks", "people")]
    tracks = [t for t in tracks if t["track_id"] is not None]
    bindings = [_normalise_binding(r) for r in
                _as_list(edge_bindings, "bindings", "worker_bindings")]

    engine_bindings = engine.get("bindings") or []
    engine_active = engine.get("active_episodes") or []
    candidates = engine.get("candidates") or []
    db_active = list(db_active_episodes or [])
    ingest = [r for r in (ingest_tracks or []) if isinstance(r, dict)]

    geovision_connected = bool(edge_error is None and edge_configured
                               and edge_tracks is not None)

    # -- BINDING ------------------------------------------------------------
    # WASTRAQ's own binding is the one that matters: it is what the episode
    # engine acts on. The edge row only enriches it (lock state, the rule the
    # tap was resolved by), and stands in alone when WASTRAQ has not seen the
    # WORKER_TRACK_BOUND event yet.
    wq_binding = engine_bindings[0] if engine_bindings else None
    edge_binding = None
    if wq_binding:
        edge_binding = next(
            (b for b in bindings
             if b["collector_id"] == wq_binding.get("collector_id")
             or (b["track_id"] is not None
                 and b["track_id"] == wq_binding.get("track_id"))),
            None)
    elif bindings:
        edge_binding = bindings[0]

    collector_id = (wq_binding or {}).get("collector_id") \
        or (edge_binding or {}).get("collector_id")
    bound_track = (wq_binding or {}).get("track_id")
    if bound_track is None and edge_binding:
        bound_track = edge_binding["track_id"]
    collector_name = ((edge_binding or {}).get("collector_name")
                      or names.get(collector_id or "", None))

    binding = {
        "bound": bool(wq_binding or edge_binding),
        "collector_id": collector_id,
        "collector_name": collector_name,
        "track_id": bound_track,
        "rfid_uid": (wq_binding or {}).get("rfid_uid")
                    or (edge_binding or {}).get("rfid_uid"),
        "session_id": (wq_binding or {}).get("session_id")
                      or (edge_binding or {}).get("session_id"),
        "bound_at": (wq_binding or {}).get("bound_at")
                    or (edge_binding or {}).get("bound_at"),
        "locked": (edge_binding or {}).get("locked"),
        "identity_confidence": (edge_binding or {}).get("identity_confidence"),
        "selection_rule": (edge_binding or {}).get("selection_rule"),
        "status": (edge_binding or {}).get("status"),
        # Which machine each half came from, so "not bound" can be told from
        # "bound on the edge, not yet mirrored into WASTRAQ".
        "known_to_wastraq": bool(wq_binding),
        "known_to_geovision": bool(edge_binding),
        "edge_binding_count": len(bindings),
    }

    # -- TRACKING -----------------------------------------------------------
    # "Current authorized track" means, in order: the track WASTRAQ's binding
    # points at; the track the edge says is authorised; otherwise the best
    # track it can see, plainly labelled as unauthorised.
    track = None
    if bound_track is not None:
        track = next((t for t in tracks if t["track_id"] == bound_track), None)
    if track is None:
        track = next((t for t in tracks if t["authorized"]), None)
    if track is None and tracks:
        track = sorted(tracks, key=lambda t: (t["confidence"] or 0),
                       reverse=True)[0]

    source = "GEOVISION_EDGE" if track else None
    if track is None and ingest:
        # The edge is unreachable but its observations already landed here.
        # Second-best, and said so.
        row = ingest[0]
        if bound_track is not None:
            row = next((r for r in ingest
                        if r.get("track_id") == bound_track), row)
        track = _normalise_track(row)
        source = "WASTRAQ_INGEST"

    authorized = bool(
        (track or {}).get("authorized")
        or (bound_track is not None and (track or {}).get("track_id") == bound_track)
    )
    if track is None:
        authorization_state = "NO_PICKER"
    elif authorized:
        authorization_state = "AUTHORIZED"
    else:
        authorization_state = "UNAUTHORIZED"

    tracking = {
        "available": track is not None,
        "track_id": (track or {}).get("track_id"),
        "collector_id": (track or {}).get("collector_id") or collector_id,
        "collector_name": ((track or {}).get("collector_name")
                           or collector_name
                           or names.get((track or {}).get("collector_id") or "", None)),
        "rfid_uid": (track or {}).get("rfid_uid") or binding["rfid_uid"],
        "depth_m": (track or {}).get("depth_m"),
        "depth_valid": (track or {}).get("depth_valid"),
        "depth_status": (track or {}).get("depth_status"),
        "authorized": authorized if track else None,
        "authorization_state": authorization_state,
        "identity_confidence": ((track or {}).get("identity_confidence")
                                or binding["identity_confidence"]),
        "confidence": (track or {}).get("confidence"),
        "age_s": (track or {}).get("age_s"),
        "source_id": (track or {}).get("source_id"),
        "source": source,
        "track_count": len(tracks) if tracks else len(ingest),
    }

    # -- EPISODE ------------------------------------------------------------
    # ACTIVE beats CLOSED beats nothing. The property, the association and the
    # segregation status all come from here - WASTRAQ's engine - and are not
    # merged with anything the edge said.
    active = None
    if collector_id:
        active = next((a for a in engine_active
                       if a.get("collector_id") == collector_id), None)
    if active is None and engine_active:
        active = engine_active[0]

    episode: dict[str, Any]
    if active:
        db_row = next((r for r in db_active
                       if r.get("episode_id") == active.get("episode_id")), {})
        episode = {
            "state": "ACTIVE",
            "episode_id": active.get("episode_id"),
            "property_id": active.get("property_id"),
            "track_id": active.get("track_id"),
            "collector_id": active.get("collector_id"),
            "segregation_status": active.get("segregation_status"),
            "association_status": active.get("association_status"),
            "association_confidence": _num(db_row.get("association_confidence")),
            "observations": active.get("observations"),
            "started_at": active.get("started_at"),
            "last_inside": active.get("last_inside"),
            "house_number": db_row.get("house_number"),
            "collection_event_id": db_row.get("collection_event_id"),
        }
    elif last_episode:
        episode = {
            "state": last_episode.get("state") or "CLOSED",
            "episode_id": last_episode.get("episode_id"),
            "property_id": last_episode.get("property_id"),
            "track_id": last_episode.get("track_id"),
            "collector_id": last_episode.get("collector_id"),
            "segregation_status": last_episode.get("segregation_status"),
            "association_status": last_episode.get("association_status"),
            "association_confidence": _num(last_episode.get("association_confidence")),
            "observations": last_episode.get("observations"),
            "started_at": last_episode.get("started_at"),
            "last_inside": last_episode.get("last_seen_at"),
            "house_number": last_episode.get("house_number"),
            "collection_event_id": last_episode.get("collection_event_id"),
            "ended_at": last_episode.get("ended_at"),
        }
    else:
        episode = {
            "state": "NONE", "episode_id": None, "property_id": None,
            "track_id": None, "collector_id": None,
            "segregation_status": None, "association_status": None,
            "association_confidence": None, "observations": None,
            "started_at": None, "last_inside": None, "house_number": None,
            "collection_event_id": None,
        }

    # A dwell candidate is the state between "in a zone" and "an episode
    # exists". Without it, the panel shows nothing at all for the seconds the
    # collector is standing at the bin, which is exactly when someone is
    # watching it.
    cand = None
    if collector_id:
        cand = next((c for c in candidates
                     if c.get("collector_id") == collector_id), None)
    if cand is None and candidates:
        cand = candidates[0]
    episode["candidate"] = ({
        "property_id": cand.get("property_id"),
        "dwell_s": _num(cand.get("dwell_s"), 2),
        "confidence": _num(cand.get("confidence")),
        "dwell_required_s": _num(engine.get("dwell_s"), 2),
    } if cand and episode["state"] != "ACTIVE" else None)
    episode["active_count"] = len(engine_active)

    # -- EVIDENCE -----------------------------------------------------------
    # The panel does not play anything itself. It counts what the EXISTING
    # evidence modal would show for this event and offers a button that opens
    # that modal - so there is exactly one video player in this dashboard,
    # exactly one media URL shape, and one place where "is this clip actually
    # on the Mac" is decided (`evidence_media.describe`).
    event_id = episode.get("collection_event_id") or fallback_event_id
    evidence = {
        "event_id": event_id,
        "evidence_count": 0,
        "playable_count": 0,
        "pending_count": 0,
        "unavailable_count": 0,
        "placeholder_count": 0,
        "media_status": "NONE",
        "playable": False,
        "from_episode": bool(episode.get("collection_event_id")),
    }
    if event_id and evidence_for_event is not None:
        try:
            items = evidence_for_event(event_id) or []
        except Exception as exc:  # noqa: BLE001
            items = []
            evidence["error"] = f"{type(exc).__name__}: {exc}"
        real = [i for i in items if not i.get("is_placeholder")]
        statuses = [i.get("media_status") for i in real]
        evidence["evidence_count"] = len(real)
        evidence["placeholder_count"] = len(items) - len(real)
        evidence["playable_count"] = statuses.count("AVAILABLE")
        evidence["pending_count"] = statuses.count("PENDING")
        evidence["unavailable_count"] = statuses.count("UNAVAILABLE")
        if evidence["playable_count"]:
            evidence["media_status"] = "AVAILABLE"
        elif evidence["pending_count"]:
            evidence["media_status"] = "PENDING"
        elif evidence["unavailable_count"]:
            evidence["media_status"] = "UNAVAILABLE"
        evidence["playable"] = evidence["playable_count"] > 0
        # Identity only - never a path, and never a second media URL for a
        # second player to fetch. The modal builds its own from this id.
        first = next((i for i in real if i.get("media_status") == "AVAILABLE"), None)
        evidence["evidence_id"] = (first or {}).get("evidence_id")

    # -- HEALTH -------------------------------------------------------------
    health = {
        "geovision_connected": geovision_connected,
        "geovision_configured": bool(edge_configured),
        "geovision_error": edge_error,
        "geovision_last_ok_age_s": edge_last_ok_age_s,
        "episode_engine_enabled": bool(engine.get("enabled")),
        "camera_configured": bool(engine.get("camera_configured")),
        "camera": engine.get("camera"),
        "dwell_s": engine.get("dwell_s"),
        "mirror_enabled": bool((mirror or {}).get("enabled")),
        "mirror_queue_depth": (mirror or {}).get("queue_depth"),
        "ingest_track_count": len(ingest),
        "db_active_episodes": len(db_active),
    }

    return {
        "generated_at": (now or datetime.now(timezone.utc)),
        "tracking": tracking,
        "binding": binding,
        "episode": episode,
        "evidence": evidence,
        "health": health,
        # Said in the payload, not only in the docs: the property on this
        # panel was decided here, by PostGIS, and never by the camera.
        "authority": {
            "property_decided_by": "WASTRAQ PostGIS service-zone association",
            "geovision_supplies": ["identity (RFID)", "camera-frame position",
                                   "depth", "non-segregation signal"],
        },
    }


# --- routes ------------------------------------------------------------------
def _collector_names(ids: Iterable[str]) -> dict[str, str]:
    """collector_id -> a human name, from the pickers table when it knows one."""
    wanted = [i for i in {i for i in ids if i}]
    if not wanted:
        return {}
    try:
        from ..database import fetch_all
        rows = fetch_all(
            "SELECT picker_id, picker_name FROM pickers WHERE picker_id = ANY(%s)",
            (wanted,),
        )
    except Exception:  # noqa: BLE001
        return {}
    return {r["picker_id"]: r["picker_name"] for r in rows if r.get("picker_name")}


@router.get("/state")
def live_state(include_raw: bool = Query(False, description="Attach the "
                                         "sanitised edge payloads for debugging.")):
    """The Live GeoVision / Live Collection State panel, in one call.

    Always 200. Every failure this can meet - the edge asleep, the engine
    off, the database busy - is a field in the answer, because a panel that
    disappears when something breaks is the one thing worse than a panel that
    says what broke.
    """
    reader = get_reader()
    tracks_payload, tracks_error = reader.read(EDGE_TRACKS_PATH)
    bindings_payload, bindings_error = reader.read(EDGE_BINDINGS_PATH)

    engine_snapshot: dict[str, Any] = {}
    mirror_status: dict[str, Any] = {}
    try:
        from ..episodes.engine import get_engine
        from ..episodes.mirror import get_mirror
        engine_snapshot = get_engine().snapshot()
        mirror_status = get_mirror().status()
    except Exception as exc:  # noqa: BLE001
        engine_snapshot = {"error": f"{type(exc).__name__}: {exc}"}

    db_active: list[dict[str, Any]] = []
    last_episode: dict[str, Any] | None = None
    ingest: list[dict[str, Any]] = []
    try:
        from ..episodes.store import EpisodeStore
        store = EpisodeStore()
        db_active = store.active_episodes()
        recent = store.recent_episodes(limit=1)
        last_episode = recent[0] if recent else None
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..integrations import service as gv_service
        ingest = gv_service.active_tracks(settings.GEOVISION_TRACK_STALE_S, limit=10)
    except Exception:  # noqa: BLE001
        pass

    ids = [b.get("collector_id") for b in (engine_snapshot.get("bindings") or [])]
    ids += [a.get("collector_id") for a in (engine_snapshot.get("active_episodes") or [])]

    # The evidence the panel counts is read through the SAME helper the
    # evidence modal reads through, so "3 clips, 2 playable" on the panel and
    # what the modal then renders cannot disagree.
    def _evidence_for(event_id: str) -> list[dict[str, Any]]:
        from .. import evidence_media
        return evidence_media.enrich(evidence_media.evidence_for_event(event_id))

    fallback_event_id = None
    try:
        from ..database import fetch_one
        row = fetch_one(
            """
            SELECT ce.event_id FROM collection_events ce
              JOIN properties p ON p.property_id = ce.property_id
             WHERE p.route_id = %s
             ORDER BY ce.collection_time DESC LIMIT 1
            """,
            (settings.DEMO_ROUTE_ID,),
        )
        fallback_event_id = (row or {}).get("event_id")
    except Exception:  # noqa: BLE001
        pass

    state = build_live_state(
        engine=engine_snapshot,
        edge_tracks=tracks_payload,
        edge_bindings=bindings_payload,
        edge_error=tracks_error or bindings_error,
        edge_configured=reader.configured,
        edge_last_ok_age_s=reader.last_ok_age_s(),
        ingest_tracks=ingest,
        db_active_episodes=db_active,
        last_episode=last_episode,
        collector_names=_collector_names(ids),
        mirror=mirror_status,
        evidence_for_event=_evidence_for,
        fallback_event_id=fallback_event_id,
    )
    if include_raw:
        state["raw"] = {"tracks": tracks_payload, "worker_bindings": bindings_payload}
    return state


@router.get("/geovision/tracks")
def proxy_tracks():
    """Read-only pass-through of the edge's /tracks, path-scrubbed."""
    payload, error = get_reader().read(EDGE_TRACKS_PATH)
    return {"connected": error is None, "error": error, "tracks": payload}


@router.get("/geovision/worker-bindings")
def proxy_worker_bindings():
    """Read-only pass-through of the edge's /worker-bindings, path-scrubbed."""
    payload, error = get_reader().read(EDGE_BINDINGS_PATH)
    return {"connected": error is None, "error": error, "worker_bindings": payload}
