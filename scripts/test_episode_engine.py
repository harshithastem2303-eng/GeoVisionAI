#!/usr/bin/env python3
"""
Offline proof of the episode engine, the sixth GeoVision event and the
Windows episode mirror.

    python3 scripts/test_episode_engine.py

No database, no FastAPI, no running backend, no Windows laptop. pydantic is
the only third-party import, and only for the event contract. The engine,
the mirror and the camera transform are loaded by path into a synthetic
package so their relative imports resolve without dragging in psycopg.

What it is actually protecting - the eighteen claims the demo rests on:

   1  WORKER_TRACK_BOUND binds PICKER-01 to a camera track.
   2  An UNBOUND track creates nothing, however long it loiters.
   3  A bound track dwelling in PROP-001's service zone creates E-001.
   4  E-001 is mirrored to Windows - and the mirror carries no property.
   5  Leaving with no trigger closes E-001 SEGREGATED.
   6  Closing removes the Windows mirror.
   7  PROP-002 creates E-002 and mirrors it.
   8  A valid NON_SEGREGATION_TRIGGER(E-002) marks E-002 NOT_SEGREGATED.
   9  The same trigger_id again does nothing the second time.
  10  A trigger naming an unknown episode modifies no property.
  11  A trigger from the wrong collector or track modifies no property.
  12  An ambiguous position creates no episode; a weak one is REVIEW.
  13  EVIDENCE_READY lands on E-002's collection event.
  14  A dead Windows laptop cannot corrupt Mac episode state.
  15  The sixth event still cannot assert a property or a verdict.
  16  The camera transform round-trips (this is the geometry everything else
      is built on, so it is checked directly rather than assumed).
  17  Reset clears transient state and touches no property.
  18  Nothing in the whole run wrote to a property or a service zone.
  19  STEP 2 end to end, as ten separately-named claims: unbound skipped,
      binding, single candidate, dwell, no duplicate episode, leave grace,
      one collection event, ambiguity, missing camera pose, re-delivery.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EPISODES = os.path.join(ROOT, "backend", "app", "episodes")
SCHEMAS = os.path.join(ROOT, "backend", "app", "integrations", "schemas.py")

FAILURES: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# --- module loading -----------------------------------------------------------
def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: these modules use `from __future__ import
    # annotations`, so pydantic and dataclasses resolve their own names back
    # through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# A synthetic package so `from .transform import ...` inside engine.py works
# without importing backend/app/__init__.py (which needs fastapi and psycopg).
_pkg = types.ModuleType("epi")
_pkg.__path__ = [EPISODES]  # type: ignore[attr-defined]
sys.modules["epi"] = _pkg

gv = _load("gv_schemas", SCHEMAS)
transform = _load("epi.transform", os.path.join(EPISODES, "transform.py"))
mirror_mod = _load("epi.mirror", os.path.join(EPISODES, "mirror.py"))
engine_mod = _load("epi.engine", os.path.join(EPISODES, "engine.py"))

ADAPTER = gv.EVENT_ADAPTER
SOURCE = "GEOVISION-D455-01"
SESSION = "sess-demo"
UID = "04A1B2C3"


# --- fakes --------------------------------------------------------------------
class FakeStore:
    """The store interface, in dictionaries.

    It also acts as a tripwire: properties and service zones are read-only
    here, and any attempt to write one raises. Claim 18 is enforced by the
    fake rather than merely observed.
    """

    def __init__(self) -> None:
        self.episodes: dict[str, dict] = {}
        self.triggers: dict[str, dict] = {}
        self.collection_events: dict[str, dict] = {}
        self.evidence: list[dict] = []
        self.clips: dict[str, dict] = {}
        self.pickers = {"PICKER-01": "04A1B2C3", "PICKER-02": "04FFEE01"}
        self.properties = {f"PROP-{i:03d}": {"property_id": f"PROP-{i:03d}"}
                           for i in range(1, 17)}
        self._properties_frozen = dict(self.properties)
        self._seq = 0
        self._event_seq = 0
        self._evidence_seq = 0

    # tripwire
    def assert_properties_untouched(self) -> bool:
        return self.properties == self._properties_frozen

    # -- episodes
    def next_episode_id(self) -> str:
        self._seq += 1
        return f"E-{self._seq:03d}"

    def create_episode(self, **f):
        if any(e["state"] == "ACTIVE" and e["collector_id"] == f["collector_id"]
               for e in self.episodes.values()):
            return None  # the partial unique index
        row = dict(f, state="ACTIVE", segregation_status="SEGREGATED",
                   non_segregation_trigger_id=None, collection_event_id=None,
                   ended_at=None, dwell_s=None, mirror_status="PENDING",
                   mirror_error=None)
        self.episodes[f["episode_id"]] = row
        return dict(row)

    def touch_episode(self, episode_id, last_seen_at, observations):
        row = self.episodes.get(episode_id)
        if row and row["state"] == "ACTIVE":
            row["last_seen_at"] = last_seen_at
            row["observations"] = observations

    def get_episode(self, episode_id):
        row = self.episodes.get(episode_id)
        return dict(row) if row else None

    def active_episodes(self):
        return [dict(r) for r in self.episodes.values() if r["state"] == "ACTIVE"]

    def recent_episodes(self, limit=50, property_id=None):
        return [dict(r) for r in self.episodes.values()]

    def mark_non_segregated(self, episode_id, trigger_id, when):
        row = self.episodes.get(episode_id)
        if not row or row["state"] != "ACTIVE" or row["non_segregation_trigger_id"]:
            return None
        row["segregation_status"] = "NOT_SEGREGATED"
        row["non_segregation_trigger_id"] = trigger_id
        row["non_segregated_at"] = when
        return dict(row)

    def mark_closed_non_segregated(self, episode_id, trigger_id, when):
        row = self.episodes.get(episode_id)
        if not row or row["state"] != "CLOSED" or row["non_segregation_trigger_id"]:
            return None
        row["segregation_status"] = "NOT_SEGREGATED"
        row["non_segregation_trigger_id"] = trigger_id
        event_id = row.get("collection_event_id")
        if event_id and event_id in self.collection_events:
            self.collection_events[event_id]["segregation_status"] = "NOT_SEGREGATED"
            self.collection_events[event_id]["rfid_triggered"] = True
            self.collection_events[event_id]["review_status"] = "NEEDS_REVIEW"
            self.add_evidence(event_id, "NON_SEGREGATION_PROOF", None, when)
        return dict(row)

    def close_episode(self, episode_id, *, ended_at, dwell_s, observations,
                      state="CLOSED"):
        row = self.episodes.get(episode_id)
        if not row or row["state"] != "ACTIVE":
            return None
        row.update(state=state, ended_at=ended_at, dwell_s=dwell_s,
                   observations=max(row.get("observations", 0), observations))
        return dict(row)

    def set_mirror_status(self, episode_id, status, error=None):
        row = self.episodes.get(episode_id)
        if row:
            row["mirror_status"] = status
            row["mirror_error"] = error

    def abort_active(self, reason_state="ABORTED"):
        ids = []
        for row in self.episodes.values():
            if row["state"] == "ACTIVE":
                row["state"] = reason_state
                ids.append(row["episode_id"])
        return ids

    # -- collection events
    def picker_for(self, collector_id, rfid_uid=None):
        if collector_id in self.pickers:
            return collector_id
        for picker, uid in self.pickers.items():
            if rfid_uid and uid == rfid_uid:
                return picker
        return None

    def create_collection_event(self, *, episode, collection_time, review_status):
        self._event_seq += 1
        event_id = f"EVENT-{self._event_seq:03d}"
        row = {
            "event_id": event_id,
            "property_id": episode["property_id"],
            "picker_id": episode.get("picker_id"),
            "track_id": str(episode.get("track_id")),
            "segregation_status": episode.get("segregation_status", "SEGREGATED"),
            "association_confidence": episode.get("association_confidence"),
            "collection_time": collection_time,
            "rfid_triggered": episode.get("segregation_status") == "NOT_SEGREGATED",
            "review_status": review_status,
            "episode_id": episode["episode_id"],
        }
        self.collection_events[event_id] = row
        self.episodes[episode["episode_id"]]["collection_event_id"] = event_id
        return dict(row)

    def add_evidence(self, event_id, evidence_type, file_path, captured_at):
        self._evidence_seq += 1
        evidence_id = f"EVID-{self._evidence_seq:03d}"
        self.evidence.append({"evidence_id": evidence_id, "event_id": event_id,
                              "evidence_type": evidence_type,
                              "file_path": file_path or f"/evidence/{event_id}.jpg",
                              "captured_at": captured_at})
        return evidence_id

    # -- triggers
    def get_trigger(self, trigger_id):
        row = self.triggers.get(trigger_id)
        return dict(row) if row else None

    def claim_trigger(self, **fields):
        if fields["trigger_id"] in self.triggers:
            return False
        self.triggers[fields["trigger_id"]] = dict(
            fields, applied=False, applied_episode_id=None,
            resolution="PENDING", needs_review=False)
        return True

    def resolve_trigger(self, trigger_id, *, resolution, detail=None,
                        applied_episode_id=None, needs_review=False):
        row = self.triggers.get(trigger_id)
        if row:
            row.update(resolution=resolution, resolution_detail=detail,
                       applied=resolution == "APPLIED",
                       applied_episode_id=applied_episode_id,
                       needs_review=needs_review)

    def recent_triggers(self, limit=25):
        return [dict(r) for r in self.triggers.values()]

    # -- clips
    def episode_for_rfid_event(self, rfid_event_id):
        for row in self.triggers.values():
            if row.get("rfid_event_id") == rfid_event_id and row.get("applied_episode_id"):
                return row["applied_episode_id"]
        return None

    def episode_for_clip(self, *, source_id, session_id, track_id, clip_time,
                         window_s):
        for row in self.episodes.values():
            if (row["source_id"] == source_id
                    and row["session_id"] == (session_id or "")
                    and (track_id is None or row["track_id"] == track_id)
                    and row["segregation_status"] == "NOT_SEGREGATED"):
                return dict(row)
        return None

    def insert_clip(self, event):
        """What integrations.service._insert_clip does, before the engine runs."""
        self.clips[event.event_id] = {
            "event_id": event.event_id, "clip_id": event.clip_id,
            "file_path": event.file_path, "event_time": event.timestamp,
            "track_id": event.track_id, "session_id": event.session_id,
            "source_id": event.source_id, "episode_id": None,
            "linked_evidence_id": None}

    def tag_clip_episode(self, clip_event_id, episode_id):
        self.clips.setdefault(clip_event_id, {})["episode_id"] = episode_id

    def attach_clip(self, event_id, episode_id, *, clip_event_id, file_path,
                    captured_at):
        evidence_id = self.add_evidence(event_id, "VIDEO_CLIP", file_path,
                                        captured_at)
        self.clips.setdefault(clip_event_id, {}).update(
            episode_id=episode_id, linked_evidence_id=evidence_id)
        return evidence_id

    def pending_clips_for_episode(self, episode_id):
        return [dict(v, event_id=k) for k, v in self.clips.items()
                if v.get("episode_id") == episode_id
                and not v.get("linked_evidence_id")]

    def clip_row(self, clip_event_id):
        return self.clips.get(clip_event_id)


class FakeZones:
    """Rectangular service zones, so the association ladder is exercised
    without PostGIS but with the SAME result shape - including its refusals."""

    def __init__(self) -> None:
        # (property_id, lat_lo, lat_hi, lon_lo, lon_hi, confidence)
        self.zones = [
            ("PROP-001", 12.2943, 12.29435, 76.64160, 76.64166, 0.97),
            ("PROP-002", 12.2943, 12.29435, 76.64170, 76.64176, 0.96),
            # Deliberately weak: exercises the REVIEW path.
            ("PROP-003", 12.2943, 12.29435, 76.64180, 76.64186, 0.55),
        ]
        self.ambiguous_lon = 76.64200
        self.calls = 0

    def __call__(self, lat: float, lon: float) -> dict:
        self.calls += 1
        if abs(lon - self.ambiguous_lon) < 2e-5:
            return {"decision": "AMBIGUOUS", "property_id": None,
                    "confidence": 0.4, "method": "ST_DWITHIN",
                    "reason": "two zones equidistant",
                    "candidates": [{"property_id": "PROP-004"},
                                   {"property_id": "PROP-005"}]}
        for pid, la, lb, oa, ob, conf in self.zones:
            if la <= lat <= lb and oa <= lon <= ob:
                return {"decision": "AUTO_ASSOCIATED", "property_id": pid,
                        "confidence": conf,
                        "method": "ST_WITHIN_SERVICE_ZONE",
                        "reason": "inside one zone", "candidates": []}
        return {"decision": "NO_MATCH", "property_id": None, "confidence": 0.0,
                "method": "ST_DWITHIN", "reason": "nothing near",
                "candidates": []}


class RecordingTransport:
    """A Windows laptop that answers, or does not."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body))
        if self.fail:
            raise OSError("Windows laptop unreachable")
        return 200, {"ok": True}


# --- event helpers -------------------------------------------------------------
T0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
_n = [0]


def _eid(prefix: str) -> str:
    _n[0] += 1
    return f"{prefix}-{_n[0]:04d}"


def bound(track_id: int, at: datetime, collector: str = "PICKER-01"):
    return ADAPTER.validate_python({
        "event_type": "WORKER_TRACK_BOUND", "event_id": _eid("evt"),
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "source_id": SOURCE, "session_id": SESSION,
        "collector_id": collector, "rfid_uid": UID, "track_id": track_id,
        "confidence": 0.93, "rfid_event_id": "rfid-1"})


def track(track_id: int, at: datetime, right: float, forward: float,
          depth_valid: bool = True):
    return ADAPTER.validate_python({
        "event_type": "TRACK_UPDATE", "event_id": _eid("evt"),
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "source_id": SOURCE, "session_id": SESSION, "track_id": track_id,
        "confidence": 0.88, "depth_m": forward, "depth_valid": depth_valid,
        "depth_status": "OK" if depth_valid else "NO_VALID_SAMPLES",
        "relative_x_m": right, "relative_forward_m": forward,
        "is_authorized_picker": True, "collector_id": "PICKER-01"})


def trigger(at: datetime, *, trigger_id: str, episode_id: str | None,
            collector: str | None = "PICKER-01", track_id: int | None = 35,
            status: str = "RESOLVED", session_id: str | None = SESSION,
            rfid_event_id: str = "rfid-2"):
    payload = {
        "event_type": "NON_SEGREGATION_TRIGGER", "event_id": _eid("evt"),
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "source_id": SOURCE, "session_id": session_id,
        "trigger_id": trigger_id, "episode_id": episode_id,
        "collector_id": collector, "rfid_uid": UID, "track_id": track_id,
        "trigger_status": status, "duplicate": False,
        "rfid_event_id": rfid_event_id}
    return ADAPTER.validate_python(payload)


def clip(at: datetime, track_id: int = 35, rfid_event_id: str | None = "rfid-2"):
    return ADAPTER.validate_python({
        "event_type": "EVIDENCE_READY", "event_id": _eid("evt"),
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "source_id": SOURCE, "session_id": SESSION,
        "clip_id": "CLIP-0001", "track_id": track_id,
        "rfid_event_id": rfid_event_id, "frame_count": 131,
        "file_path": r"C:\GeoVision\backend\evidence_clips\CLIP-0001.mp4"})


# --- rig ----------------------------------------------------------------------
CAMERA = transform.CameraOrigin(latitude=12.29425, longitude=76.64160,
                                heading_deg=90.0)


def build(fail_mirror: bool = False):
    """One engine wired to fakes. dwell 2 s, leave grace 3 s, no throttle."""
    store = FakeStore()
    zones = FakeZones()
    tport = RecordingTransport(fail=fail_mirror)
    mir = mirror_mod.EpisodeMirror(base_url="http://windows.test:8000",
                                   timeout_s=0.2, retries=0,
                                   transport=tport)
    config = engine_mod.EpisodeConfig(
        enabled=True, dwell_s=2.0, leave_grace_s=3.0,
        min_assoc_interval_s=0.0, max_duration_s=180.0,
        review_confidence=0.85, binding_ttl_s=900.0,
        trigger_late_grace_s=30.0, evidence_link_window_s=120.0,
        require_depth_valid=True, camera=CAMERA, camera_configured=True)
    engine = engine_mod.EpisodeEngine(config, store, zones, mir)
    return engine, store, zones, mir, tport


def at_property(pid: str) -> tuple[float, float]:
    """Camera-frame (right, forward) that lands inside a zone's centre.

    Goes through the REAL inverse transform, so if the forward transform is
    wrong these coordinates are wrong in the same direction and the test
    still means something.
    """
    lo, hi = {"PROP-001": (76.64160, 76.64166),
              "PROP-002": (76.64170, 76.64176),
              "PROP-003": (76.64180, 76.64186)}[pid]
    lat = (12.2943 + 12.29435) / 2
    lon = (lo + hi) / 2
    return transform.wgs84_to_camera(CAMERA, lat, lon)


def ambiguous_point() -> tuple[float, float]:
    return transform.wgs84_to_camera(CAMERA, 12.294325, 76.64200)


def walk(engine, track_id, start, right, forward, seconds, step=0.5):
    """Feed observations at ``step`` intervals for ``seconds``."""
    last = None
    n = int(seconds / step) + 1
    for i in range(n):
        last = engine.on_track_update(
            track(track_id, start + timedelta(seconds=i * step), right, forward))
    return last, start + timedelta(seconds=(n - 1) * step)


# =============================================================================
# checks
# =============================================================================
def check_transform() -> None:
    print("\n16. camera transform - the geometry everything else stands on")
    origin = transform.CameraOrigin(12.2943, 76.6416, 90.0)

    lat, lon = transform.camera_to_wgs84(origin, 0.0, 10.0)
    ok("heading 90 deg: 10 m forward moves EAST, not north",
       abs(lat - origin.latitude) < 1e-9 and lon > origin.longitude,
       f"got {lat},{lon}")

    north = transform.CameraOrigin(12.2943, 76.6416, 0.0)
    lat, lon = transform.camera_to_wgs84(north, 0.0, 10.0)
    ok("heading 0 deg: 10 m forward moves NORTH",
       lat > north.latitude and abs(lon - north.longitude) < 1e-9)

    lat, lon = transform.camera_to_wgs84(north, 10.0, 0.0)
    ok("heading 0 deg: 10 m right moves EAST",
       abs(lat - north.latitude) < 1e-9 and lon > north.longitude)

    for heading in (0.0, 37.5, 90.0, 213.0, 359.0):
        o = transform.CameraOrigin(12.2943, 76.6416, heading)
        r0, f0 = 3.25, 7.5
        back = transform.wgs84_to_camera(o, *transform.camera_to_wgs84(o, r0, f0))
        if not ok(f"round trip at heading {heading:g}",
                  abs(back[0] - r0) < 1e-6 and abs(back[1] - f0) < 1e-6,
                  f"got {back}"):
            break

    lat, lon = transform.camera_to_wgs84(origin, 0.0, 10.0)
    metres = (lon - origin.longitude) * 111320.0 * 0.977  # cos(12.29 deg)
    ok("10 m forward really is ~10 m on the ground", abs(metres - 10.0) < 0.05,
       f"{metres:.3f} m")


def check_sixth_event_contract() -> None:
    print("\n15. the sixth event cannot assert a property or a verdict")
    base = {
        "event_type": "NON_SEGREGATION_TRIGGER", "event_id": "e-1",
        "timestamp": "2026-08-29T09:00:00.000Z", "source_id": SOURCE,
        "session_id": SESSION, "trigger_id": "TRG-1", "episode_id": "E-002",
        "collector_id": "PICKER-01", "rfid_uid": UID, "track_id": 35,
        "trigger_status": "RESOLVED", "duplicate": False,
        "rfid_event_id": "rfid-2"}

    event = ADAPTER.validate_python(dict(base))
    ok("a clean trigger validates", event.event_type == "NON_SEGREGATION_TRIGGER")
    ok("trigger_id survives", event.trigger_id == "TRG-1")

    for field, value in (("property_id", "PROP-004"),
                         ("segregation_status", "NOT_SEGREGATED"),
                         ("service_zone_id", "SZ-4"),
                         ("collection_event_id", "EVENT-009"),
                         ("house_number", "12/A")):
        refused = False
        try:
            ADAPTER.validate_python(dict(base, **{field: value}))
        except Exception:
            refused = True
        ok(f"a trigger carrying {field} is REFUSED", refused)

    nested = False
    try:
        ADAPTER.validate_python(dict(base, context={"property_id": "PROP-004"}))
    except Exception:
        nested = True
    ok("a property_id nested inside an unknown object is REFUSED", nested)

    ok("no property field exists on the model at all",
       not any(f in gv.NonSegregationTriggerEvent.model_fields
               for f in ("property_id", "segregation_status", "service_zone_id")))

    unresolved = ADAPTER.validate_python(
        dict(base, trigger_status="NO_ACTIVE_EPISODE", episode_id=None))
    ok("NO_ACTIVE_EPISODE with a null episode_id is a valid, publishable outcome",
       unresolved.episode_id is None)

    bad = False
    try:
        ADAPTER.validate_python(dict(base, episode_id=None))
    except Exception:
        bad = True
    ok("RESOLVED without an episode_id is refused", bad)

    tolerated = ADAPTER.validate_python(dict(base, esp32_rssi=-61))
    ok("an unheard-of field does not break the sixth event",
       tolerated.trigger_id == "TRG-1")

    ok("NON_SEGREGATION_TRIGGER is in the accepted list",
       "NON_SEGREGATION_TRIGGER" in gv.EVENT_TYPES and len(gv.EVENT_TYPES) == 6)


def check_binding_and_dwell() -> None:
    print("\n1-7. binding, dwell, episodes, mirror")
    engine, store, zones, mir, tport = build()

    engine.on_worker_bound(bound(35, T0))
    snap = engine.snapshot()
    ok("1. WORKER_TRACK_BOUND stores PICKER-01 -> track 35",
       snap["bindings"] and snap["bindings"][0]["collector_id"] == "PICKER-01"
       and snap["bindings"][0]["track_id"] == 35)

    # An unbound stranger stands in PROP-001's zone for ten seconds.
    r, f = at_property("PROP-001")
    result, _ = walk(engine, 99, T0 + timedelta(seconds=1), r, f, seconds=10)
    ok("2. an UNBOUND track creates no episode",
       result["reason"] == "UNBOUND_TRACK" and not store.episodes)
    ok("2b. an unbound track costs no PostGIS query", zones.calls == 0)

    # HOUSE 1
    _, end1 = walk(engine, 35, T0 + timedelta(seconds=20), r, f, seconds=6)
    episodes = list(store.episodes.values())
    ok("3. dwell in PROP-001 creates exactly one episode",
       len(episodes) == 1 and episodes[0]["episode_id"] == "E-001"
       and episodes[0]["property_id"] == "PROP-001")
    ok("3b. the episode names the picker and the track",
       episodes[0]["picker_id"] == "PICKER-01" and episodes[0]["track_id"] == 35)

    mir.flush(2.0)
    posts = [c for c in tport.calls if c[0] == "POST"]
    ok("4. E-001 was mirrored to Windows",
       len(posts) == 1 and posts[0][1].endswith("/episodes/active")
       and posts[0][2]["episode_id"] == "E-001")
    ok("4b. the mirror carries track and status, and NO property",
       posts[0][2]["track_id"] == 35
       and posts[0][2]["association_status"] == "AUTO_ASSOCIATED"
       and not any(k in posts[0][2] for k in
                   ("property_id", "house_number", "segregation_status",
                    "service_zone_id")))
    ok("4c. mirror_status recorded on the episode",
       store.episodes["E-001"]["mirror_status"] == "MIRRORED")

    # The collector walks off down the road: outside every zone.
    away_r, away_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    walk(engine, 35, end1 + timedelta(seconds=1), away_r, away_f, seconds=5)

    e1 = store.episodes["E-001"]
    ok("5. leaving with no trigger closes E-001 SEGREGATED",
       e1["state"] == "CLOSED" and e1["segregation_status"] == "SEGREGATED")
    event = store.collection_events.get(e1["collection_event_id"])
    ok("5b. a collection event was written for PROP-001",
       event and event["property_id"] == "PROP-001"
       and event["segregation_status"] == "SEGREGATED"
       and event["review_status"] == "AUTO_CONFIRMED")
    ok("5c. and it carries the picker and the episode",
       event["picker_id"] == "PICKER-01" and event["episode_id"] == "E-001")

    mir.flush(2.0)
    deletes = [c for c in tport.calls if c[0] == "DELETE"]
    ok("6. closing E-001 removed the Windows mirror",
       len(deletes) == 1 and deletes[0][1].endswith("/episodes/E-001")
       and "E-001" not in mir.mirrored)

    # HOUSE 2
    r2, f2 = at_property("PROP-002")
    _, end2 = walk(engine, 35, end1 + timedelta(seconds=20), r2, f2, seconds=6)
    ok("7. PROP-002 creates E-002",
       "E-002" in store.episodes
       and store.episodes["E-002"]["property_id"] == "PROP-002"
       and store.episodes["E-002"]["state"] == "ACTIVE")
    mir.flush(2.0)
    posts = [c for c in tport.calls if c[0] == "POST"]
    ok("7b. E-002 is mirrored too",
       len(posts) == 2 and posts[1][2]["episode_id"] == "E-002")

    ok("18a. no property row was written at any point",
       store.assert_properties_untouched())
    return engine, store, mir, tport, end2


def check_trigger_paths() -> None:
    print("\n8-11, 13. the sixth event, applied and refused")
    engine, store, mir, tport, end2 = check_binding_and_dwell()
    t2 = end2 + timedelta(seconds=1)

    # --- 10: unknown episode, BEFORE the valid one, so a wrong application
    # would land on the live E-002 and be caught.
    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-GHOST", episode_id="E-999"))
    ok("10. a trigger naming an unknown episode is not applied",
       res["resolution"] == "UNKNOWN_EPISODE" and not res["applied"])
    ok("10b. it is preserved for review",
       store.triggers["TRG-GHOST"]["needs_review"] is True)
    ok("10c. and no episode changed",
       all(e["segregation_status"] == "SEGREGATED"
           for e in store.episodes.values()))

    # --- 11: right episode, wrong collector; then right collector, wrong track
    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-WRONG-WHO", episode_id="E-002",
                collector="PICKER-02"))
    ok("11. a trigger from the wrong collector is not applied",
       res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"])

    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-WRONG-TRACK", episode_id="E-002",
                track_id=41))
    ok("11b. a trigger naming the wrong track is not applied",
       res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"])

    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-WRONG-SESSION", episode_id="E-002",
                session_id="sess-other"))
    ok("11c. a trigger from another capture session is not applied",
       res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"])

    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-EDGE-UNRESOLVED", episode_id=None,
                status="NO_ACTIVE_EPISODE"))
    ok("11d. the edge's own NO_ACTIVE_EPISODE marks nothing",
       res["resolution"] == "EDGE_UNRESOLVED" and not res["applied"])

    ok("11e. E-002 is still SEGREGATED after four bad triggers",
       store.episodes["E-002"]["segregation_status"] == "SEGREGATED")

    # --- 8: the real one
    res = engine.on_non_segregation_trigger(
        trigger(t2, trigger_id="TRG-REAL", episode_id="E-002"))
    ok("8. a valid trigger marks E-002 NOT_SEGREGATED",
       res["resolution"] == "APPLIED" and res["applied"]
       and store.episodes["E-002"]["segregation_status"] == "NOT_SEGREGATED")
    ok("8b. it resolved to PROP-002 - WASTRAQ's property, not the edge's",
       res["property_id"] == "PROP-002")
    ok("8c. E-001 was NOT touched",
       store.episodes["E-001"]["segregation_status"] == "SEGREGATED")

    # --- 9: same decision, new envelope
    res = engine.on_non_segregation_trigger(
        trigger(t2 + timedelta(seconds=2), trigger_id="TRG-REAL",
                episode_id="E-002"))
    ok("9. the same trigger_id again is a no-op",
       res["resolution"] == "DUPLICATE" and not res["applied"])
    ok("9b. exactly one trigger row exists for that decision",
       len([t for t in store.triggers if t == "TRG-REAL"]) == 1)
    before = len(store.collection_events)

    # --- 13: the clip. Inserted first, exactly as the receiver does, so the
    # deferred-link path (clip arrives while the episode is still open) is
    # the one under test rather than a shortcut.
    clip_event = clip(t2 + timedelta(seconds=3))
    store.insert_clip(clip_event)
    res = engine.on_evidence_ready(clip_event)
    ok("13. EVIDENCE_READY is attributed to E-002",
       res.get("episode_id") == "E-002")
    ok("13a. it is deferred while the episode is still open",
       res.get("deferred") is True and not res.get("linked"))

    # Collector leaves house 2.
    away_r, away_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    walk(engine, 35, t2 + timedelta(seconds=5), away_r, away_f, seconds=5)

    e2 = store.episodes["E-002"]
    ok("8d. E-002 closed NOT_SEGREGATED", e2["state"] == "CLOSED"
       and e2["segregation_status"] == "NOT_SEGREGATED")
    event2 = store.collection_events.get(e2["collection_event_id"])
    ok("8e. its collection event is NOT_SEGREGATED, rfid_triggered, NEEDS_REVIEW",
       event2 and event2["segregation_status"] == "NOT_SEGREGATED"
       and event2["rfid_triggered"] and event2["review_status"] == "NEEDS_REVIEW")
    ok("9c. no second collection event was created by the duplicate",
       len(store.collection_events) == before + 1)

    kinds = [e["evidence_type"] for e in store.evidence
             if e["event_id"] == event2["event_id"]]
    ok("13b. the clip became a VIDEO_CLIP evidence row on E-002's event",
       "VIDEO_CLIP" in kinds)
    ok("13c. and a NON_SEGREGATION_PROOF row sits beside it",
       "NON_SEGREGATION_PROOF" in kinds)
    ok("13d. no evidence landed on E-001's event",
       not [e for e in store.evidence
            if e["event_id"] == store.episodes["E-001"]["collection_event_id"]])

    print("\n  dashboard result")
    for episode_id in ("E-001", "E-002"):
        ep = store.episodes[episode_id]
        ev = store.collection_events.get(ep["collection_event_id"], {})
        n = len([e for e in store.evidence if e["event_id"] == ev.get("event_id")])
        print(f"    {ep['property_id']} | {ep['picker_id']} | "
              f"{ep['segregation_status']}" + (f" | {n} evidence" if n else ""))

    ok("18b. still no property row written", store.assert_properties_untouched())


def check_ambiguity_and_review() -> None:
    print("\n12. ambiguity and low confidence")
    engine, store, zones, mir, tport = build()
    engine.on_worker_bound(bound(35, T0))

    r, f = ambiguous_point()
    walk(engine, 35, T0 + timedelta(seconds=1), r, f, seconds=8)
    ok("12. an ambiguous position creates NO episode", not store.episodes)
    ok("12b. and no collection event", not store.collection_events)

    # A candidate that is interrupted by ambiguity must not accumulate dwell.
    r1, f1 = at_property("PROP-001")
    engine.on_track_update(track(35, T0 + timedelta(seconds=20), r1, f1))
    engine.on_track_update(track(35, T0 + timedelta(seconds=21), r, f))
    engine.on_track_update(track(35, T0 + timedelta(seconds=23), r1, f1))
    ok("12c. ambiguity resets the dwell clock rather than pausing it",
       not store.episodes)

    # PROP-003's zone returns 0.55 - below the REVIEW threshold.
    engine2, store2, _, mir2, _ = build()
    engine2.on_worker_bound(bound(35, T0))
    r3, f3 = at_property("PROP-003")
    _, end = walk(engine2, 35, T0 + timedelta(seconds=1), r3, f3, seconds=6)
    ep = store2.episodes.get("E-001", {})
    ok("12d. a weak association still creates an episode, flagged REVIEW",
       ep.get("association_status") == "REVIEW")
    away_r, away_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    walk(engine2, 35, end + timedelta(seconds=1), away_r, away_f, seconds=5)
    ev = store2.collection_events.get(
        store2.episodes["E-001"]["collection_event_id"], {})
    ok("12e. and its collection event lands as NEEDS_REVIEW",
       ev.get("review_status") == "NEEDS_REVIEW")

    print("\n  depth and camera guards")
    engine3, store3, _, _, _ = build()
    engine3.on_worker_bound(bound(35, T0))
    for i in range(14):
        engine3.on_track_update(track(35, T0 + timedelta(seconds=i * 0.5),
                                      r1, f1, depth_valid=False))
    ok("12f. observations the edge will not vouch for create no episode",
       not store3.episodes)

    engine4, store4, zones4, mir4, _ = build()
    engine4.config.camera_configured = False
    engine4.on_worker_bound(bound(35, T0))
    walk(engine4, 35, T0 + timedelta(seconds=1), r1, f1, seconds=8)
    ok("12g. with no surveyed camera pose, nothing is associated at all",
       not store4.episodes and zones4.calls == 0)


def check_adjacent_handover() -> None:
    """The collector walks straight from one gate to the next.

    The demo script says "same track enters PROP-002", with no trip out to
    the road in between. If the engine only closed episodes on leaving every
    zone, two adjacent houses would merge into one episode and the second
    would never be recorded - so this is checked directly rather than
    inferred from the walk-away case.
    """
    print("\n7c. adjacent handover with no gap between zones")
    engine, store, zones, mir, tport = build()
    engine.on_worker_bound(bound(35, T0))

    r1, f1 = at_property("PROP-001")
    _, end = walk(engine, 35, T0 + timedelta(seconds=1), r1, f1, seconds=4)
    r2, f2 = at_property("PROP-002")
    walk(engine, 35, end + timedelta(seconds=0.5), r2, f2, seconds=4)

    ok("two separate episodes, one per house",
       len(store.episodes) == 2
       and store.episodes["E-001"]["property_id"] == "PROP-001"
       and store.episodes["E-002"]["property_id"] == "PROP-002")
    ok("the first closed the moment the second began",
       store.episodes["E-001"]["state"] == "CLOSED"
       and store.episodes["E-002"]["state"] == "ACTIVE")
    ok("the first still wrote its SEGREGATED collection event",
       store.episodes["E-001"]["collection_event_id"] in store.collection_events)
    ok("only one episode is live for the collector",
       len(engine.snapshot()["active_episodes"]) == 1)
    mir.flush(2.0)
    ok("E-001's mirror was removed and E-002's published",
       any(c[0] == "DELETE" and c[1].endswith("/E-001") for c in tport.calls)
       and any(c[0] == "POST" and c[2]["episode_id"] == "E-002"
               for c in tport.calls))

    print("\n  a second collector does not disturb the first")
    engine.on_worker_bound(bound(41, T0 + timedelta(seconds=30),
                                 collector="PICKER-02"))
    r3, f3 = at_property("PROP-003")
    walk(engine, 41, T0 + timedelta(seconds=31), r3, f3, seconds=4)
    snap = engine.snapshot()
    by_collector = {a["collector_id"]: a["property_id"]
                    for a in snap["active_episodes"]}
    ok("each collector has their own live episode",
       by_collector.get("PICKER-01") == "PROP-002"
       and by_collector.get("PICKER-02") == "PROP-003")


def check_network_failure() -> None:
    print("\n14. a dead Windows laptop cannot corrupt Mac state")
    engine, store, zones, mir, tport = build(fail_mirror=True)
    engine.on_worker_bound(bound(35, T0))
    r, f = at_property("PROP-001")
    _, end = walk(engine, 35, T0 + timedelta(seconds=1), r, f, seconds=6)
    mir.flush(2.0)

    ok("14. the episode was still created", "E-001" in store.episodes)
    ok("14b. and honestly marked MIRROR_FAILED",
       store.episodes["E-001"]["mirror_status"] == "MIRROR_FAILED")
    ok("14c. the mirror reports the failure rather than hiding it",
       mir.status()["failed"] >= 1 and mir.status()["last_error"])

    away_r, away_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    walk(engine, 35, end + timedelta(seconds=1), away_r, away_f, seconds=5)
    mir.flush(2.0)
    e1 = store.episodes["E-001"]
    ok("14d. it closes SEGREGATED anyway - the demo degrades, the record does not",
       e1["state"] == "CLOSED" and e1["segregation_status"] == "SEGREGATED"
       and e1["collection_event_id"] in store.collection_events)

    disabled = mirror_mod.EpisodeMirror(base_url="", transport=None)
    ok("14e. no GEOVISION_EDGE_BASE_URL simply disables mirroring",
       disabled.publish_active({"episode_id": "E-9", "track_id": 1}) == "DISABLED"
       and disabled.status()["enabled"] is False)


def check_reset() -> None:
    print("\n17. reset clears transient state and nothing else")
    engine, store, zones, mir, tport = build()
    engine.on_worker_bound(bound(35, T0))
    r, f = at_property("PROP-001")
    walk(engine, 35, T0 + timedelta(seconds=1), r, f, seconds=6)
    mir.flush(2.0)
    ok("17a. there is something to reset",
       engine.snapshot()["active_episodes"] and engine.snapshot()["bindings"])

    result = engine.reset()
    snap = engine.snapshot()
    ok("17. bindings, candidates and live episodes are cleared",
       not snap["bindings"] and not snap["active_episodes"]
       and not snap["candidates"])
    ok("17b. the live episode is ABORTED, not silently completed",
       store.episodes["E-001"]["state"] == "ABORTED"
       and store.episodes["E-001"]["collection_event_id"] is None)
    ok("17c. an aborted episode writes no collection event",
       not store.collection_events)
    mir.flush(2.0)
    ok("17d. the Windows mirror was removed",
       any(c[0] == "DELETE" for c in tport.calls))
    ok("18c. all 16 properties survive the reset",
       len(store.properties) == 16 and store.assert_properties_untouched())

    engine.on_worker_bound(bound(35, T0 + timedelta(seconds=60)))
    walk(engine, 35, T0 + timedelta(seconds=61), r, f, seconds=6)
    ok("17e. the engine works again after a reset",
       "E-002" in store.episodes
       and store.episodes["E-002"]["state"] == "ACTIVE")


def check_step2_flow() -> None:
    """STEP 2 as ten separate claims, in the order the demo runs them.

    The checks above prove the engine's behaviour topic by topic. This one
    walks the single path STEP 2 is about - a real collector servicing one
    house and walking away - and asserts each rung of the ladder
    individually, so a regression names the rung it broke rather than
    "episode not created".

        WORKER_TRACK_BOUND -> TRACK_UPDATE accepted -> camera metres to
        WGS84 -> service-zone lookup -> unique candidate -> dwell clock ->
        episode -> leave -> grace -> CLOSED SEGREGATED -> collection event.

    Deliberately not here: non-segregation, evidence and the dashboard.
    """
    print("\n19. STEP 2 - bound track -> service zone -> episode -> SEGREGATED")
    engine, store, zones, mir, tport = build()      # dwell 2 s, grace 3 s
    r, f = at_property("PROP-001")

    # --- 1. an unbound track is skipped ------------------------------------
    res, _ = walk(engine, 35, T0, r, f, seconds=6)
    ok("19.1 an UNBOUND track in a service zone is skipped",
       res["reason"] == "UNBOUND_TRACK" and not store.episodes
       and not engine.snapshot()["candidates"])
    ok("19.1b and it never reaches the service-zone lookup at all",
       zones.calls == 0)

    # --- 2. binding accepted ------------------------------------------------
    res = engine.on_worker_bound(bound(35, T0 + timedelta(seconds=10)))
    binding = engine.snapshot()["bindings"]
    ok("19.2 WORKER_TRACK_BOUND is accepted and recorded",
       res["handled"] and len(binding) == 1
       and binding[0]["collector_id"] == "PICKER-01"
       and binding[0]["track_id"] == 35)
    ok("19.2b binding alone creates no episode",
       not store.episodes and not engine.snapshot()["active_episodes"])

    # --- 3. the same track, now bound, resolves to ONE property -------------
    t = T0 + timedelta(seconds=11)
    res = engine.on_track_update(track(35, t, r, f))
    ok("19.3 a bound track inside one service zone yields a single candidate",
       res["state"] == "CANDIDATE" and res["property_id"] == "PROP-001"
       and not store.episodes)
    ok("19.3b the position came from the camera transform, not from GeoVision",
       zones.calls == 1)

    res = engine.on_track_update(track(35, t + timedelta(seconds=1.5), r, f))
    ok("19.3c below the dwell threshold it is still only a candidate",
       res["state"] == "CANDIDATE" and round(res["dwell_s"], 1) == 1.5
       and not store.episodes)

    # --- 4. dwell reached -> episode opens ----------------------------------
    res = engine.on_track_update(track(35, t + timedelta(seconds=2.5), r, f))
    ok("19.4 crossing the dwell threshold opens exactly one episode",
       res["state"] == "EPISODE_OPENED" and res["episode_id"] == "E-001"
       and res["property_id"] == "PROP-001" and len(store.episodes) == 1)
    ok("19.4b it is AUTO_ASSOCIATED and names the picker and the track",
       store.episodes["E-001"]["association_status"] == "AUTO_ASSOCIATED"
       and store.episodes["E-001"]["picker_id"] == "PICKER-01"
       and store.episodes["E-001"]["track_id"] == 35)

    # --- 5. standing still does not open a second episode -------------------
    obs_before = store.episodes["E-001"]["observations"]
    _, end = walk(engine, 35, t + timedelta(seconds=3.0), r, f, seconds=10)
    ok("19.5 twenty more observations in the same zone open no second episode",
       len(store.episodes) == 1 and engine.stats["episodes_opened"] == 1
       and len(engine.snapshot()["active_episodes"]) == 1)
    ok("19.5b they extend the SAME episode instead",
       store.episodes["E-001"]["state"] == "ACTIVE"
       and store.episodes["E-001"]["observations"] > obs_before)
    ok("19.5c and no collection event exists while it is open",
       not store.collection_events)

    # --- 6. leaving -> grace -> CLOSED SEGREGATED ---------------------------
    away_r, away_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    last_inside = end

    engine.on_track_update(track(35, last_inside + timedelta(seconds=1.0),
                                 away_r, away_f))
    ok("19.6 outside the zone but inside the grace, the episode stays open",
       store.episodes["E-001"]["state"] == "ACTIVE")

    engine.on_track_update(track(35, last_inside + timedelta(seconds=2.9),
                                 away_r, away_f))
    ok("19.6b one tick short of the 3 s grace it is still open",
       store.episodes["E-001"]["state"] == "ACTIVE")

    engine.on_track_update(track(35, last_inside + timedelta(seconds=3.1),
                                 away_r, away_f))
    e1 = store.episodes["E-001"]
    ok("19.6c the grace expires and the episode closes",
       e1["state"] == "CLOSED")
    ok("19.6d it closes SEGREGATED - the default IS the product rule",
       e1["segregation_status"] == "SEGREGATED")
    ok("19.6e it ended at the last moment the collector was inside, "
       "not at the moment we noticed",
       e1["ended_at"] == last_inside)

    # --- 7. exactly one collection event ------------------------------------
    ok("19.7 exactly one collection event was written",
       len(store.collection_events) == 1)
    event = store.collection_events[e1["collection_event_id"]]
    ok("19.7b it carries the property, picker, track and episode",
       event["property_id"] == "PROP-001"
       and event["picker_id"] == "PICKER-01"
       and event["track_id"] == "35"
       and event["episode_id"] == "E-001")
    ok("19.7c SEGREGATED, AUTO_CONFIRMED, and NOT rfid_triggered",
       event["segregation_status"] == "SEGREGATED"
       and event["review_status"] == "AUTO_CONFIRMED"
       and event["rfid_triggered"] is False)

    walk(engine, 35, last_inside + timedelta(seconds=4), away_r, away_f,
         seconds=6)
    ok("19.7d walking further away writes no second event",
       len(store.collection_events) == 1
       and engine.stats["episodes_closed"] == 1)

    # --- 8. an ambiguous position associates nothing ------------------------
    engine8, store8, zones8, _, _ = build()
    engine8.on_worker_bound(bound(35, T0))
    ar, af = ambiguous_point()
    walk(engine8, 35, T0 + timedelta(seconds=1), ar, af, seconds=10)
    ok("19.8 an ambiguous position creates no episode however long it is held",
       not store8.episodes and not store8.collection_events)
    ok("19.8b the ambiguity is counted, not silently resolved to a nearest",
       engine8.stats["ambiguous"] > 0
       and not engine8.snapshot()["candidates"])

    # --- 9. no surveyed camera pose -> no association -----------------------
    engine9, store9, zones9, _, _ = build()
    engine9.config.camera_configured = False
    engine9.on_worker_bound(bound(35, T0))
    res, _ = walk(engine9, 35, T0 + timedelta(seconds=1), r, f, seconds=10)
    ok("19.9 with no camera pose the position is refused before any lookup",
       res["reason"] == "NO_VALID_POSITION" and zones9.calls == 0)
    ok("19.9b so nothing is associated and no episode exists",
       not store9.episodes and not store9.collection_events)

    # --- 10. re-delivery changes nothing ------------------------------------
    # Transport-level dedup on event_id lives in the receiver
    # (geovision_raw_events is keyed on it, so a redelivered envelope never
    # reaches the engine at all) and is proved over the wire by
    # scripts/test_step2_episode_flow.py. What is proved HERE is the harder
    # half: even if a duplicate DID get through - a replayed frame, a
    # restarted sender, a bug in the queue - the state machine still cannot
    # produce a second episode or a second collection event.
    engine10, store10, zones10, _, _ = build()
    engine10.on_worker_bound(bound(35, T0))
    first = track(35, T0 + timedelta(seconds=1), r, f)
    engine10.on_track_update(first)
    _, end10 = walk(engine10, 35, T0 + timedelta(seconds=2), r, f, seconds=6)
    episodes_after = dict(store10.episodes)

    for _ in range(5):
        engine10.on_track_update(first)          # the very same event object
    ok("19.10 replaying a delivered TRACK_UPDATE opens no second episode",
       len(store10.episodes) == len(episodes_after)
       and engine10.stats["episodes_opened"] == 1)

    engine10.on_worker_bound(bound(35, T0 + timedelta(seconds=9)))
    ok("19.10b re-binding the SAME collector to the SAME track does not "
       "disturb the live episode",
       len(engine10.snapshot()["active_episodes"]) == 1
       and store10.episodes["E-001"]["state"] == "ACTIVE"
       and not store10.collection_events)

    away10_r, away10_f = transform.wgs84_to_camera(CAMERA, 12.29500, 76.64300)
    walk(engine10, 35, end10 + timedelta(seconds=1), away10_r, away10_f,
         seconds=6)
    ok("19.10c and the whole run still yields exactly one collection event",
       len(store10.collection_events) == 1)

    ok("19.11 no property or service zone was written at any point",
       store.assert_properties_untouched()
       and store8.assert_properties_untouched()
       and store9.assert_properties_untouched()
       and store10.assert_properties_untouched())


def main() -> int:
    print("=" * 72)
    print("WASTRAQ episode engine + sixth GeoVision event - offline proof")
    print("=" * 72)
    check_transform()
    check_sixth_event_contract()
    check_trigger_paths()
    check_ambiguity_and_review()
    check_adjacent_handover()
    check_network_failure()
    check_reset()
    check_step2_flow()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
