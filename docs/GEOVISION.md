# GeoVision edge ingestion

WASTRAQ's receiver for the Windows RealSense/RFID laptop.

    POST /integrations/geovision/events    ingest one edge event
    GET  /integrations/geovision/status    what has arrived, and from whom
    GET  /integrations/geovision/events    recent raw envelopes (inspection)

The sender's contract is authoritative:
`geovision-darshan/docs/WASTRAQ_INTEGRATION.md`, implemented in
`backend/integration/{events,client,publisher}.py` on that side.

---

## Where the boundary is

GeoVision **observes**. It has a depth camera, a person tracker and an RFID
reader. It can say *someone is 3.4 m in front of the camera* and *this card
was tapped at 07:10:12*. It cannot see a service-zone polygon, so it has no
basis for saying which house was served.

WASTRAQ **decides**. Property association is the PostGIS ladder in
`backend/app/gis.py`, run against surveyed entrance / frontage / service-zone
geometry, and it stays that way.

This receiver is the ingestion floor beneath that, and nothing more:

* No table it writes has a `property_id`, and none references `properties`,
  `property_service_zones` or `collection_events`.
* It never writes to `collection_events` or `evidence`. Those rows mean *a
  property was served*; nothing in an edge event knows that.
* An event that carries a property-association field is **refused with 422**,
  not stripped. The sender already strips them
  (`events.PROPERTY_FIELDS`); the receiver refuses them
  (`schemas.FORBIDDEN_PROPERTY_FIELDS`). Two independent guards, because the
  failure they prevent - WASTRAQ quietly trusting a camera's guess about
  which house was served - is the one this whole design exists to avoid.
* There is **no nearest-GPS matching here**, and there must not be. The `gps`
  block on a TRACK_UPDATE is a phone fix with ~8 m accuracy, kept as its own
  object precisely so it is never mistaken for a position.
* RFID answers **who and when**. It does not answer where.
* Ambiguity is stored as ambiguity. A tap the edge could not attribute to
  one person arrives as `binding_status: AMBIGUOUS` with every candidate
  track listed, and is stored that way. A tap that reports AMBIGUOUS *and*
  names a track has resolved the ambiguity by guessing somewhere upstream,
  so it is refused.

---

## Idempotency

`event_id` is a UUID and the primary key of `geovision_raw_events`. The
edge's retry queue reuses it across attempts, so the same event can
legitimately arrive twice.

Each request runs one transaction:

1. `INSERT INTO geovision_raw_events ... ON CONFLICT (event_id) DO NOTHING
   RETURNING event_id` - the raw envelope is on disk before anything is
   derived from it, and the primary key does the deduplication.
2. No row returned means it is a redelivery. Stop. Nothing downstream is
   written, no counter moves twice.
3. Otherwise write the normalised row, then mark `processed`.

A duplicate answers **200 with `duplicate: true`**, not 409. The sender
treats any non-2xx as a delivery failure and requeues with the same
`event_id`, so a 409 would produce an event that retries until it is
dropped. A duplicate is not an error - it is the retry queue working.

A malformed payload answers **422**. That one will not become valid by being
sent again, and it must not reach the database.

## Rate

The edge publishes TRACK_UPDATE at ~5 Hz **per track** and gives up after
2 seconds. Every accepted event costs three small statements on one
connection and no spatial work at all.

Track updates are therefore an **upsert of current state**, keyed by
`(source_id, session_id, track_id)` - not an append-only history. Two
pickers over an eight-hour shift is ~290k events and none of them is
interesting once the next one arrives. The raw envelope of every accepted
event is still kept.

`session_id` matters: BoT-SORT renumbers tracks on every capture restart, so
a `track_id` is only meaningful within one session.

If association work is added later it belongs **behind** this endpoint,
reading `geovision_raw_events`, not inside the request.

---

## Tables

| table | shape | holds |
|---|---|---|
| `geovision_raw_events` | one row per accepted event | the verbatim payload; `event_id` is the idempotency key |
| `geovision_track_updates` | one row per `(source, session, track)` | latest camera-relative observation + `observation_count` |
| `geovision_rfid_taps` | one row per tap | uid, collector, binding status, every candidate track |
| `geovision_worker_bindings` | append-only | `collector_id` ↔ `track_id` for a session |
| `geovision_evidence_clips` | one row per clip | a **reference**; the MP4 stays on the GeoVision machine |
| `geovision_devices` | one row per `source_id` | liveness and the edge's last self-diagnosis |

Apply the migration (additive, idempotent, safe on a live database):

```bash
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/geovision_integration.sql
```

## Settings

| env | default | meaning |
|---|---|---|
| `GEOVISION_ENABLED` | `1` | reported on the status endpoint |
| `GEOVISION_TRACK_STALE_S` | `15` | a track unseen for longer is not "active" |
| `GEOVISION_DEVICE_STALE_S` | `60` | a source silent for longer reads as offline |

---

## Running it

```bash
# WASTRAQ first, bound to the LAN so the Windows laptop can reach it
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000

# the Mac's address goes in the edge's WASTRAQ_BASE_URL
ipconfig getifaddr en0
```

One event by hand:

```bash
curl -sS -X POST http://127.0.0.1:8000/integrations/geovision/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"TRACK_UPDATE",
       "event_id":"11111111-2222-3333-4444-555555555555",
       "timestamp":"2026-08-28T07:10:12.341Z",
       "source_id":"GEOVISION-D455-01",
       "session_id":"9f2c1ab0c3d4",
       "track_id":17,"confidence":0.94,
       "bbox":{"x1":220,"y1":90,"x2":390,"y2":470},
       "depth_m":3.54,"camera_x_m":-0.82,"camera_y_m":0.14,"camera_z_m":3.44,
       "relative_x_m":-0.82,"relative_forward_m":3.44,
       "depth_valid":true,"depth_status":"OK",
       "is_authorized_picker":true,"collector_id":"PICKER-01",
       "identity_confidence":0.88}'
```

Send it twice: the first answers `202` with `"duplicate": false`, the second
`200` with `"duplicate": true`, and nothing is written the second time.

## Tests

```bash
# validation contract - no database, no backend, pydantic only
python3 scripts/test_geovision_contract.py

# persistence contract - runs the SQL from service.py against the database.
# Non-destructive: scoped to its own source_id and cleans up after itself.
python3 scripts/test_geovision_sql.py

# end to end over HTTP, including the existing WASTRAQ endpoints
python3 scripts/test_geovision_receiver.py            # needs the backend running
```

## Troubleshooting

* **Everything 422s.** Read `detail.errors` in the response - it names the
  field. A naive timestamp and an `AMBIGUOUS` tap that names a `track_id`
  are the two that look like valid events.
* **Nothing arrives.** `GET /integrations/geovision/status` reads only; it
  makes no outbound call. If `devices` is empty the events never landed -
  check the edge's `GET /integration/status` and that WASTRAQ is bound to
  `0.0.0.0`, not `127.0.0.1`.
* **Tracks look stale.** `active_tracks` is filtered by
  `GEOVISION_TRACK_STALE_S`. An empty list with a recent `last_seen_at` on
  the device means the camera stopped, not that ingestion broke.
