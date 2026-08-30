# Evidence media — the Mac contract (STEP 4A)

How a clip recorded by GeoVision on Windows becomes something an operator
can press play on, in the WASTRAQ dashboard, on the Mac.

Mac-side only. Nothing in this document changes the Windows repository;
what Windows must provide is in **[What Windows must do](#what-windows-must-do)**
at the end, and until it does, everything here degrades to "announced, not
held" rather than breaking.

---

## The problem

`EVIDENCE_READY` announces a clip by `file_path`. That path is on the
Windows machine:

    C:\GeoVision\evidence\clip_20260830_071204.mp4

Before STEP 4A that string was copied verbatim into `evidence.file_path`
and printed in the dashboard's File column. It is not a file on this Mac.
No browser on this network can open it. A UI that shows it as "the
evidence" is claiming something the system cannot produce — and an
evidence engine that cannot produce its evidence is the one failure mode
worth spending real design on.

## The rule

Two different things, never the same string:

| | what it is | where it lives | who may see it |
|---|---|---|---|
| **provenance** | the Windows path | `geovision_evidence_clips.file_path` | shown as `source_ref`, never as a link |
| **playback** | bytes this Mac holds | `<evidence root>/geovision/<name>` | served by `GET /evidence/{id}/media` |

`evidence.file_path` for a GeoVision clip is neither. It is an opaque
identifier:

    geovision://<source_id>/<clip_id>

Chosen because it is obviously not a filesystem path and obviously not a
URL a browser should follow. Any code that mistakes it for either fails
immediately and visibly instead of in front of an operator.

## Store, don't proxy

The bytes are pulled once and kept, rather than streamed from the edge on
every request.

* An evidence engine whose evidence disappears when a laptop in another
  room is closed has not stored evidence.
* `FileResponse` already implements HTTP Range correctly, so seeking in
  the `<video>` element works for free. A hand-written proxy would have to
  re-implement Range, and would get it wrong the first time.
* One clip is a few megabytes, fetched once. Re-fetching it on every
  timeline scrub over site wifi is the expensive option, not the cheap one.

The edge's HTTP endpoint is still how the bytes travel. **HTTP is the
transport; local storage is the contract.**

---

## The flow

```
GeoVision (Windows)                    WASTRAQ (Mac)
─────────────────────                  ──────────────────────────────────────
records clip
POST EVIDENCE_READY  ───────────────▶  202 ACCEPTED   (always, immediately)
  clip_id, file_path,                    │
  file_url, sha256                       ├─ geovision_raw_events        (dedup by event_id)
                                         ├─ geovision_evidence_clips    (dedup by source_id+clip_id)
                                         ├─ episode engine: which episode? which event?
                                         │    └─ evidence row, clip_event_id UNIQUE
                                         └─ background thread ──┐
                                                                │
GET /evidence/clips/{id}/file  ◀────────────────────────────────┘
  200 video/mp4  ─────────────────────▶  <root>/geovision/<source>_<clip>.mp4
                                         fetch_status = STORED

                                       operator opens the evidence modal
                                       GET /collection-events/{id}/evidence
                                         → media_status: AVAILABLE
                                           media_url: /evidence/EVID-042/media
                                       <video src="/evidence/EVID-042/media">
                                       GET /evidence/EVID-042/media → 200, Range OK
```

Every arrow into WASTRAQ is one-way and non-blocking. The `202` is earned
by storing the announcement; it never waits on the file transfer.

---

## The event

`EVIDENCE_READY` gains four optional fields. All four are optional so that
a GeoVision build predating them keeps working unchanged.

| field | required | meaning |
|---|---|---|
| `clip_id` | yes | the edge's identifier for the clip. With `source_id`, this is what makes a clip one clip |
| `file_path` | yes | where it sits on Windows. **Provenance. Never served, never linked, never turned into a URL** |
| `file_url` | no | `http(s)://…` or a `/path` relative to `GEOVISION_EDGE_BASE_URL`. **This is what makes the clip retrievable** |
| `content_type` | no | defaults to `video/mp4` |
| `size_bytes` | no | advisory |
| `sha256` | no | 64 hex chars. When present, a fetched copy is **not** marked `STORED` unless it matches |

`file_url` is refused unless it is `http://`, `https://` or begins with `/`.
A `file://` or UNC "URL" is a Windows path wearing a hat, and the whole
point of the field is not to be that.

When `file_url` is absent, WASTRAQ derives one:

    GEOVISION_EDGE_BASE_URL + GEOVISION_CLIP_URL_TEMPLATE
    e.g. http://192.168.0.126:8000/evidence/clips/CLIP-77/file

The Windows `file_path` is **never** a fallback for this. There is no
transformation from a Windows path to a URL that is right rather than
lucky, and guessing one produces a 404 that looks like an edge outage.

Everything the sixth-event contract already forbids still applies: no
`property_id`, no `segregation_status`, no property association of any
kind. GeoVision says *a clip exists*; WASTRAQ decides *whose house it is*.

---

## Idempotency

Three independent layers, because a retry is normal traffic and each layer
catches a different mistake:

1. **`geovision_raw_events.event_id` (PK)** — a redelivered packet. The
   receiver returns `200 DUPLICATE` and writes nothing.
2. **`geovision_evidence_clips (source_id, clip_id)` (UNIQUE)** — the same
   clip re-announced under a *fresh* envelope. The first row is kept; the
   announcement is not applied twice.
3. **`evidence.clip_event_id` (partial UNIQUE index)** — one clip produces
   at most one evidence row, enforced in the database rather than by a
   read-then-write check that two concurrent retries can both pass.

Layer 3 is the one that was missing, and layer 2 is why: on a
re-announcement the envelope's own `event_id` has no clip row at all, so
code that used it tagged nothing and then inserted a *second* evidence
record. The engine now resolves the clip by `(source_id, clip_id)` —
identity, not delivery — before doing anything with it.

---

## Availability, honestly

`media_url` is populated **if and only if** the bytes are on this Mac right
now. The front end never has to decide whether a string is playable.

| `media_status` | meaning | `media_url` |
|---|---|---|
| `AVAILABLE` | the file is here; play it | `/evidence/{id}/media` |
| `PENDING` | announced by the edge, not pulled yet | `null` |
| `UNAVAILABLE` | the edge was asked and could not deliver | `null` |
| `NONE` | this record has no playable artefact (a demo placeholder) | `null` |

`STORED` in the database plus a missing file on disk reports `UNAVAILABLE`,
not `AVAILABLE`. The bytes are what matters, not the row.

`GET /evidence/{id}/media` for a clip that is not held answers **409** with
the reason and the `source_ref` — never 200 with a Windows path. "We do not
have it yet, and here is where it is" is a true and useful answer.

---

## Endpoints

| method | path | purpose |
|---|---|---|
| `GET` | `/collection-events/{event_id}/evidence` | rows for one event, with media state |
| `GET` | `/evidence` | recent rows, with media state |
| `GET` | `/evidence/{evidence_id}` | one row |
| `GET` | `/evidence/{evidence_id}/media` | **the bytes** — `<video src>` points here |
| `POST` | `/evidence/{evidence_id}/fetch` | pull this one clip from the edge now |
| `GET` | `/evidence-media/status` | what is held, what is not, where the root is |
| `POST` | `/evidence-media/retry` | re-attempt every announced-but-unheld clip |

`/evidence-media/status` makes no request to the edge. A status endpoint
that blocks on an unreachable Windows machine is useless during exactly the
outage it is meant to describe.

`POST /evidence-media/retry` is the answer to *"Windows was off when the
clip was recorded"*. Nothing was lost: the announcement is stored, and the
bytes come across the next time anyone asks.

## Security

* **No endpoint accepts a path.** Every media request names an
  `evidence_id`; the path comes from the database.
* Everything that resolves to a file goes through
  `evidence_media.safe_local_path()`, which refuses anything landing
  outside the evidence root — `..`, an absolute path elsewhere, a Windows
  path, a UNC path, a symlink pointing out.
* Stored filenames are **ours**, built from sanitised identifiers. The
  edge's basename is attacker-controlled text arriving over the network,
  and the only safe thing to do with it is not use it as a filename.
* `GEOVISION_CLIP_MAX_BYTES` caps what one announced clip may write to this
  disk. Downloads are streamed to a `.part` file and renamed only on
  success, so a truncated transfer never becomes a half-file that looks
  playable.
* A declared `sha256` that does not match is a refusal, not a warning.

---

## Configuration

See `backend/.env.example`. In short:

    GEOVISION_EDGE_BASE_URL=http://<windows-ip>:8000
    GEOVISION_CLIP_URL_TEMPLATE=/evidence/clips/{clip_id}/file
    GEOVISION_CLIP_FETCH_ENABLED=1
    GEOVISION_CLIP_FETCH_ON_INGEST=1
    GEOVISION_CLIP_FETCH_TIMEOUT_S=20.0
    GEOVISION_CLIP_MAX_BYTES=200000000
    # EVIDENCE_MEDIA_ROOT=<repo>/evidence

Migration (idempotent, safe to re-run):

    psql -d wastraq_demo -f database/evidence_media.sql

## Tests

    python3 scripts/test_step4a_evidence_media.py     # no DB, no network
    python3 scripts/test_step4a_evidence_http.py      # backend running

The second one stands up a fake GeoVision edge on localhost serving
`scripts/fixtures/sample_clip.mp4` from the same URL shape the real edge
will use, so the whole chain can be proved without a Windows machine in the
room.

---

## What Windows must do

One endpoint. Nothing else about the edge changes.

**1. Serve the clip over HTTP.**

    GET /evidence/clips/{clip_id}/file  ->  200, Content-Type: video/mp4

* Look the clip up by `clip_id` in the edge's own registry. **Do not accept
  a path parameter** — a filesystem path in a URL is a traversal hole, and
  WASTRAQ never sends one.
* `Content-Length` set. HTTP Range support is welcome but not required:
  WASTRAQ downloads the whole file once, and serves Range itself afterwards.
* `404` for an unknown `clip_id`, `409` while the clip is still being
  written. Both are handled — WASTRAQ marks the clip `UNAVAILABLE` and
  retries later.
* Bind on the LAN interface, not `127.0.0.1`.

**2. Include `file_url` in `EVIDENCE_READY`** (recommended, optional):

```json
{
  "event_type": "EVIDENCE_READY",
  "event_id": "…", "source_id": "…", "timestamp": "2026-08-30T07:12:04.120Z",
  "clip_id": "clip_20260830_071204",
  "file_path": "C:\\GeoVision\\evidence\\clip_20260830_071204.mp4",
  "file_url": "/evidence/clips/clip_20260830_071204/file",
  "content_type": "video/mp4",
  "size_bytes": 3214880,
  "sha256": "…64 hex…",
  "rfid_event_id": "…"
}
```

`rfid_event_id` matters more than any of the new fields: it is the
strongest key WASTRAQ has for attributing a clip to the right episode, and
therefore to the right house.

**3. Publish `EVIDENCE_READY` only after the file is closed and complete.**
Announcing a clip still being written produces a fetch that succeeds and
stores a truncated file — which is worse than a fetch that fails, because
it looks like evidence.

Still forbidden, unchanged: `property_id`, `segregation_status`, or any
other field asserting which property was served. The envelope validator
refuses the whole event if one appears.

---

# STEP 4C — click to video

STEP 4A made the bytes reachable. 4C is the last hop: from a row in the
Live Operations feed to the footage playing on screen, in one click, with
nothing invented along the way.

## The path, exactly

```
Live Operations feed
  │  the event's evidence chip is a <button class="chip … ev-open"
  │  carrying data-ev="EVENT-006"
  ▼
openEvidence('EVENT-006')            existing modal #evm — not a new one
  │
  ├─ GET /collection-events/EVENT-006
  │     → property, house number, owner, picker_name, collection_time,
  │       track, confidence
  │     → evidence[]: each row already carries media_status, media_url,
  │       source_label, is_placeholder, clip_start/end/seconds/frames
  │
  ├─ GET /properties/{property_id}        (reference frontage photo only;
  │                                        failure here does not block the clip)
  ▼
<video controls preload="metadata" src="/evidence/EVID-042/media">
  │
  ▼
GET /evidence/EVID-042/media
      → FileResponse from <evidence root>/geovision/<clip>.mp4
      → 200 video/mp4, Accept-Ranges: bytes; the browser's seek issues
        Range and gets 206 back for free
```

The `<video>` `src` is always a WASTRAQ path. The edge is never contacted
by the browser, and the Windows machine's URL never reaches it.

## Three rules the UI enforces

**1. A player appears only when the bytes are here.** `media_url` is
non-null iff this Mac holds the file — decided in `evidence_media.describe`,
not in the browser. Everything else renders as a state with a reason and a
`Retrieve clip from GeoVision` button.

**2. No filesystem path is ever shown.** 4A rendered the Windows path as
text under the player. 4C stopped: an operator cannot act on
`C:\GeoVision\clips\CLIP-77.mp4`, and every place it is rendered is a place
the next change could turn it into a link. The path stays in `source_ref`
for the audit trail; the UI renders `source_label` — built from identifiers
only (`GeoVision GEOVISION-D455-01 · CLIP-3f2a1b0c9d8e`), so it cannot
degrade into a path when a field is empty. The 409 body was changed for the
same reason: it is rendered verbatim when a clip fails to load.

**3. A demo seed row is not evidence.** `is_placeholder` marks a row whose
`file_path` was never a file. Those rows are summarised in one line
("1 placeholder record … nothing was ever recorded for it") and are excluded
from the evidence list and from the feed's playable count, so an evidence
chip never promises footage that does not exist.

## The count on the chip

`GET /collection-events/feed/detailed` returns four numbers per event, from
one `LATERAL` over `v_evidence_media`:

| field | meaning | chip |
|---|---|---|
| `playable_evidence_count` | clips whose bytes are on this Mac | `▶ 1 clip`, good |
| `clip_evidence_count` | clips announced by GeoVision | `1 clip pending`, warn |
| `evidence_count` | every row | `2 evidence`, neutral |
| `local_evidence_count` | rows that are not edge clips | — |

The tone is a promise the modal can keep. `playable` is the database's
belief; the modal re-checks the file on disk before rendering a player, so
a clip deleted underneath us downgrades to `UNAVAILABLE` there rather than
being offered twice.

## Loading and error states

Attached, never inlined — an inline `onerror` on an element whose `src`
came from the API is one escaping mistake from being an injection point.

| state | what the operator sees |
|---|---|
| loading | "Loading clip…", cleared on `loadeddata` / `canplay` / `playing` |
| `MediaError` 2 | "The connection to this Mac dropped while the clip was loading." |
| `MediaError` 3 | "The clip is on this Mac but could not be decoded — the file may be truncated." |
| `MediaError` 4 | "This Mac could not serve the clip. It may have been removed from the evidence store." |
| any failure | a `Re-fetch from GeoVision` button (`POST /evidence/{id}/fetch?force=true`), then the modal re-renders |

An element that already failed before the listener attached is caught too,
so the spinner cannot outlive the clip.

## Auto-refresh

Untouched. `WQ.autoRefresh(refresh, 10)` still drives the feed, map, KPIs
and property table; the modal is separate DOM and is never re-rendered by a
poll, so a clip keeps playing while the feed updates behind it. The
evidence action calls `stopPropagation()` because it sits inside the feed
row, whose own click opens the property drawer — without it the drawer
would slide over the clip.
