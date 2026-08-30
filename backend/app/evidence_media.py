"""Evidence media resolution and retrieval - the Mac side of STEP 4A.

What this module exists to prevent
----------------------------------
``EVIDENCE_READY`` announces a clip that lives on the Windows GeoVision
machine, and it announces it by ``file_path`` - a Windows path. Before this
module, that string was copied into ``evidence.file_path`` and rendered in
the dashboard's File column. A browser on the Mac cannot open
``C:\\GeoVision\\clips\\x.mp4``; showing it as the evidence claims something
WASTRAQ cannot produce.

So there are two separate things and they are never the same string:

    provenance   geovision_evidence_clips.file_path   where it was recorded
    playback     <evidence root>/<local_path>         bytes this Mac holds

The operator-facing URL is always ``/evidence/{evidence_id}/media`` and it
is always served from the second. If the Mac does not hold the bytes, the
endpoint says so; it never falls back to the remote path, because there is
nothing on the Mac to fall back to.

Store, don't proxy
------------------
The bytes are pulled once and kept (option B), rather than streamed from
the edge on every request (option A). Three reasons, in order:

* An evidence engine whose evidence disappears when a laptop in another
  room is closed has not stored evidence. The whole point of the clip is
  that it outlives the moment.
* ``FileResponse`` already does HTTP Range correctly, so seeking in the
  ``<video>`` element works for free. A hand-written proxy would have to
  re-implement Range, and would re-implement it wrong the first time.
* One 15-second clip is a few megabytes, pulled once. Re-fetching it on
  every scrub of the timeline over site wifi is the expensive option, not
  the cheap one.

The edge's HTTP endpoint is still how the bytes travel - option A is the
transport, option B is the contract.

Nothing here raises into the ingestion path. A clip that cannot be fetched
is a clip marked UNAVAILABLE and retried later; it is never a failed
EVIDENCE_READY ack, because the event was true when it was sent.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .database import execute, fetch_all, fetch_one

log = logging.getLogger("wastraq.evidence_media")

#: Prefix written into ``evidence.file_path`` for a GeoVision clip. Chosen to
#: be obviously NOT a filesystem path and obviously NOT a URL a browser
#: should follow: any code that treats it as either is wrong in a way that
#: shows up immediately rather than in front of an operator.
CLIP_URI_SCHEME = "geovision"

#: Where fetched edge clips land, under the evidence root.
CLIP_SUBDIR = "geovision"

_MEDIA_EXT = {
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
}

_fetch_lock = threading.Lock()
_in_flight: set[str] = set()


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------
def evidence_root() -> Path:
    """The one directory the Mac serves evidence bytes from.

    Everything playable lives under here. Resolved (symlinks included) once
    per call so the containment test below compares real paths against a
    real path - comparing a symlinked root would let a link inside the root
    point anywhere and still "contain".
    """
    return Path(settings.EVIDENCE_MEDIA_ROOT).expanduser().resolve()


def clip_dir() -> Path:
    return evidence_root() / CLIP_SUBDIR


def safe_local_path(candidate: str | None) -> Path | None:
    """Resolve ``candidate`` inside the evidence root, or return None.

    The single choke point for turning a stored string into a file this
    process will open. Accepts a path relative to the root (what we write)
    or an absolute path (what older rows and the local rolling recorder
    hold). Anything that resolves outside the root - ``../``, an absolute
    path elsewhere, a symlink pointing out - returns None rather than a
    path, so a traversal cannot be expressed, only refused.

    Note this is defence in depth, not the primary defence: no endpoint
    accepts a caller-supplied path at all. Callers pass an evidence_id and
    the path comes from the database.
    """
    if not candidate:
        return None
    text = str(candidate).strip()
    if not text or text.startswith(f"{CLIP_URI_SCHEME}://"):
        return None
    # A Windows path is never a local path, whatever else it might look
    # like. Refused explicitly so the reason is visible in a log.
    if len(text) > 2 and text[1] == ":" and text[2] in "\\/":
        return None
    if "\\" in text and "/" not in text:
        return None

    root = evidence_root()
    raw = Path(text).expanduser()
    joined = raw if raw.is_absolute() else (root / raw)
    try:
        resolved = joined.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def media_kind_for(path_or_name: str | None, content_type: str | None = None) -> str:
    if content_type:
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("image/"):
            return "image"
    ext = os.path.splitext(str(path_or_name or ""))[1].lower()
    return _MEDIA_EXT.get(ext, ("none", ""))[0]


def content_type_for(path_or_name: str, declared: str | None = None) -> str:
    """The Content-Type to serve THIS file with.

    ``declared`` is what the edge said the clip was. It is used, but not
    trusted blindly: it arrives over the network from another machine, and
    a bad value here has two costs. A malformed or empty one produces a
    response with no usable type, so the ``<video>`` element has nothing to
    dispatch on; a well-formed but wrong one (``text/html`` on an MP4) is
    worse, because the browser then interprets bytes we are serving from
    our own origin as something they are not.

    So the declared type is honoured only when it is a well-formed
    ``type/subtype`` AND its top-level type agrees with the extension of the
    file we are actually about to open. Otherwise the extension decides,
    which for ``.mp4`` means ``video/mp4``. The file on disk is the thing
    being served, so the file on disk gets the casting vote.

    The Windows source path is never consulted: it names a file on another
    machine, and its extension is not evidence about the bytes here.
    """
    ext = os.path.splitext(path_or_name)[1].lower()
    kind, from_ext = _MEDIA_EXT.get(ext, ("none", "application/octet-stream"))

    candidate = (declared or "").split(";")[0].strip().lower()
    if candidate.count("/") == 1 and all(part for part in candidate.split("/")):
        top = candidate.split("/", 1)[0]
        # An unknown extension has no opinion to contradict the sender with.
        if kind == "none" or top == kind:
            return candidate

    return from_ext


def clip_uri(source_id: str, clip_id: str) -> str:
    """The string written into ``evidence.file_path`` for an edge clip."""
    return (f"{CLIP_URI_SCHEME}://{urllib.parse.quote(source_id, safe='')}"
            f"/{urllib.parse.quote(clip_id, safe='')}")


# ---------------------------------------------------------------------------
# URL derivation
# ---------------------------------------------------------------------------
def derive_clip_url(clip: dict[str, Any]) -> str | None:
    """Where to GET the bytes for this clip, or None if we cannot know.

    An explicit ``file_url`` from the edge always wins - the edge knows how
    it serves itself. Otherwise the URL is built from the configured edge
    base and a template, which is what lets a GeoVision build that does not
    yet send ``file_url`` still work.

    The Windows ``file_path`` is deliberately NOT a fallback. There is no
    transformation from a Windows path to a URL that is right rather than
    lucky, and guessing one produces a 404 that looks like an edge outage.
    """
    explicit = (clip.get("file_url") or "").strip()
    if explicit:
        parsed = urllib.parse.urlparse(explicit)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return explicit
        # A relative URL is meaningful only against the configured edge.
        base = settings.GEOVISION_EDGE_BASE_URL.strip()
        return urllib.parse.urljoin(base + "/", explicit.lstrip("/")) if base else None

    base = settings.GEOVISION_EDGE_BASE_URL.strip()
    clip_id = clip.get("clip_id")
    if not base or not clip_id:
        return None
    template = settings.GEOVISION_CLIP_URL_TEMPLATE
    try:
        suffix = template.format(
            clip_id=urllib.parse.quote(str(clip_id), safe=""),
            source_id=urllib.parse.quote(str(clip.get("source_id") or ""), safe=""),
            event_id=urllib.parse.quote(str(clip.get("event_id") or ""), safe=""),
        )
    except (KeyError, IndexError):
        return None
    return urllib.parse.urljoin(base.rstrip("/") + "/", suffix.lstrip("/"))


# ---------------------------------------------------------------------------
# read side: what can the operator actually play
# ---------------------------------------------------------------------------
#: STEP 4C. The operator is never shown a filesystem path.
#:
#: 4A drew the line at "the Windows path is provenance, not a link" and put
#: it on screen as text. 4C moves the line further: an operator watching a
#: clip has no use for ``C:\GeoVision\clips\CLIP-77.mp4``, cannot act on
#: it, and every place it is rendered is a place the next change might turn
#: it into an href. So the path stays in ``source_ref`` for the audit trail
#: and the API, and the UI is given ``source_label`` - identity, not
#: location - which is the only provenance string the dashboard renders.
_SOURCE_KIND_EDGE = "GEOVISION_EDGE"
_SOURCE_KIND_LOCAL = "LOCAL_CAPTURE"
_SOURCE_KIND_PLACEHOLDER = "PLACEHOLDER"


def source_label(row: dict[str, Any], state: str, *, local: Path | None = None) -> tuple[str, str]:
    """``(label, kind)`` - who produced this artefact, in operator words.

    Deliberately built from identifiers (source id, clip id, file name) and
    never from ``file_path``/``remote_file_path``. A label that is derived
    from a path can become a path the moment a field is empty; this one
    cannot, because a path is never one of its inputs.
    """
    if state != "LOCAL" or row.get("clip_event_id"):
        source_id = str(row.get("clip_source_id") or "").strip()
        clip_id = str(row.get("clip_id") or "").strip()
        parts = [p for p in (source_id, clip_id) if p]
        return ("GeoVision " + " · ".join(parts) if parts else "GeoVision edge",
                _SOURCE_KIND_EDGE)

    if local is not None and local.is_file():
        # The bare file name, never the directory it sits in: it identifies
        # the artefact without describing this machine's layout.
        return f"WASTRAQ capture · {local.name}", _SOURCE_KIND_LOCAL

    return "Demo placeholder — no file was ever recorded", _SOURCE_KIND_PLACEHOLDER


def clip_timing(row: dict[str, Any]) -> dict[str, Any]:
    """Clip start/end/duration, for the metadata the modal shows.

    ``clip_seconds`` is computed here rather than in SQL so a row missing
    either end degrades to ``None`` instead of to a wrong number.
    """
    start, end = row.get("clip_start"), row.get("clip_end")
    seconds = None
    if isinstance(start, datetime) and isinstance(end, datetime):
        delta = (end - start).total_seconds()
        if delta >= 0:
            seconds = round(delta, 1)
    return {
        "clip_start": start,
        "clip_end": end,
        "clip_seconds": seconds,
        "frame_count": row.get("frame_count"),
        "clip_track_id": row.get("clip_track_id"),
    }


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a ``v_evidence_media`` row into the fields the API returns.

    ``media_url`` is populated ONLY when this Mac can serve bytes right now.
    Everything else - a Windows path, an edge URL, a clip announced but not
    pulled - produces a null URL and a status that says which of those it
    is. The UI never has to decide whether a string is playable.
    """
    evidence_id = row.get("evidence_id")
    state = row.get("media_state") or "LOCAL"
    remote = row.get("remote_file_path")

    local: Path | None = None
    if state == "STORED":
        local = safe_local_path(row.get("local_path"))
    elif state == "LOCAL":
        # Not an edge clip: either the local rolling recorder's MP4 (a real
        # absolute path under the evidence root) or one of the demo's
        # placeholder strings, which resolve to nothing and stay unplayable.
        local = safe_local_path(row.get("file_path"))

    if local is not None and local.is_file():
        ctype = content_type_for(local.name, row.get("content_type")
                                 if state == "STORED" else None)
        label, kind = source_label(row, state, local=local)
        return {
            "media_status": "AVAILABLE",
            "media_kind": media_kind_for(local.name, ctype),
            "media_url": f"/evidence/{evidence_id}/media",
            "media_bytes": local.stat().st_size,
            "media_content_type": ctype,
            "source_ref": remote or row.get("file_path"),
            "source_label": label,
            "source_kind": kind,
            "is_placeholder": False,
            "fetch_status": row.get("fetch_status"),
            "fetch_error": row.get("fetch_error"),
            **clip_timing(row),
        }

    if state in ("PENDING", "UNAVAILABLE", "ORPHANED", "STORED"):
        # STORED lands here only when the row says STORED and the file is
        # gone - someone cleaned the evidence directory. Reported as
        # UNAVAILABLE, not AVAILABLE, because the bytes are what matters.
        status = "PENDING" if state in ("PENDING", "ORPHANED") else "UNAVAILABLE"
        label, kind = source_label(row, state)
        return {
            "media_status": status,
            "media_kind": media_kind_for(remote, row.get("content_type")),
            "media_url": None,
            "media_bytes": None,
            "media_content_type": row.get("content_type"),
            "source_ref": remote,
            "source_label": label,
            "source_kind": kind,
            "is_placeholder": False,
            "fetch_status": row.get("fetch_status"),
            "fetch_error": row.get("fetch_error"),
            **clip_timing(row),
        }

    # A placeholder path from the demo seed, or a record with no artefact.
    # STEP 4C names it as such: `is_placeholder` is how the dashboard keeps
    # seed rows out of the evidence count it offers as an action, so
    # "2 evidence" never means "two files that do not exist".
    label, kind = source_label(row, state)
    return {
        "media_status": "NONE",
        "media_kind": "none",
        "media_url": None,
        "media_bytes": None,
        "media_content_type": None,
        "source_ref": row.get("file_path"),
        "source_label": label,
        "source_kind": kind,
        "is_placeholder": kind == _SOURCE_KIND_PLACEHOLDER,
        "fetch_status": None,
        "fetch_error": None,
        **clip_timing(row),
    }


def evidence_media_row(evidence_id: str) -> dict[str, Any] | None:
    return fetch_one(
        "SELECT * FROM v_evidence_media WHERE evidence_id = %s", (evidence_id,)
    )


def evidence_for_event(event_id: str) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT * FROM v_evidence_media WHERE event_id = %s ORDER BY captured_at",
        (event_id,),
    )


def enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**row, **describe(row)} for row in rows]


def playable_file(evidence_id: str) -> tuple[Path | None, dict[str, Any]]:
    """``(path, described)`` - path is non-None only if it is on disk now."""
    row = evidence_media_row(evidence_id)
    if row is None:
        return None, {}
    described = describe(row)
    if described["media_status"] != "AVAILABLE":
        return None, {**row, **described}
    state = row.get("media_state")
    path = safe_local_path(row.get("local_path") if state == "STORED"
                           else row.get("file_path"))
    return path, {**row, **described}


# ---------------------------------------------------------------------------
# write side: pulling the bytes across
# ---------------------------------------------------------------------------
def _mark(clip_event_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    execute(
        f"UPDATE geovision_evidence_clips SET {sets} WHERE event_id = %(event_id)s",
        {**fields, "event_id": clip_event_id},
    )


def fetch_clip(clip_event_id: str, *, force: bool = False) -> dict[str, Any]:
    """Pull one clip's bytes from the edge onto this Mac.

    Returns a small report; never raises. Safe to call twice: a clip already
    STORED with the file present is a no-op, and a second concurrent call
    for the same clip returns IN_FLIGHT rather than writing the same file
    from two threads.
    """
    clip = fetch_one(
        "SELECT * FROM geovision_evidence_clips WHERE event_id = %s", (clip_event_id,)
    )
    if clip is None:
        return {"ok": False, "status": "UNKNOWN_CLIP", "clip_event_id": clip_event_id}

    if not settings.GEOVISION_CLIP_FETCH_ENABLED:
        _mark(clip_event_id, fetch_status="SKIPPED",
              fetch_error="GEOVISION_CLIP_FETCH_ENABLED is off",
              last_fetch_at=datetime.now(timezone.utc))
        return {"ok": False, "status": "SKIPPED", "clip_event_id": clip_event_id}

    existing = safe_local_path(clip.get("local_path"))
    if not force and clip.get("fetch_status") == "STORED" and existing and existing.is_file():
        return {"ok": True, "status": "STORED", "clip_event_id": clip_event_id,
                "local_path": clip.get("local_path"),
                "bytes": existing.stat().st_size, "cached": True}

    url = derive_clip_url(clip)
    if not url:
        _mark(clip_event_id, fetch_status="UNAVAILABLE", fetch_error="NO_CLIP_URL",
              last_fetch_at=datetime.now(timezone.utc))
        return {"ok": False, "status": "NO_CLIP_URL", "clip_event_id": clip_event_id,
                "hint": "Set GEOVISION_EDGE_BASE_URL, or have the edge send file_url."}

    with _fetch_lock:
        if clip_event_id in _in_flight:
            return {"ok": False, "status": "IN_FLIGHT", "clip_event_id": clip_event_id}
        _in_flight.add(clip_event_id)

    try:
        return _do_fetch(clip, url)
    finally:
        with _fetch_lock:
            _in_flight.discard(clip_event_id)


def _do_fetch(clip: dict[str, Any], url: str) -> dict[str, Any]:
    clip_event_id = clip["event_id"]
    now = datetime.now(timezone.utc)
    _mark(clip_event_id, fetch_status="FETCHING", last_fetch_at=now,
          fetch_attempts=int(clip.get("fetch_attempts") or 0) + 1)

    target_dir = clip_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _local_name(clip)
    target = target_dir / name
    limit = int(settings.GEOVISION_CLIP_MAX_BYTES)

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(target_dir), suffix=".part")
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    digest = hashlib.sha256()
    written = 0
    try:
        request = urllib.request.Request(url, method="GET",
                                         headers={"Accept": "*/*"})
        with urllib.request.urlopen(
                request, timeout=settings.GEOVISION_CLIP_FETCH_TIMEOUT_S) as response:
            declared = response.headers.get("Content-Type")
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise ValueError(
                            f"clip exceeds GEOVISION_CLIP_MAX_BYTES ({limit} bytes)")
                    digest.update(chunk)
                    handle.write(chunk)
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        message = f"{type(exc).__name__}: {exc}"
        _mark(clip_event_id, fetch_status="UNAVAILABLE", fetch_error=message[:500],
              last_fetch_at=datetime.now(timezone.utc))
        log.warning("evidence clip fetch failed %s <- %s: %s",
                    clip_event_id, url, message)
        return {"ok": False, "status": "UNAVAILABLE", "clip_event_id": clip_event_id,
                "url": url, "error": message}

    if written == 0:
        tmp.unlink(missing_ok=True)
        _mark(clip_event_id, fetch_status="UNAVAILABLE", fetch_error="EMPTY_RESPONSE",
              last_fetch_at=datetime.now(timezone.utc))
        return {"ok": False, "status": "UNAVAILABLE", "clip_event_id": clip_event_id,
                "url": url, "error": "EMPTY_RESPONSE"}

    got = digest.hexdigest()
    expected = (clip.get("sha256") or "").strip().lower()
    if expected and expected != got:
        # Kept, not discarded: a clip that arrived corrupt is still the only
        # copy on this side, and deleting it would lose the one artefact
        # while a human decides. It simply is not marked STORED, so nothing
        # is ever presented as verified evidence.
        tmp.unlink(missing_ok=True)
        _mark(clip_event_id, fetch_status="UNAVAILABLE",
              fetch_error=f"SHA256_MISMATCH expected={expected} got={got}",
              last_fetch_at=datetime.now(timezone.utc))
        return {"ok": False, "status": "SHA256_MISMATCH",
                "clip_event_id": clip_event_id, "url": url,
                "expected_sha256": expected, "sha256": got}

    shutil.move(str(tmp), str(target))
    relative = f"{CLIP_SUBDIR}/{name}"
    _mark(clip_event_id, fetch_status="STORED", local_path=relative,
          local_bytes=written, sha256=(clip.get("sha256") or got),
          fetch_error=None, fetched=True,
          last_fetch_at=datetime.now(timezone.utc))
    log.info("evidence clip stored %s -> %s (%d bytes)",
             clip_event_id, relative, written)
    return {"ok": True, "status": "STORED", "clip_event_id": clip_event_id,
            "url": url, "local_path": relative, "bytes": written, "sha256": got}


def _local_name(clip: dict[str, Any]) -> str:
    """A filename that is ours, not the edge's.

    Derived from identifiers we control and sanitised to a conservative
    character set. The edge's basename is never reused: it is attacker- (or
    at least stranger-) controlled text arriving over the network, and the
    only safe thing to do with it is not to use it as a filename.
    """
    ext = os.path.splitext(str(clip.get("file_path") or ""))[1].lower()
    if ext not in _MEDIA_EXT:
        ext = ".mp4"
    stem = f"{clip.get('source_id') or 'edge'}_{clip.get('clip_id') or clip['event_id']}"
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in stem)
    return f"{safe[:120]}{ext}"


def fetch_clip_in_background(clip_event_id: str) -> None:
    """Fire-and-forget pull. Used from the ingestion path.

    A thread rather than the request: EVIDENCE_READY is acked on delivery,
    and an edge that is slow to serve its own file must not make the event
    that announced it look like a failure.
    """
    if not settings.GEOVISION_CLIP_FETCH_ON_INGEST:
        return

    def _run() -> None:
        try:
            fetch_clip(clip_event_id)
        except Exception:  # noqa: BLE001
            log.exception("background clip fetch crashed for %s", clip_event_id)

    threading.Thread(target=_run, name=f"clip-fetch-{clip_event_id}",
                     daemon=True).start()


def retry_pending(limit: int = 20) -> list[dict[str, Any]]:
    """Re-attempt every clip that is not on disk yet.

    The answer to "Windows was off when the clip was announced". Nothing is
    lost by that: the announcement is stored, and the bytes are pulled
    whenever the edge is next reachable.
    """
    rows = fetch_all(
        """
        SELECT event_id FROM geovision_evidence_clips
         WHERE fetch_status IN ('PENDING','UNAVAILABLE','FETCHING')
         ORDER BY event_time DESC
         LIMIT %s
        """,
        (limit,),
    )
    return [fetch_clip(row["event_id"]) for row in rows]


def media_status_summary() -> dict[str, Any]:
    rows = fetch_all(
        """
        SELECT fetch_status, count(*) AS clips,
               sum(COALESCE(local_bytes, 0)) AS bytes
          FROM geovision_evidence_clips
         GROUP BY fetch_status
        """
    )
    return {
        "evidence_root": str(evidence_root()),
        "clip_dir": str(clip_dir()),
        "fetch_enabled": settings.GEOVISION_CLIP_FETCH_ENABLED,
        "fetch_on_ingest": settings.GEOVISION_CLIP_FETCH_ON_INGEST,
        "edge_base_url": settings.GEOVISION_EDGE_BASE_URL or None,
        "clip_url_template": settings.GEOVISION_CLIP_URL_TEMPLATE,
        "by_fetch_status": {r["fetch_status"]: int(r["clips"]) for r in rows},
        "stored_bytes": int(sum(int(r["bytes"] or 0) for r in rows)),
    }
