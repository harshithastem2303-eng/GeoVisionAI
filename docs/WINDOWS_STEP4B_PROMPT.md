# Windows Claude prompt — STEP 4B

Copy everything below the line into a Claude session opened on the **GeoVision
Windows repository**. Nothing above the line is part of the prompt.

---

Work only on **STEP 4B: serve evidence clips to WASTRAQ over HTTP**.

Repo: the GeoVision edge repository on this Windows machine
(`geovision-darshan` or whatever this checkout is called — do not assume, look).

## Context you can rely on

The WASTRAQ Mac side of this is already built, tested and merged (STEP 4A). Do
not design around a Mac that reads Windows paths — it does not, and will not.

* WASTRAQ receives `EVIDENCE_READY` and stores the announcement immediately.
  `file_path` (the Windows path) is kept as **provenance only**. It is never
  served to a browser, never linked, and never converted into a URL.
* WASTRAQ then **pulls the clip's bytes over HTTP** onto the Mac, keeps them,
  and serves them itself from `GET /evidence/{evidence_id}/media`. The Mac is
  the only operator-facing UI and the only thing that serves evidence bytes to
  an operator.
* Until the bytes arrive, the dashboard shows the clip as `PENDING` with the
  Windows path as text and a "Retrieve clip from GeoVision" button. Nothing
  breaks; the evidence is simply not held yet.
* WASTRAQ's fetch is `GET` with `Accept: */*`, a ~20 s timeout, streaming, and
  a 200 MB ceiling. It sends no path, no credentials, and no custom headers.

So the only thing missing is on your side: **GeoVision does not currently
expose the clip file over HTTP.**

## What to build

### 1. `GET /evidence/clips/{clip_id}/file`

Add this to the edge's existing FastAPI/HTTP app (do not stand up a second
server, and do not add a new framework).

* Look `clip_id` up in the edge's own clip registry/metadata and resolve it to
  a path **server-side**.
* **Do not accept a path, a filename, or any part of one as a parameter.** A
  filesystem path in a URL is a traversal hole. WASTRAQ never sends one, so
  there is no compatibility reason to accept one.
* Before opening the resolved path, verify it is inside the configured evidence
  directory (resolve it and compare against the resolved root). Refuse anything
  else. Defence in depth — the registry should already guarantee this.
* Respond `200` with `Content-Type: video/mp4` and a correct `Content-Length`.
  HTTP Range support is welcome but **not required**: WASTRAQ downloads the
  whole file once and serves Range itself afterwards.
* `404` for an unknown `clip_id`. `409` while the clip is still being written.
  Both are handled cleanly — WASTRAQ marks the clip `UNAVAILABLE` and retries.
* Bind on the LAN interface, not `127.0.0.1`, and make sure Windows Firewall
  allows inbound on that port. If the app already binds `0.0.0.0`, say so and
  change nothing.
* Add a matching `GET /evidence/clips/{clip_id}` metadata route only if the
  edge does not already have one. WASTRAQ does not need it; a human debugging
  on demo morning does.

### 2. Extend `EVIDENCE_READY` (all fields optional, backward compatible)

In the event builder (`backend/integration/events.py` or wherever
`EVIDENCE_READY` is constructed), add:

| field | value |
|---|---|
| `file_url` | `"/evidence/clips/{clip_id}/file"` — a leading-slash relative URL is preferred over an absolute one, so it keeps working when the laptop's IP changes |
| `content_type` | `"video/mp4"` |
| `size_bytes` | the finished file's size |
| `sha256` | hex sha256 of the finished file, lowercase, 64 chars — **omit it entirely rather than sending a wrong or partial one** |

WASTRAQ's validator refuses `file_url` unless it is `http://`, `https://` or
starts with `/`. A `file://` or UNC path will be rejected — that is deliberate,
it is a Windows path wearing a hat.

If `file_url` is omitted altogether, WASTRAQ derives it from
`GEOVISION_EDGE_BASE_URL + /evidence/clips/{clip_id}/file`, so step 1 alone is
enough to make this work. Step 2 just removes a configuration coupling.

### 3. Publish `EVIDENCE_READY` only after the file is closed and complete

Check this in the existing code and fix it if it is not already true. Announcing
a clip that is still being written produces a fetch that **succeeds** and stores
a truncated file — which is worse than a fetch that fails, because it looks like
evidence. If the writer cannot easily signal completion, write to a `.part` name
and rename on close, and announce after the rename.

### 4. Also send `rfid_event_id`

Verify `EVIDENCE_READY` carries `rfid_event_id` — the id of the RFID tap that
caused the clip. This matters more than any of the new fields: it is the
strongest key WASTRAQ has for attributing a clip to the right episode, and
therefore to the right house. Without it, attribution falls back to
(source, session, track, time-window) matching, which is correct but weaker.

## Constraints — do not violate these

* **Never add `property_id`, `property_name`, `segregation_status`,
  `service_zone_id`, `collection_event_id` or any other property-asserting
  field to any event.** WASTRAQ's envelope validator refuses the entire event
  if one appears, and that guard is the point of the architecture: GeoVision
  cannot see a service zone, so it has no basis for naming a house.
* Do not modify anything in the WASTRAQ Mac repository. It is not on this
  machine.
* Do not add authentication, TLS, Kafka, a queue, cloud storage, or a second
  service. This is a demo on one LAN. One `GET` route.
* Do not change the six event types, the envelope, or the `RFID_TAP`
  binding-status rules.
* Do not delete or move existing clip files. The edge keeps its own copy;
  WASTRAQ keeping a second one is the design, not a duplication to clean up.

## How to verify, without the Mac

1. `curl -v http://localhost:<port>/evidence/clips/<a real clip_id>/file -o out.mp4`
   → `200`, `Content-Type: video/mp4`, `out.mp4` byte-identical to the file on
   disk (compare sha256).
2. Same request from another machine on the LAN using the Windows LAN IP →
   `200`. If it hangs, it is the firewall, not the code.
3. `curl -i .../evidence/clips/definitely-not-a-clip/file` → `404`.
4. Attempt traversal: `curl -i ".../evidence/clips/..%2F..%2Fwindows%2Fsystem32%2Fdrivers%2Fetc%2Fhosts/file"`
   → `404`, and nothing outside the evidence directory is ever opened.
5. Print one real `EVIDENCE_READY` payload and check it contains `clip_id`,
   `file_path`, `file_url`, `content_type`, `size_bytes`, `sha256`,
   `rfid_event_id` — and **none** of the forbidden property fields.

## Report back

1. Files changed.
2. The exact route path, method, and response headers.
3. The exact `EVIDENCE_READY` JSON now published (one real example).
4. The host/port the edge binds, and whether the firewall rule exists.
5. Whether `sha256` is sent, and over what bytes.
6. Test commands and their actual output.

Do not commit or push. Stop after STEP 4B.
