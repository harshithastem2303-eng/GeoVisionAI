# GeoVision

Picker perception and RFID worker identity for **WASTRAQ**.

GeoVision answers one question and hands the answer to WASTRAQ:

> Which tracked people in this camera frame are actual garbage workers,
> who are they, and roughly where are we?

It does **not** decide which property is being served. Property Master, the
surveyed PostGIS geometry (entrances, frontages, service zones), verification,
GIS QA, final property matching and the evidence workflow all live in the main
WASTRAQ repository.

```
CAMERA
  ↓  YOLO detects every person
  ↓  BoT-SORT tracks every person
  ↓  RFID tap occurs
  ↓  RFID identifies the collector
  ↓  camera evidence zone identifies which track tapped
  ↓  collector ↔ track binding
  ↓  only bound tracks become AUTHORIZED_PICKER
  ↓  optional RealSense depth gives relative XYZ
  ↓  phone/laptop browser provides a coarse location
GeoVision emits clean worker observations → WASTRAQ associates properties
```

**The architectural rule:** nobody is a garbage worker for having been
detected first. RFID establishes *who*; camera spatial evidence establishes
*which current track*. When the evidence is weak or contested, the answer is
`AMBIGUOUS` and nothing is bound.

---

## Quick start

Cross-platform. Same commands on macOS and Windows apart from venv activation.

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows

pip install -r backend/requirements.txt

cp backend/.env.example backend/.env    # then edit it
```

Run the backend:

```bash
cd backend
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Run the dashboard:

```bash
cd frontend
npm install
npm run dev -- --host          # --host so a phone on the LAN can reach it
```

Run with **no hardware at all**:

```bash
GEOVISION_CAMERA_BACKEND=mock python -m uvicorn app:app --port 8000
```

Tests (no camera, no RFID reader, no database needed):

```bash
pytest backend/tests -v
# or, without pytest installed:
python -m unittest discover -s backend/tests -t backend/tests
```

---

## Configuration

Everything is environment-driven; see `backend/.env.example` for the full
list with comments. Nothing is hardcoded to one machine — no COM ports, no
`/dev/...` paths, no absolute paths, no credentials in source.

The two settings you will actually need to change:

| Variable | Why |
|---|---|
| `GEOVISION_RFID_ZONE` | `x1,y1,x2,y2` of the RFID reader **in the camera image**. The default is a guess and must be re-surveyed for the real installation. |
| `GEOVISION_VISION_SERIAL` | The RealSense serial to bind. Set it whenever more than one camera may be attached — startup refuses to guess. |

`backend/.env` is git-ignored. Never commit it.

---

## The RFID evidence zone

The physical RFID reader occupies a known rectangle of the camera frame.
Whoever taps it must be standing in that rectangle, which is what lets a tap
be attributed to a camera track.

On `POST /rfid/events`:

1. Resolve the tag to a collector. An unassigned tag is rejected immediately.
2. Take every tracked person within `RFID_MATCH_WINDOW_S` of the tap.
3. Score each track by its strongest overlap with the zone in that window.
4. Bind only if one track clears `RFID_MIN_OVERLAP` **and** beats the
   runner-up by `RFID_AMBIGUITY_MARGIN`.

Outcomes:

| Status | Meaning |
|---|---|
| `BOUND` | One person, unambiguously. `collector_id ↔ track_id` recorded. |
| `AMBIGUOUS` | Two or more plausible people. Candidates returned, **nothing bound**. |
| `NO_TRACK_IN_READER_ZONE` | Nobody was at the reader. |
| `NO_TRACK_DATA` | The camera wasn't running around that timestamp. |
| `UNKNOWN_RFID` | Tag not assigned to a collector. |

Every outcome returns HTTP 200 — an unresolved tap is a real result to record,
not a transport error to retry.

### Binding lifecycle

* A binding survives `GEOVISION_BINDING_GRACE_S` of the track being out of
  view, so walking behind the vehicle does not destroy identity.
* Every binding carries the **session id** it was made in. BoT-SORT renumbers
  when the pipeline restarts, so bindings are dropped on restart — track 14
  after a restart is a different human.
* There is no face recognition and no appearance-based identity. RFID is the
  identity anchor.

---

## API

### Perception

| Method | Path | Notes |
|---|---|---|
| `POST` | `/connect` | Open the camera; returns the negotiated profile |
| `POST` | `/start` | Begin capture under a fresh identity session |
| `POST` | `/stop`, `/disconnect` | |
| `GET` | `/stats` | fps, frames, people, authorised count, session |
| `GET` | `/people` | Every tracked person, authorised or not |
| `GET` | `/observations` | Full worker observations for WASTRAQ ingestion |
| `GET` | `/video_feed` | MJPEG |
| `GET` | `/camera`, `/health` | Subsystem diagnostics |

`GET /people` includes pedestrians on purpose:

```json
{ "track_id": 14, "is_authorized_picker": true,  "collector_id": "COLLECTOR-002" }
{ "track_id": 12, "is_authorized_picker": false, "collector_id": null }
```

### Identity

| Method | Path |
|---|---|
| `POST` | `/rfid/events` — `{"rfid_id": "AB12CD34", "timestamp": "..."}` |
| `GET` | `/worker-bindings` |
| `DELETE` | `/worker-bindings/{collector_id}` |
| `GET` | `/rfid/zone` |

### Location

| Method | Path |
|---|---|
| `POST` | `/location` — `{latitude, longitude, accuracy_m, timestamp, source}` |
| `GET` | `/location` |

### Preserved

`/collectors`, `/rfids`, `/rfids/assign`, `/rfids/{id}/state` — unchanged
CRUD the dashboard depends on.

`/frames`, `/latest_detection`, `/detection/not-segregated` — **legacy,
demo-only.** Proximity matching against a 50 m radius. Not authoritative
WASTRAQ property association; see `backend/legacy_property_match.py`. Do not
build on these.

---

## Location architecture

There is **no dedicated GNSS receiver and no IMU** in this demo.

```
LocationProvider
├── PhoneLocationProvider     (preferred — a phone browser posting fixes)
├── BrowserLocationProvider   (fallback — the laptop's own browser)
└── MockLocationProvider      (deterministic, for dev and tests)
```

A browser calls `navigator.geolocation.watchPosition` and POSTs each fix to
`/location`. No native app, no serial device, no OS-specific call — the same
path on Windows and macOS. The dashboard's **Share this device's location**
button does exactly this.

Consequently GeoVision never reports a heading, never dead-reckons and never
claims a precise world trajectory. `heading_deg` is always `null` and
`/health` states `imu: false`, `gnss_receiver: false`. Uncertainty travels
with the fix as `accuracy_m` plus `age_s`/`stale`, and WASTRAQ does the
conservative association.

Browsers only grant geolocation in a secure context. Over LAN a phone on
plain `http://` will be refused; the UI says so rather than failing silently.

---

## RealSense

Depth is **optional**. The API starts, the tests run and mock mode works with
no camera attached; `pyrealsense2` is imported lazily inside the RealSense
source only.

The startup sequence in `backend/vision/camera.py` follows a pattern proven
against physical D455 hardware, and the order is not stylistic:

* `enable_device(serial)` before `start(config)` — bind one camera, never guess.
* **No** `config.can_resolve()` on the startup path. It can turn a startable
  profile into a failure.
* **No** sensor or stream-profile enumeration before `start()`, and no device
  handle held open across it. Discovery reads identity fields only and
  releases every handle first.
* Intrinsics, depth scale and the negotiated profile are read **after** a
  successful `start()`, from the profile object it returned.

Stream profiles are unchanged from the working implementation: `640x480 BGR8
@ 30` colour and `640x480 Z16 @ 30` depth, now configurable.

Depth position: bottom-centre of the bounding box (`u=(x1+x2)/2, v=y2`),
median over a small neighbourhood rather than one pixel, de-projected to
camera-relative XYZ. Missing or invalid depth yields `null`, never a
fabricated number.

---

## Layout

```
backend/
├── vision/
│   ├── types.py            value objects, stdlib only
│   ├── camera.py           RealSense + mock frame sources
│   ├── detector.py         YOLO person detection + BoT-SORT
│   ├── track_history.py    time-indexed buffer of tracked people
│   ├── rfid_binding.py     tap → which track tapped the reader
│   ├── worker_registry.py  live collector ↔ track bindings
│   ├── depth_position.py   optional camera-relative XYZ
│   └── pipeline.py         the capture loop
├── location/
│   ├── base.py             LocationFix, LocationProvider, validation
│   ├── pushed.py           phone + laptop browser providers
│   ├── mock.py             deterministic provider
│   └── service.py          preference order and ingestion
├── routes/                 vision, rfid, location, collectors, legacy
├── tests/                  hardware-free
├── app.py, config.py, database.py, schemas.py, services.py, stream.py
└── legacy_property_match.py    quarantined, demo-only
```

## Not implemented here, on purpose

Property matching, service-zone scoring, frontage matching, PostGIS candidate
generation, non-segregation adjudication, face recognition, a trained
garbage-worker classifier, GNSS integration, IMU fusion, RTK, SLAM, vehicle
odometry. Those belong to WASTRAQ or to later work.
