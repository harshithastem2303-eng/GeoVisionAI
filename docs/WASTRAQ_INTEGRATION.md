# GeoVision → WASTRAQ integration

GeoVision is the **edge / perception** service. WASTRAQ is the **system of
record**. This document is the contract between them.

---

## 1. The architecture boundary

One rule decides where every piece of logic belongs:

> GeoVision reports **what the camera saw**.
> WASTRAQ decides **which house that means**.

GeoVision cannot see a service zone. It has a camera, a depth sensor, a
tracker, an RFID tap and a coarse browser location. Turning that into "the
waste at PROP-003 was not segregated" needs surveyed PostGIS geometry,
entrances, frontages and an ambiguity policy — none of which live here.

### What GeoVision owns

| | |
|---|---|
| RealSense colour + depth acquisition | `backend/vision/camera.py` |
| YOLO person detection, BoT-SORT track ids | `backend/vision/detector.py` |
| Per-track depth and camera-relative XYZ | `backend/vision/depth_position.py` |
| Absolute UTC timestamps on every observation | `backend/vision/pipeline.py` |
| RFID tap → which camera track tapped | `backend/vision/rfid_binding.py` |
| Live `collector_id ↔ track_id` bindings | `backend/vision/worker_registry.py` |
| Coarse location (phone/laptop browser) | `backend/location/` |
| Rolling evidence clips (local files) | `backend/evidence/buffer.py` |
| Rate-limited, retrying outbound events | `backend/integration/` |

### What WASTRAQ owns

Property Master · PostGIS service zones, entrances, frontages · world
trajectory interpretation · collection episode detection · candidate
property generation · the property matcher · confidence and ambiguity
policy · final property association · non-segregation claim state ·
evidence review and dashboards.

WASTRAQ also **pushes** the one piece of that state GeoVision needs back
down: which locked track currently has a collection episode open
(`POST /episodes/active`). That is a mirror, not a second implementation —
no dwell logic, no service zones and no property id live on the Windows
node, and the mirror is advisory: every re-tap outcome, resolved or not, is
still published to WASTRAQ for the final word.

### What GeoVision must never send

No event contains `property_id`, `property_name`, `service_zone_id`,
`segregation_status`, `collection_event_id` or any sibling. This is not left
to discipline: `integration/events.py` defines `PROPERTY_FIELDS` and every
builder runs `reject_property_fields()`, which strips and logs an error if
one ever appears. A test asserts it for all six event types, including
`NON_SEGREGATION_TRIGGER` — which is why that event says *that* a collector
flagged non-segregation and never *which* property, nor its
`segregation_status`.

GeoVision still contains a legacy 50 m nearest-GPS property matcher
(`backend/legacy_property_match.py`, `routes/legacy.py`). It is **demo-only
and quarantined**. It is not authoritative, and nothing it produces enters a
WASTRAQ event.

---

## 2. Environment variables

Copy the template and edit it — `backend/.env` is git-ignored:

```powershell
Copy-Item backend\.env.example backend\.env
notepad backend\.env
```

The variables that matter for integration:

| Variable | Default | Meaning |
|---|---|---|
| `WASTRAQ_BASE_URL` | *(empty)* | e.g. `http://192.168.1.23:8000`. Never hard-coded in source. |
| `WASTRAQ_INTEGRATION_ENABLED` | `false` | Master switch. Nothing is sent until this is `true` **and** a base URL is set. |
| `WASTRAQ_SOURCE_ID` | `GEOVISION-D455-01` | Which edge node produced the observation. |
| `WASTRAQ_EVENTS_PATH` | `/integrations/geovision/events` | Path appended to the base URL. |
| `WASTRAQ_TIMEOUT_S` | `2.0` | Per-request timeout. Short by design. |
| `WASTRAQ_TRACK_PUBLISH_HZ` | `5` | Outbound TRACK_UPDATE rate **per track**. `0` disables. |
| `WASTRAQ_QUEUE_MAX` | `500` | Bounded retry buffer; oldest dropped when full. |
| `WASTRAQ_RETRY_BACKOFF_S` | `5` | Delay before retrying a failed event. |
| `WASTRAQ_MAX_ATTEMPTS` | `5` | Attempts before an event is abandoned. |
| `WASTRAQ_HEARTBEAT_S` | `0` | Optional liveness beat. `0` = off. |
| `WASTRAQ_INCLUDE_GPS` | `true` | Attach the coarse fix to TRACK_UPDATE, in its own `gps` object. |
| `GEOVISION_GPS_SOURCE_ID` | `GEOVISION-GPS-01` | Source id on `/gps`. |
| `GEOVISION_RFID_SOURCE_ID` | `GEOVISION-RFID-01` | Source id on RFID_TAP. |
| `GEOVISION_EVIDENCE_ENABLED` | `true` | Rolling clip buffer on/off. |
| `GEOVISION_EVIDENCE_BUFFER_S` | `20` | Seconds retained in memory. |
| `GEOVISION_EVIDENCE_CAPTURE_HZ` | `10` | Frames retained per second (below camera FPS on purpose). |
| `GEOVISION_EVIDENCE_PRE_S` / `_POST_S` | `10` / `3` | Clip window around a trigger. |
| `GEOVISION_EVIDENCE_AUTO_ON_RFID` | `true` | Save a clip when a tap resolves to a track. |
| `GEOVISION_EVIDENCE_AUTO_ON_NON_SEGREGATION` | `true` | Save a clip when a bound collector flags non-segregation. |
| `GEOVISION_RFID_DEPTH_MARGIN_M` | `0.5` | Metres the closest person must beat the runner-up by, or the tap is `AMBIGUOUS`. |
| `GEOVISION_EPISODE_MAX_AGE_S` | `180` | How long a pushed episode stays live without an update from WASTRAQ. |
| `GEOVISION_NON_SEGREGATION_DEBOUNCE_S` | `10` | A bouncing reader raises one trigger, not several. |

Secrets: the only credential in this backend is `GEOVISION_DB_PASSWORD`, and
it has **no default** — an unset password fails loudly at first use rather
than shipping in source. Nothing logs it.

---

## 3. Endpoints

### Perception

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/tracks` | Normalised live track observations (WASTRAQ-facing name). |
| `GET` | `/people` | Identical payload (dashboard-facing name). |
| `GET` | `/observations` | Full internal observation records. |
| `GET` | `/stats` | FPS, frame count, session, authorised count. |
| `GET` | `/camera` | Negotiated RealSense profile, intrinsics, depth scale. |
| `GET` | `/video_feed` | MJPEG stream of annotated frames. |
| `POST` | `/connect` `/start` `/stop` `/disconnect` | Camera lifecycle. |

### Identity

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/rfid/events` | Ingest one RFID tap. Body: `{"rfid_uid": "...", "timestamp": "..."}` (or `rfid_id`). **What it does depends on state** — see section 3a. |
| `GET` | `/rfid/zone` | The configured evidence zone and the selection order, for drawing on the preview. |
| `GET` | `/worker-bindings` | Live `collector_id ↔ track_id` bindings, plus `active_picker_tracks`. |
| `DELETE` | `/worker-bindings/{collector_id}` | Release a binding (end of shift). Also drops that track's mirrored episode. |

### Episodes — **WASTRAQ writes these**

WASTRAQ is authoritative for property association and for the episode table.
These endpoints are an inbound mirror so a *second* RFID tap has a subject;
GeoVision opens no episode of its own and stores no property id.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/episodes/active` | WASTRAQ: "track 35 has episode E-12 open, AUTO_ASSOCIATED". |
| `DELETE` | `/episodes/{episode_id}` | WASTRAQ: "that collection is finished". |
| `GET` | `/episodes` | What this edge node currently believes. Diagnostic. |

Request body for `POST /episodes/active`:

```json
{
  "episode_id": "EP-HOUSE-2",
  "track_id": 35,
  "association_status": "AUTO_ASSOCIATED",
  "collector_id": "PICKER-01",
  "session_id": "9f2c1ab0c3d4"
}
```

`association_status` must be `AUTO_ASSOCIATED` or `REVIEW` for a re-tap to
flag it; anything else is mirrored but answers `EPISODE_NOT_ACTIONABLE`.
`session_id` is optional and checked when present — a track id from a
previous capture run names a different human, so it is rejected with
`STALE_SESSION`. Any property-naming field in the body (`property_id`,
`house_number`, `segregation_status`, …) is dropped before storage and
logged as an error. Always HTTP 200; `accepted: false` with a `reason` is a
state disagreement to record, not a transport error to retry.

### Location

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/location` | Push one browser Geolocation fix. |
| `GET` | `/location` | Best fix with source, accuracy, staleness. |
| `GET` | `/gps` | The same fix in the normalised GPS shape. |

### Integration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/integration/status` | The live diagnostic panel (section 7). |
| `POST` | `/integration/ping` | Send one HEARTBEAT now; reports success/failure. |
| `GET` | `/integration/queue` | Publisher counters. |
| `POST` | `/integration/evidence` | Manually request a clip. |
| `GET` | `/integration/evidence` | Recent clips and buffer state. |
| `GET` | `/health` | Every subsystem, including WASTRAQ and evidence. |

---

## 3a. What an RFID tap means

The reader sends the same eight hex digits every time. What the tap *does*
is decided by the state it lands in.

```
                    ┌──────────────────────────────┐
   tap ────────────►│ is this collector bound?     │
                    └───────┬──────────────┬───────┘
                            │ no           │ yes
                            ▼              ▼
                    IDENTIFY + LOCK   "not segregated here"
```

### Case 1 — no collector bound: identify and lock

1. Resolve the UID against the collector registry. An unknown tag does
   nothing at all — no binding, no flag.
2. Take every track the camera saw within `GEOVISION_RFID_MATCH_WINDOW_S`
   of the tap timestamp.
3. Choose the tapper, strongest evidence first:

   | Rule | When | Confidence |
   |---|---|---|
   | `DEPTH_IN_ZONE` | Valid-depth tracks overlap the reader zone → **closest to the camera** wins | zone coverage + metre separation |
   | `DEPTH_ANY` | Zone singles nobody out → every valid-depth track is eligible, **closest to the camera** wins | discounted; `reason` says why |
   | `ZONE_OVERLAP` | No usable depth anywhere in the window → original zone-overlap rule | as before |

4. The winner must be at least `GEOVISION_RFID_DEPTH_MARGIN_M` nearer than
   the runner-up. Two people shoulder to shoulder at the reader stay
   `AMBIGUOUS` and **nothing is bound**.
5. On success the track is **locked**: `PICKER-01 → track 35`.

The lock is the point. Nothing re-runs the "who is closest" question once a
binding exists — not the next frame, not a nearer stranger, not the
collector's own next tap. It is released only by the binding grace timeout
(`GEOVISION_BINDING_GRACE_S`), a new capture session, or
`DELETE /worker-bindings/{collector_id}`.

The response carries `intent: "BIND"` and `selection_rule` so the dashboard
can show *why* a track was chosen.

### Case 2 — bound collector, episode open: not segregated

The same card now means "the waste at the property I am servicing is not
segregated". Accepted only when all of these hold:

* the collector is already bound, and the UID resolves to **that** collector;
* WASTRAQ has an episode open on that collector's locked track;
* the episode is `AUTO_ASSOCIATED` or `REVIEW`.

Then the episode is flagged, a `NON_SEGREGATION_TRIGGER` is published and an
evidence clip is requested. **Idempotent**: a bounced reader, a card held too
long or a retried POST returns the *same* `trigger_id` with
`status: DUPLICATE_TRIGGER`, and no second clip.

### Case 3 — bound collector, no episode open

Nothing is marked. Not the previous property, not a nearby one, not a guess.
The response is `NO_ACTIVE_EPISODE` — and it is still published to WASTRAQ,
which holds the authoritative episode table and may know about a collection
this edge mirror does not.

### Statuses

| `status` | `intent` | Meaning |
|---|---|---|
| `BOUND` | `BIND` | Collector locked to a track |
| `AMBIGUOUS` | `BIND` | Two plausible people; nothing bound |
| `NO_TRACK_IN_READER_ZONE` | `BIND` | Nobody at the reader and no depth to rank by |
| `NO_TRACK_DATA` | `BIND` | Camera not running around that timestamp |
| `UNKNOWN_RFID` | `null` | Tag not assigned to a collector |
| `NON_SEGREGATION` | `NON_SEGREGATION` | Episode flagged; clip requested |
| `DUPLICATE_TRIGGER` | `NON_SEGREGATION` | Already flagged; nothing changed |
| `NO_ACTIVE_EPISODE` | `NON_SEGREGATION` | No open episode; **nothing marked** |
| `EPISODE_NOT_ACTIONABLE` | `NON_SEGREGATION` | Episode not associated confidently enough |

All of them are HTTP 200.

## 4. Event payloads

All events are `POST`ed as JSON to
`{WASTRAQ_BASE_URL}{WASTRAQ_EVENTS_PATH}`.

Every event carries the same envelope:

```json
{
  "event_type": "TRACK_UPDATE",
  "event_id": "0f0b0a4e-...",
  "timestamp": "2026-08-28T07:10:12.341Z",
  "source_id": "GEOVISION-D455-01"
}
```

`event_id` is a UUID. **WASTRAQ must deduplicate on it** — the retry queue
can legitimately deliver the same event twice.

### TRACK_UPDATE

```json
{
  "event_type": "TRACK_UPDATE",
  "event_id": "...",
  "timestamp": "2026-08-28T07:10:12.341Z",
  "source_id": "GEOVISION-D455-01",
  "session_id": "9f2c1ab0c3d4",
  "track_id": 17,
  "confidence": 0.94,
  "bbox": { "x1": 220, "y1": 90, "x2": 390, "y2": 470 },
  "depth_m": 3.54,
  "camera_x_m": -0.82,
  "camera_y_m": 0.14,
  "camera_z_m": 3.44,
  "relative_x_m": -0.82,
  "relative_forward_m": 3.44,
  "depth_valid": true,
  "depth_status": "OK",
  "is_authorized_picker": true,
  "collector_id": "GC-001",
  "identity_confidence": 0.88,
  "gps": {
    "timestamp": "2026-08-28T07:10:11.900Z",
    "latitude": 12.294209,
    "longitude": 76.641702,
    "accuracy_m": 8.0,
    "source": "PHONE",
    "age_s": 0.44,
    "stale": false,
    "altitude_m": null,
    "speed_mps": null,
    "hdop": null,
    "satellites": null,
    "heading_deg": null
  }
}
```

Notes:

- Coordinates are **camera-relative metres**, RealSense convention: `x`
  right, `y` down, `z` forward along the optical axis. No world position is
  implied.
- `depth_valid: false` means the numbers are `null`, not zero.
  `depth_status` says why: `NO_DEPTH_FRAME`, `NO_INTRINSICS`,
  `NO_DEPTH_SCALE`, `NO_VALID_SAMPLES`, `DEPROJECTION_FAILED`.
- `gps` is a **separate nested object** and is omitted when there is no fix.
  It is not fused with the tracker observation; the two are different
  measurements with different error models.
- `session_id` changes on every capture start. BoT-SORT renumbers on
  restart, so `track_id` is only meaningful **within** a session.

### RFID_TAP

```json
{
  "event_type": "RFID_TAP",
  "event_id": "...",
  "timestamp": "2026-08-28T07:10:12.341Z",
  "source_id": "GEOVISION-RFID-01",
  "rfid_uid": "04A1B2C3",
  "collector_id": "PICKER-01",
  "track_id": 17,
  "binding_status": "BOUND",
  "binding_confidence": 0.91,
  "candidate_track_ids": [17],
  "session_id": "9f2c1ab0c3d4"
}
```

`binding_status` is one of `BOUND`, `AMBIGUOUS`, `NO_TRACK_IN_READER_ZONE`,
`NO_TRACK_DATA`, `UNKNOWN_RFID`. **`track_id` is `null` unless the status is
`BOUND`.** When two people were at the reader, both appear in
`candidate_track_ids` and nothing is bound — the ambiguity is sent to
WASTRAQ rather than resolved by a guess.

### WORKER_TRACK_BOUND

```json
{
  "event_type": "WORKER_TRACK_BOUND",
  "event_id": "...",
  "timestamp": "...",
  "source_id": "GEOVISION-D455-01",
  "collector_id": "PICKER-01",
  "rfid_uid": "04A1B2C3",
  "track_id": 17,
  "confidence": 0.91,
  "session_id": "9f2c1ab0c3d4",
  "rfid_event_id": "<event_id of the RFID_TAP>"
}
```

### NON_SEGREGATION_TRIGGER

```json
{
  "event_type": "NON_SEGREGATION_TRIGGER",
  "event_id": "...",
  "timestamp": "2026-08-29T07:14:02.118Z",
  "source_id": "GEOVISION-RFID-01",
  "trigger_id": "6d0c1e4a-...",
  "episode_id": "EP-HOUSE-2",
  "collector_id": "PICKER-01",
  "rfid_uid": "04A1B2C3",
  "track_id": 35,
  "trigger_status": "NON_SEGREGATION",
  "duplicate": false,
  "session_id": "9f2c1ab0c3d4",
  "rfid_event_id": "<event_id of the RFID_TAP>"
}
```

A **signal, not a verdict**. It says the collector locked to track 35 tapped
their card while WASTRAQ's episode `EP-HOUSE-2` was open. WASTRAQ decides
which property that episode belongs to and sets its segregation status —
`segregation_status` is a forbidden field here for exactly that reason.

Deduplicate on `trigger_id`, not `event_id`: repeats carry the same
`trigger_id` with `duplicate: true`. Unresolved outcomes are sent too, with
`trigger_status` set to `NO_ACTIVE_EPISODE` or `EPISODE_NOT_ACTIONABLE` and
a null `episode_id`.

### EVIDENCE_READY

```json
{
  "event_type": "EVIDENCE_READY",
  "event_id": "...",
  "timestamp": "...",
  "source_id": "GEOVISION-D455-01",
  "track_id": 17,
  "rfid_event_id": "...",
  "episode_id": "EP-...",
  "clip_id": "CLIP-3f2a1b0c9d8e",
  "file_path": "C:\\...\\backend\\evidence_clips\\CLIP-3f2a1b0c9d8e.mp4",
  "file_url": "http://192.168.1.42:8000/evidence/clips/CLIP-3f2a1b0c9d8e/file",
  "file_name": "CLIP-3f2a1b0c9d8e.mp4",
  "content_type": "video/mp4",
  "size_bytes": 1483920,
  "sha256": "9f86d081...",
  "start_time": "2026-08-28T07:10:02.341Z",
  "end_time": "2026-08-28T07:10:15.341Z",
  "frame_count": 131,
  "session_id": "9f2c1ab0c3d4"
}
```

A **reference**, never bytes. The clip stays on the GeoVision machine;
WASTRAQ fetches it only if a human reviews the claim.

Two strings that must never be confused:

| Field | What it is | What WASTRAQ does with it |
|---|---|---|
| `file_path` | where the clip sits on **this Windows machine** | provenance only — stored, displayed, never opened or linked |
| `file_url` | how to **retrieve** the bytes | fetched on demand, then stored on the Mac |

`file_url` is derived from the clip id, never from the path, so it exposes no
filesystem layout. It is absolute when `GEOVISION_PUBLIC_BASE_URL` is set and
relative (`/evidence/clips/{clip_id}/file`) otherwise — this node does not
guess at a hostname it cannot verify, and WASTRAQ already knows how it
reached here.

`episode_id` is present when the clip was captured inside a collection
WASTRAQ had open on that track. Mirrored from WASTRAQ, never decided here,
and it names an *episode* — not a property.

All five retrieval fields are optional and `null` together when the file
cannot be resolved back inside the evidence directory. `null` means "no
fetchable file", never "fetch failed": the event still goes out so the clip
is on record.

The event is published **after** the file is closed and renamed into place,
so the URL is live the moment WASTRAQ reads it.

### Fetching a clip

```
GET /evidence/clips/{clip_id}/file
```

| Status | When |
|---|---|
| `200` | the clip; `video/mp4`, `Content-Disposition: inline`, `Accept-Ranges: bytes` so a `<video>` element can seek |
| `400` | malformed clip id (`{"code": "INVALID_CLIP_ID"}`) |
| `404` | no such clip, or one that resolves outside the evidence directory |
| `409` | the clip was written as a directory of stills, not one video file |
| `503` | `GEOVISION_EVIDENCE_SERVE_ENABLED=false` |

Read-only and idempotent — fetch it as often as you like; nothing on this
side changes. `HEAD` is served too, so size and content type can be checked
before committing to a download.

**Lookup is by clip id, never by path.** No route, query parameter or event
field on this side accepts a filesystem path from the network, so directory
traversal has no entry point rather than a filter in front of it. The id must
match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` (no separators, no `..`), and the
resolved file is re-checked against the resolved evidence root, which also
catches a symlink or junction planted *inside* that directory.

**No authentication.** The Mac sends no credential and this asks for none; a
shared secret living in two `.env` files on a demo LAN buys less than it
costs. Bind the API to the LAN interface, not to the internet — see
§ *Firewall*.

### HEARTBEAT (optional)

```json
{
  "event_type": "HEARTBEAT",
  "event_id": "...",
  "timestamp": "...",
  "source_id": "GEOVISION-D455-01",
  "status": { "...": "the /integration/status payload" }
}
```

---

## 5. Publishing behaviour

**Rate.** Tracking runs at full camera FPS. Publishing is throttled to
`WASTRAQ_TRACK_PUBLISH_HZ` **per track id**, so two workers in frame produce
two ~5 Hz streams rather than one combined stream. Nothing is sent at 30 FPS.

**Non-blocking.** `publish()` appends to an in-memory deque and returns. All
HTTP happens on the `wastraq-sender` background thread. An unreachable
WASTRAQ costs the RealSense loop nothing.

**Retry.** A failed event is requeued with a `WASTRAQ_RETRY_BACKOFF_S` delay
and up to `WASTRAQ_MAX_ATTEMPTS` attempts, keeping its original `event_id`.

**Bounded.** The queue holds at most `WASTRAQ_QUEUE_MAX` events. When full,
the **oldest** is dropped — during an outage a fresh position is worth more
than a stale one — and the drop is logged and counted in
`/integration/queue`.

---

## 6. Startup order

1. **WASTRAQ first**, on the Mac: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
2. **GeoVision second**, on the Windows laptop (see section 12).
3. `POST /connect` then `POST /start` to open the camera and begin capture.
4. Push a location fix (open the dashboard's location pusher on a phone) if
   you want `gps` blocks on the events.
5. Tap RFID cards.

GeoVision does not require WASTRAQ to be up. If WASTRAQ starts late, the
queued events drain on their own.

---

## 7. Live diagnostics

```powershell
curl.exe http://localhost:8000/integration/status
```

```json
{
  "source_id": "GEOVISION-D455-01",
  "realsense_connected": true,
  "camera_running": true,
  "tracking_active": true,
  "depth_available": true,
  "gps_valid": true,
  "rfid_available": false,
  "rfid_mode": "API_INGEST_ONLY",
  "wastraq_enabled": true,
  "wastraq_reachable": true,
  "pending_events": 0,
  "last_track_sent_at": "2026-08-28T07:10:12.341Z",
  "last_rfid_event_at": null
}
```

`wastraq_reachable` is the outcome of the **last real delivery**, and `null`
before anything has been sent. The endpoint performs no outbound request —
a status call that itself blocks on an unreachable host is useless during an
outage. Use `POST /integration/ping` to force a round trip.

---

## 8. Same-network requirements

Both machines must be on the same L2/L3 network and able to reach each
other's port 8000. Phone hotspot, site wifi or a direct router all work; a
guest network with client isolation does not.

```powershell
# On Windows, find this machine's address:
ipconfig
```

```bash
# On the Mac, find its address (this is what goes in WASTRAQ_BASE_URL):
ipconfig getifaddr en0
```

Verify from the Windows laptop before doing anything else:

```powershell
Test-NetConnection -ComputerName 192.168.1.23 -Port 8000
```

WASTRAQ must bind `0.0.0.0`, not `127.0.0.1`, or it is unreachable from
another machine.

---

## 9. Windows firewall

The first `uvicorn` run usually raises a Windows Defender prompt. Allow it
on **Private** networks. If the prompt was dismissed, add the rule
explicitly from an **elevated** PowerShell:

```powershell
New-NetFirewallRule -DisplayName "GeoVision 8000" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

Outbound connections to WASTRAQ are not normally blocked. If they are:

```powershell
New-NetFirewallRule -DisplayName "GeoVision to WASTRAQ" -Direction Outbound `
  -Protocol TCP -RemotePort 8000 -Action Allow -Profile Private
```

Also confirm the network is classified Private, not Public:

```powershell
Get-NetConnectionProfile
```

---

## 10. Timestamp synchronisation

Every GeoVision event is ISO-8601 **UTC with a `Z`** and millisecond
precision. WASTRAQ correlates GeoVision tracks with its own collection
episodes by time, so **clock skew between the two machines becomes
association error**.

Keep both machines on network time:

```powershell
# Windows, elevated:
w32tm /resync
w32tm /query /status
```

```bash
# macOS:
sudo sntp -sS time.apple.com
```

A skew of more than about a second is enough to misattribute a tap near a
zone boundary. Check it before any recorded run.

---

## 11. RealSense requirements

- Intel RealSense D455 (or D4xx), **USB 3.x** cable and port. USB 2 will
  enumerate the device and then fail to deliver frames at 640×480@30.
- `pyrealsense2` installed for the running interpreter.
- Streams: colour `640×480 BGR8 @30`, depth `640×480 Z16 @30`
  (`GEOVISION_CAMERA_*` to change).
- With two cameras attached, set `GEOVISION_VISION_SERIAL` — startup
  **refuses to guess** rather than binding the wrong one.
- Startup deliberately avoids `config.can_resolve()` and any pre-`start()`
  profile enumeration; both are known to break an otherwise startable
  configuration on this hardware. Introspection happens only after
  `pipeline.start()` returns.
- No camera at all? Set `GEOVISION_CAMERA_BACKEND=mock` — the API,
  dashboard and tests all run.

Check what was actually negotiated:

```powershell
curl.exe http://localhost:8000/camera
```

---

## 12. RFID: current support level

**There is no RFID reader driver in this repository.** Nothing reads a
serial port, a USB HID device or an MQTT topic. This is stated in
`/integration/status` as `rfid_available: false`,
`rfid_mode: "API_INGEST_ONLY"`.

What exists is the ingestion path a real reader will use unchanged:

```powershell
curl.exe -X POST http://localhost:8000/rfid/events `
  -H "Content-Type: application/json" `
  -d '{\"rfid_uid\":\"04A1B2C3\",\"timestamp\":\"2026-08-28T07:10:12.341Z\"}'
```

The tag is resolved to a collector against the `rfids` table, then attributed
to a camera track by the image-space **evidence zone** over the reader. When
the reader hardware is wired in, it posts to this endpoint and nothing in the
pipeline changes.

⚠️ The default evidence zone `220,240,420,470` is a **guess**. It must be
re-surveyed against the real reader installation
(`GEOVISION_RFID_ZONE=x1,y1,x2,y2`); `GET /rfid/zone` returns the current
value so it can be drawn over the video preview while measuring.

---

## 13. Evidence buffer: current support level

Enabled by default. Recent annotated frames are kept **JPEG-encoded** in
memory at `GEOVISION_EVIDENCE_CAPTURE_HZ` (10 Hz) for
`GEOVISION_EVIDENCE_BUFFER_S` (20 s) — single-digit megabytes, versus the
~550 MB a raw 30 FPS ring would need.

A clip covering `T-10s … T+3s` is written when an RFID tap resolves to a
track, or on `POST /integration/evidence`. Writing happens on its own thread
(the trailing three seconds do not exist yet when the trigger arrives), and
an `EVIDENCE_READY` event follows once the file exists.

Output is `.mp4` via OpenCV's `mp4v` writer, falling back to a directory of
numbered JPEGs if the codec is unavailable. Clips land in
`backend/evidence_clips/` (git-ignored).

Not yet validated on the physical D455 — see "Not tested" below. To take it
out of the loop entirely: `GEOVISION_EVIDENCE_ENABLED=false`.

---

## 14. Test procedure

### Automated (no hardware)

```powershell
cd C:\path\to\GeoVision
.\.venv\Scripts\activate
$env:PYTHONPATH="backend"
python -m unittest discover -s backend\tests -t backend\tests -v
# or, with pytest installed:
pytest backend\tests -v
```

Covers: track payload schema · depth conversion · every invalid-depth path ·
event serialisation · publish rate limiting · WASTRAQ unavailable · retry
and queue bounding · RFID normalisation including ambiguity · GPS
normalisation · and that no property field can reach a WASTRAQ event.

RealSense, the RFID reader and PostgreSQL are all absent from these tests.
**A green suite is not hardware validation.**

### Manual, no camera

```powershell
$env:GEOVISION_CAMERA_BACKEND="mock"
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --app-dir backend

curl.exe http://localhost:8000/health
curl.exe http://localhost:8000/integration/status
curl.exe -X POST http://localhost:8000/connect
curl.exe -X POST http://localhost:8000/start
curl.exe http://localhost:8000/tracks
curl.exe http://localhost:8000/gps
```

### Manual, with the camera and WASTRAQ

1. `WASTRAQ_INTEGRATION_ENABLED=true`, `WASTRAQ_BASE_URL=http://<mac-ip>:8000`.
2. Start WASTRAQ, then GeoVision.
3. `POST /integration/ping` → expect `{"delivered": true}`.
4. `POST /connect`, `POST /start`; walk in front of the camera.
5. `GET /tracks` → `depth_valid: true` and plausible `depth_m`.
6. Watch WASTRAQ's ingest log: roughly 5 TRACK_UPDATE per second per person.
7. Tap a card → RFID_TAP, then WORKER_TRACK_BOUND, then EVIDENCE_READY ~3 s later.
8. Stop WASTRAQ; keep walking. GeoVision must keep tracking;
   `/integration/status` shows `wastraq_reachable: false` and a rising
   `pending_events`.
9. Restart WASTRAQ. The queue drains; duplicate `event_id`s must be
   deduplicated on the WASTRAQ side.

---

## 15. Not tested

Everything below needs hardware this session could not reach, and is
**claimed as untested**, not as working:

- Per-track depth against a physical D455 (only mocked depth arrays).
- Whether JPEG encoding at 10 Hz measurably affects capture FPS on the
  demo laptop.
- The `mp4v` writer on that Windows machine (the stills fallback exists
  precisely because it may not be present).
- Any RFID reader hardware — none exists in this repository.
- End-to-end delivery into a running WASTRAQ instance.
