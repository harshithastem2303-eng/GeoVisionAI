# Collection episodes, the sixth GeoVision event, and the Windows mirror

This document covers the layer that turns *perception* into *record*: a
camera track that belongs to a known collector, standing in a mapped service
zone, long enough to be servicing that property — and what happens when they
tap their card a second time.

It supersedes the RFID/episode assumptions in `docs/GEOVISION.md`, which
described the five-event ingestion floor before any of this existed. That
document is still correct about ingestion; this one is correct about what is
done with it.

---

## 1. Who decides what

| Question | Decided by | Never decided by |
|---|---|---|
| Who is collecting | GeoVision, from the RFID tap | WASTRAQ |
| Which camera track that person is | GeoVision, and it locks it | WASTRAQ |
| Where that track is, in camera metres | GeoVision (RealSense depth) | WASTRAQ |
| **Where that is on a map** | **WASTRAQ** (surveyed camera pose) | GeoVision |
| **Which property that is** | **WASTRAQ** (PostGIS service zones) | GeoVision |
| **Whether it was a collection** | **WASTRAQ** (dwell) | GeoVision |
| That the collector raised an exception | GeoVision (second tap) | — |
| **Which property the exception applies to** | **WASTRAQ** (the episode) | GeoVision |
| **SEGREGATED / NOT_SEGREGATED** | **WASTRAQ** | GeoVision |

The right-hand column is enforced, not merely intended. Every inbound event
passes `FORBIDDEN_PROPERTY_FIELDS` in
`backend/app/integrations/schemas.py`: an event carrying `property_id`,
`segregation_status`, `service_zone_id`, `collection_event_id` or any
sibling — at any depth, including inside an unknown object — is refused with
422 and never reaches the database. The sender strips the same fields before
publishing. Two independent guards, because the failure they prevent
(WASTRAQ quietly trusting a camera's guess about which house was served) is
the one failure this whole design exists to avoid.

---

## 2. The episode lifecycle

```
WORKER_TRACK_BOUND          PICKER-01 -> track 35
        |
        v
TRACK_UPDATE (5 Hz)         relative_x_m, relative_forward_m, depth_valid
        |
        |  camera pose (CAMERA_ORIGIN_LAT/LON, CAMERA_HEADING_DEG)
        v
   lat / lon
        |
        |  PostGIS: ST_Within against property_service_zones
        v
   PROP-001, confidence 0.97          or AMBIGUOUS -> nothing happens
        |
        |  held for EPISODE_DWELL_S
        v
   EPISODE E-001  ---- POST ----> Windows /episodes/active
        |
        |  left the zone for EPISODE_LEAVE_GRACE_S
        v
   CLOSED, SEGREGATED  ---- DELETE ----> Windows /episodes/E-001
        |
        v
   collection_events row + dashboard
```

Two defaults are the product rule, not implementation details:

* **An episode that closes without an accepted trigger is `SEGREGATED`.**
  The collector acts only on the exception. Silence is a decision.
* **An ambiguous position creates nothing.** Not a guess, not a "nearest",
  not a provisional row someone will forget to check.

### States

| State | Meaning | Writes a collection event |
|---|---|---|
| `ACTIVE` | collector is in the zone now | not yet |
| `CLOSED` | they left; the outcome is final | yes |
| `ABORTED` | cleared by `POST /episodes/reset` | **no** |

`ABORTED` exists so a demo restart cannot manufacture a collection nobody
performed.

---

## 3. The sixth event: `NON_SEGREGATION_TRIGGER`

A bound collector taps again. On the edge that means *"the waste at the
property I am servicing is not segregated"*. GeoVision does not know which
property that is, and this event does not say. It carries an `episode_id` —
an identifier **WASTRAQ minted and pushed to the edge's mirror** — and
WASTRAQ resolves it back to the property it associated by service zone.

```json
{
  "event_type": "NON_SEGREGATION_TRIGGER",
  "event_id":   "5f3c...",         // identifies the DELIVERY
  "timestamp":  "2026-08-29T09:14:02.331Z",
  "source_id":  "GEOVISION-D455-01",
  "session_id": "sess-abc",
  "trigger_id": "TRG-7",           // identifies the DECISION
  "episode_id": "E-002",
  "collector_id": "PICKER-01",
  "rfid_uid":   "04A1B2C3",
  "track_id":   35,
  "trigger_status": "RESOLVED",
  "duplicate":  false,
  "rfid_event_id": "rfid-2"
}
```

### Two kinds of deduplication, on purpose

* `event_id` — **transport**. A retried packet. Handled by the primary key
  on `geovision_raw_events`; the retry queue on the edge deliberately reuses
  the id so this works.
* `trigger_id` — **semantics**. The same *decision* re-announced under a
  fresh envelope, e.g. after the edge's queue restarts. Handled by the
  primary key on `geovision_non_segregation_triggers`. Without this, one tap
  could flag two houses.

### The verification ladder

A trigger is applied only when every one of these holds:

1. `trigger_id` has not been seen (else `DUPLICATE`).
2. `trigger_status` is not one the edge itself flagged as unresolved —
   `NO_ACTIVE_EPISODE`, `EPISODE_NOT_ACTIONABLE`, `UNKNOWN_RFID`, … (else
   `EDGE_UNRESOLVED`). The edge can **veto**; it can never **cause**.
3. `episode_id` is present (else `UNKNOWN_EPISODE`).
4. That episode exists **in WASTRAQ** (else `UNKNOWN_EPISODE`).
5. `collector_id`, `track_id`, `session_id` and `source_id` match the
   episode wherever both sides state a value (else `IDENTITY_MISMATCH`).
6. The episode is `ACTIVE`, or `CLOSED` within
   `EPISODE_TRIGGER_LATE_GRACE_S` (else `EPISODE_NOT_ACTIONABLE`).

Every refusal stores the trigger with `needs_review = true` and changes **no
property**. A signal that cannot be landed is preserved for a human; it is
never applied to the previous house, the nearest house, or the house that
happens to be open next.

Read the outcomes at `GET /episodes/triggers`.

---

## 4. The Windows episode mirror

GeoVision needs exactly one thing WASTRAQ knows: that an episode is live on
a given track, so a second tap has something to point at.

```
POST   {GEOVISION_EDGE_BASE_URL}/episodes/active
       {episode_id, track_id, association_status, collector_id?, session_id?}
DELETE {GEOVISION_EDGE_BASE_URL}/episodes/{episode_id}
GET    {GEOVISION_EDGE_BASE_URL}/episodes          (diagnostic)
```

* The payload is **rebuilt from a whitelist** (`mirror._SENDABLE`), so a
  property id cannot leak outward even if someone later adds one to the
  episode object. Windows strips such fields on arrival too.
* Sends run on **one daemon thread**. The edge times out inbound requests
  after 2 s; an episode starting inside a `TRACK_UPDATE` request must not
  spend that budget waiting on a sleeping laptop.
* **A failure cannot corrupt state.** It is recorded as
  `collection_episodes.mirror_status = MIRROR_FAILED` and shown on
  `GET /episodes/status`. The episode still closes `SEGREGATED` on its own.
  What is lost is the second-tap path, not the record.
* Windows expires its own mirror after `GEOVISION_EPISODE_MAX_AGE_S` (180 s),
  so a failed `DELETE` degrades rather than sticking.
* **No default host.** A hard-coded laptop IP in source is how a demo ends
  up posting to someone else's DHCP lease. Blank = mirroring off, which is
  correct with no edge attached.

---

## 5. Environment

Add to `backend/.env`. Only the camera pose has no safe default — without it
the engine makes **no association at all**, which is deliberate.

```bash
# --- REQUIRED for episodes -------------------------------------------------
# Where the camera stands, and the compass bearing of the direction it faces
# (degrees clockwise from true north: 0 = north, 90 = east).
CAMERA_ORIGIN_LAT=12.2943291
CAMERA_ORIGIN_LON=76.6414898
CAMERA_HEADING_DEG=90

# --- Windows episode mirror ------------------------------------------------
GEOVISION_EDGE_BASE_URL=http://<WINDOWS_IP>:8000
GEOVISION_EDGE_MIRROR_ENABLED=1
GEOVISION_EDGE_TIMEOUT_S=2.0
GEOVISION_EDGE_RETRIES=1

# --- engine tuning (all have working defaults) -----------------------------
EPISODE_ENGINE_ENABLED=1
EPISODE_DWELL_S=3.0
EPISODE_LEAVE_GRACE_S=4.0
EPISODE_MIN_ASSOC_INTERVAL_S=0.4
EPISODE_MAX_DURATION_S=180
EPISODE_REVIEW_CONFIDENCE=0.85
EPISODE_BINDING_TTL_S=900
EPISODE_TRIGGER_LATE_GRACE_S=30
EPISODE_EVIDENCE_LINK_WINDOW_S=120
EPISODE_REQUIRE_DEPTH_VALID=1
CAMERA_OFFSET_RIGHT_M=0
CAMERA_OFFSET_FORWARD_M=0
```

### Surveying the camera pose

The heading matters more than the position. A 10° heading error puts a
collector 10 m away about 1.7 m sideways — enough to land in the neighbour's
service zone.

1. Stand the camera on its tripod at the mark and record its lat/lon (the
   survey UI, or QGIS against the lane geometry).
2. Have someone stand **directly in front of the camera**, 8–10 m away, and
   record that lat/lon too.
3. `CAMERA_HEADING_DEG` = the bearing from (1) to (2).
4. Check it: `POST /gis/lookup` with the position the engine derives — see
   `GET /episodes/status`, which reports the camera pose it is using — and
   confirm the property it names is the one the person is standing in front
   of.

---

## 6. Endpoints

| Endpoint | What it is for |
|---|---|
| `GET /health/episodes` | is the engine armed, and does it know where the camera is |
| `GET /episodes` | recent episodes (joined to property and picker) |
| `GET /episodes/status` | **the one to watch during a demo**: bindings, dwell candidates, live episodes, mirror queue |
| `GET /episodes/triggers` | every second-tap signal and WASTRAQ's verdict on it |
| `GET /episodes/{id}` | one episode plus its triggers |
| `POST /episodes/reset` | clear transient state between runs |

`GET /episodes/status` is the diagnostic that matters, because it is the only
place that distinguishes the three failures that look identical from a
results dashboard:

* **not bound** — no `bindings` entry: the card never tapped, or the edge
  never sent `WORKER_TRACK_BOUND`.
* **bound but not in a zone** — a binding, no `candidates`: the camera pose
  is wrong, or the collector is genuinely outside every service zone.
* **in a zone but not long enough** — a `candidates` entry with a `dwell_s`
  below `EPISODE_DWELL_S`.

There is deliberately **no endpoint that sets a property on an episode by
hand.** The association is made by the PostGIS ladder or it is not made; an
override route would be the exact hole this architecture exists to close.
Correcting a wrong outcome is a review action on the collection event, where
it is recorded as a human decision.

### Reset

```bash
curl -X POST localhost:8000/episodes/reset \
     -H 'Content-Type: application/json' \
     -d '{"abort_active_episodes": true, "clear_edge_tracks": true}'
```

Clears: bindings, track ownership, dwell candidates, live episodes (marked
`ABORTED`, writing no collection event), Windows mirrors, and optionally the
`geovision_track_updates` position cache.

Never touches: properties, service zones, entrances, frontages, photos, past
collection events, stored evidence, or the raw event log.

---

## 7. Migration

```bash
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/episodes.sql
```

Additive and idempotent: two new tables, three nullable columns, one widened
`CHECK`, one new view. It refuses to run if
`database/geovision_integration.sql` or `database/schema.sql` have not been
applied, rather than failing halfway with an opaque error.

Order, always:

```
schema.sql -> seed / real_lane_16.sql -> geovision_integration.sql -> episodes.sql
```

`schema.sql` drops `properties CASCADE`, which would take
`collection_episodes` with it — so re-running it means re-running the last
two files afterwards.

New tables: `collection_episodes`, `geovision_non_segregation_triggers`.
New columns: `collection_events.episode_id`,
`geovision_evidence_clips.episode_id`,
`geovision_evidence_clips.linked_evidence_id`.
New view: `v_episode_summary`.

---

## 8. Tests

```bash
python3 scripts/test_episode_engine.py       # offline: engine, contract, mirror
python3 scripts/test_geovision_contract.py   # offline: the five original events
python3 scripts/test_episode_demo_http.py    # live: the two-house demo end to end
./scripts/verify_demo.sh                     # live: the existing checklist
```

`test_episode_engine.py` needs pydantic and nothing else — no database, no
FastAPI, no Windows laptop. It loads the engine into a synthetic package so
relative imports resolve without dragging in psycopg.

`test_episode_demo_http.py` is **self-configuring**: it reads the camera pose
from `/episodes/status` and the service-zone polygons from `/properties`,
then inverts the camera transform to work out what the edge would have
reported for a collector standing in each zone. Move the camera in `.env`
and the coordinates follow.
