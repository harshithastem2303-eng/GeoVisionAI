#!/usr/bin/env python3
"""STEP 4A unit tests: backend/app/evidence_media.py, no database, no network.

    python3 scripts/test_step4a_evidence_media.py

Runs anywhere Python 3.10+ runs - no psycopg, no FastAPI, no PostgreSQL, no
GeoVision. ``evidence_media.py`` is copied into a throwaway package next to
STUB ``config`` and ``database`` modules, so the file under test is the real
one while its two dependencies are fakes. The "edge" is a plain
``http.server`` on localhost serving ``scripts/fixtures/sample_clip.mp4``.

What it is actually checking, in one line each:

* a Windows path, an absolute path, ``..`` and a symlink out of the
  evidence root can none of them name a file this process will open;
* ``media_url`` is populated if and only if the bytes are on this machine;
* a clip fetched from the edge arrives byte-identical and lands inside the
  evidence root under a name WE chose, whatever the edge called it;
* an unreachable edge, a truncated file and a hash mismatch each produce a
  recorded failure and never an exception.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import textwrap
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "backend" / "app" / "evidence_media.py"
FIXTURE = REPO / "scripts" / "fixtures" / "sample_clip.mp4"

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


# ---------------------------------------------------------------------------
# harness: real module, stub dependencies
# ---------------------------------------------------------------------------
STUB_CONFIG = '''
class _S:
    EVIDENCE_MEDIA_ROOT = ""
    GEOVISION_EDGE_BASE_URL = ""
    GEOVISION_CLIP_URL_TEMPLATE = "/evidence/clips/{clip_id}/file"
    GEOVISION_CLIP_FETCH_ENABLED = True
    GEOVISION_CLIP_FETCH_ON_INGEST = True
    GEOVISION_CLIP_FETCH_TIMEOUT_S = 5.0
    GEOVISION_CLIP_MAX_BYTES = 200_000_000
settings = _S()
'''

STUB_DB = '''
"""In-memory stand-in for app.database. The SQL itself is covered by the
psql-level tests; what matters here is that the module reads and writes the
columns it claims to."""
CLIPS = {}
EVIDENCE = {}

def _clip_row(ev):
    clip = CLIPS.get(ev.get("clip_event_id"))
    if ev.get("clip_event_id") is None:
        state = "LOCAL"
    elif clip is None:
        state = "ORPHANED"
    elif clip.get("fetch_status") == "STORED":
        state = "STORED"
    elif clip.get("fetch_status") == "UNAVAILABLE":
        state = "UNAVAILABLE"
    else:
        state = "PENDING"
    out = dict(ev)
    out["media_state"] = state
    if clip:
        out.update({
            "clip_source_id": clip.get("source_id"),
            "clip_id": clip.get("clip_id"),
            "remote_file_path": clip.get("file_path"),
            "remote_file_url": clip.get("file_url"),
            "content_type": clip.get("content_type"),
            "local_path": clip.get("local_path"),
            "local_bytes": clip.get("local_bytes"),
            "fetch_status": clip.get("fetch_status"),
            "fetch_attempts": clip.get("fetch_attempts"),
            "fetch_error": clip.get("fetch_error"),
            "last_fetch_at": clip.get("last_fetch_at"),
            # STEP 4C appended these to v_evidence_media; the stub mirrors
            # the view, so it grows the same columns.
            "clip_start": clip.get("clip_start"),
            "clip_end": clip.get("clip_end"),
            "frame_count": clip.get("frame_count"),
            "clip_track_id": clip.get("track_id"),
            "clip_event_time": clip.get("event_time"),
        })
    return out

def fetch_one(sql, params=None):
    if "geovision_evidence_clips" in sql and "SELECT *" in sql:
        row = CLIPS.get(params[0])
        return dict(row) if row else None
    if "v_evidence_media" in sql:
        ev = EVIDENCE.get(params[0])
        return _clip_row(ev) if ev else None
    return None

def fetch_all(sql, params=None):
    if "v_evidence_media" in sql:
        return [_clip_row(e) for e in EVIDENCE.values() if e["event_id"] == params[0]]
    if "fetch_status IN" in sql:
        return [{"event_id": k} for k, v in CLIPS.items()
                if v.get("fetch_status") in ("PENDING", "UNAVAILABLE", "FETCHING")]
    if "GROUP BY fetch_status" in sql:
        agg = {}
        for v in CLIPS.values():
            s = v.get("fetch_status")
            a = agg.setdefault(s, {"fetch_status": s, "clips": 0, "bytes": 0})
            a["clips"] += 1
            a["bytes"] += int(v.get("local_bytes") or 0)
        return list(agg.values())
    return []

def execute(sql, params=None):
    if sql.strip().startswith("UPDATE geovision_evidence_clips SET"):
        row = CLIPS.get(params["event_id"])
        if row is not None:
            for k, v in params.items():
                if k != "event_id":
                    row[k] = v
    return None
'''


def build_module(root: Path):
    """Copy the real evidence_media.py into a package with stub deps."""
    pkg = root / "wqtest"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "config.py").write_text(textwrap.dedent(STUB_CONFIG))
    (pkg / "database.py").write_text(textwrap.dedent(STUB_DB))
    shutil.copy2(SOURCE, pkg / "evidence_media.py")
    sys.path.insert(0, str(root))
    import wqtest.evidence_media as em  # noqa: E402
    import wqtest.database as db        # noqa: E402
    import wqtest.config as cfg         # noqa: E402
    return em, db, cfg


# ---------------------------------------------------------------------------
# a fake GeoVision edge
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/evidence/clips/") and self.path.endswith("/file"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence
        pass


def start_edge(body: bytes):
    handler = type("H", (_Handler,), {"body": body})
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------------------
def main() -> int:
    if not FIXTURE.is_file():
        print(f"!! fixture missing: {FIXTURE}")
        return 2
    clip_bytes = FIXTURE.read_bytes()
    clip_sha = hashlib.sha256(clip_bytes).hexdigest()

    tmp = Path(tempfile.mkdtemp(prefix="wq4a-"))
    em, db, cfg = build_module(tmp / "pkg")
    root = tmp / "evidence"
    root.mkdir()
    cfg.settings.EVIDENCE_MEDIA_ROOT = str(root)
    em.settings = cfg.settings

    now = datetime.now(timezone.utc)

    # -- 1. path safety ----------------------------------------------------
    print("\n1. safe_local_path refuses everything that is not inside the root")
    (root / "ok.mp4").write_bytes(clip_bytes)
    check("relative path inside the root resolves",
          em.safe_local_path("ok.mp4") == (root / "ok.mp4").resolve())
    check("absolute path inside the root resolves",
          em.safe_local_path(str(root / "ok.mp4")) == (root / "ok.mp4").resolve())
    for bad in ["../../../etc/passwd", "geovision/../../../etc/passwd",
                "/etc/passwd", "/etc/../etc/passwd",
                r"C:\GeoVision\clips\CLIP-77.mp4",
                r"\\WINBOX\share\clip.mp4",
                "geovision://geovision-lane1/CLIP-77", "", None]:
        check(f"refused: {bad!r}", em.safe_local_path(bad) is None)

    outside = tmp / "outside.mp4"
    outside.write_bytes(b"not evidence")
    try:
        os.symlink(outside, root / "escape.mp4")
        check("symlink pointing out of the root is refused",
              em.safe_local_path("escape.mp4") is None)
    except (OSError, NotImplementedError):
        print("  [skip] symlink test - not permitted here")

    # -- 2. URL derivation -------------------------------------------------
    print("\n2. derive_clip_url")
    cfg.settings.GEOVISION_EDGE_BASE_URL = "http://192.168.0.126:8000"
    check("explicit absolute file_url wins",
          em.derive_clip_url({"file_url": "http://10.0.0.5:9000/x.mp4",
                              "clip_id": "C1"}) == "http://10.0.0.5:9000/x.mp4")
    check("relative file_url joins the edge base",
          em.derive_clip_url({"file_url": "/clips/C1.mp4", "clip_id": "C1"})
          == "http://192.168.0.126:8000/clips/C1.mp4")
    check("template fills in when the edge sent no url",
          em.derive_clip_url({"clip_id": "CLIP-77", "source_id": "lane1"})
          == "http://192.168.0.126:8000/evidence/clips/CLIP-77/file")
    check("clip_id is percent-encoded into the url",
          em.derive_clip_url({"clip_id": "a/b c", "source_id": "lane1"})
          == "http://192.168.0.126:8000/evidence/clips/a%2Fb%20c/file")
    cfg.settings.GEOVISION_EDGE_BASE_URL = ""
    check("no edge configured -> no url invented",
          em.derive_clip_url({"clip_id": "CLIP-77"}) is None)
    check("a Windows path is NEVER turned into a url",
          em.derive_clip_url({"file_path": r"C:\GeoVision\clips\x.mp4"}) is None)
    cfg.settings.GEOVISION_EDGE_BASE_URL = "http://192.168.0.126:8000"

    # -- 2b. Content-Type ---------------------------------------------------
    print("\n2b. content_type_for: the file on disk gets the casting vote")
    check("an .mp4 with no declared type is video/mp4",
          em.content_type_for("clip.mp4") == "video/mp4")
    check("a declared type that agrees with the extension is honoured",
          em.content_type_for("clip.mov", "video/quicktime") == "video/quicktime")
    check("parameters are stripped",
          em.content_type_for("clip.mp4", "video/mp4; charset=binary") == "video/mp4")
    check("casing is normalised",
          em.content_type_for("clip.mp4", "Video/MP4") == "video/mp4")
    for junk in ["", "   ", "video", "video/", "/mp4", "not a type", None,
                 "application/octet-stream", "text/html",
                 "text/html; charset=utf-8"]:
        check(f"an .mp4 falls back to video/mp4 despite declared {junk!r}",
              em.content_type_for("clip.mp4", junk) == "video/mp4")
    check("an .jpg with a video type declared falls back to image/jpeg",
          em.content_type_for("frame.jpg", "video/mp4") == "image/jpeg")
    check("an unknown extension still trusts a well-formed declared type",
          em.content_type_for("thing.bin", "application/pdf") == "application/pdf")
    check("an unknown extension with no declared type is a byte stream",
          em.content_type_for("thing.bin") == "application/octet-stream")
    check("the Windows source path is never consulted",
          em.content_type_for("clip.mp4", None) == "video/mp4"
          and em.content_type_for(r"C:\GeoVision\clips\CLIP-77.mp4") == "video/mp4")

    # -- 3. describe() -----------------------------------------------------
    print("\n3. describe(): media_url only when the bytes are here")
    db.EVIDENCE["EVID-100"] = {"evidence_id": "EVID-100", "event_id": "EVENT-1",
                               "evidence_type": "CAMERA_FRAME",
                               "file_path": "/evidence/2026/08/30/placeholder.jpg",
                               "captured_at": now, "verified": False,
                               "clip_event_id": None}
    d = em.describe(db.fetch_one("v_evidence_media", ("EVID-100",)))
    check("demo placeholder path -> NONE, no url",
          d["media_status"] == "NONE" and d["media_url"] is None, str(d))

    db.EVIDENCE["EVID-101"] = {"evidence_id": "EVID-101", "event_id": "EVENT-1",
                               "evidence_type": "VIDEO_CLIP",
                               "file_path": str(root / "ok.mp4"),
                               "captured_at": now, "verified": False,
                               "clip_event_id": None}
    d = em.describe(db.fetch_one("v_evidence_media", ("EVID-101",)))
    check("local recorder clip -> AVAILABLE video, served by WASTRAQ",
          d["media_status"] == "AVAILABLE" and d["media_kind"] == "video"
          and d["media_url"] == "/evidence/EVID-101/media", str(d))

    db.CLIPS["gv-1"] = {"event_id": "gv-1", "source_id": "geovision-lane1",
                        "clip_id": "CLIP-77", "content_type": "video/mp4",
                        "file_path": r"C:\GeoVision\clips\CLIP-77.mp4",
                        "file_url": None, "fetch_status": "PENDING",
                        "fetch_attempts": 0, "local_path": None, "sha256": None}
    db.EVIDENCE["EVID-102"] = {"evidence_id": "EVID-102", "event_id": "EVENT-1",
                               "evidence_type": "VIDEO_CLIP",
                               "file_path": "geovision://geovision-lane1/CLIP-77",
                               "captured_at": now, "verified": False,
                               "clip_event_id": "gv-1"}
    d = em.describe(db.fetch_one("v_evidence_media", ("EVID-102",)))
    check("announced but not held -> PENDING, url is null",
          d["media_status"] == "PENDING" and d["media_url"] is None, str(d))
    check("the Windows path is reported as provenance, not as a location",
          d["source_ref"] == r"C:\GeoVision\clips\CLIP-77.mp4", str(d))

    # -- 4. fetching from the edge ----------------------------------------
    print("\n4. fetch_clip against a live fake edge")
    server, base = start_edge(clip_bytes)
    cfg.settings.GEOVISION_EDGE_BASE_URL = base
    try:
        result = em.fetch_clip("gv-1")
        check("fetch reports STORED", result.get("status") == "STORED", str(result))
        local = em.safe_local_path(db.CLIPS["gv-1"]["local_path"])
        check("stored path resolves inside the evidence root", local is not None)
        check("stored bytes are byte-identical to the edge's file",
              local is not None and local.read_bytes() == clip_bytes)
        check("sha256 recorded", db.CLIPS["gv-1"]["sha256"] == clip_sha)
        check("legacy `fetched` flag kept in sync", db.CLIPS["gv-1"]["fetched"] is True)
        check("local_path is relative, not absolute",
              not os.path.isabs(db.CLIPS["gv-1"]["local_path"]),
              db.CLIPS["gv-1"]["local_path"])

        d = em.describe(db.fetch_one("v_evidence_media", ("EVID-102",)))
        check("evidence now AVAILABLE with a WASTRAQ url",
              d["media_status"] == "AVAILABLE"
              and d["media_url"] == "/evidence/EVID-102/media"
              and d["media_kind"] == "video", str(d))
        check("media_bytes matches the file", d["media_bytes"] == len(clip_bytes))
        # What GET /evidence/{id}/media hands to FileResponse as media_type.
        check("the served Content-Type is video/mp4",
              em.content_type_for(
                  em.safe_local_path(db.CLIPS["gv-1"]["local_path"]).name,
                  d["media_content_type"]) == "video/mp4", str(d))

        # A clip the edge mislabels must still be served as what it is.
        db.CLIPS["gv-1"]["content_type"] = "application/octet-stream"
        d = em.describe(db.fetch_one("v_evidence_media", ("EVID-102",)))
        check("an edge-declared octet-stream does not become the header",
              em.content_type_for(
                  em.safe_local_path(db.CLIPS["gv-1"]["local_path"]).name,
                  d["media_content_type"]) == "video/mp4", str(d))
        check("and the row is still classified as video",
              d["media_kind"] == "video", str(d))
        db.CLIPS["gv-1"]["content_type"] = "video/mp4"

        again = em.fetch_clip("gv-1")
        check("second fetch is a no-op (idempotent)", again.get("cached") is True,
              str(again))

        # a clip whose id would escape the directory if it were used as a name
        db.CLIPS["gv-2"] = {"event_id": "gv-2", "source_id": "../../etc",
                            "clip_id": "../../../passwd", "content_type": "video/mp4",
                            "file_path": r"C:\GeoVision\clips\odd.mp4",
                            "file_url": f"{base}/evidence/clips/x/file",
                            "fetch_status": "PENDING", "fetch_attempts": 0,
                            "local_path": None, "sha256": None}
        r2 = em.fetch_clip("gv-2")
        stored2 = em.safe_local_path(db.CLIPS["gv-2"].get("local_path"))
        check("hostile source_id/clip_id cannot escape the clip directory",
              r2.get("status") == "STORED" and stored2 is not None
              and stored2.parent == em.clip_dir().resolve(), str(r2))

        # declared hash that does not match what arrived
        db.CLIPS["gv-3"] = {"event_id": "gv-3", "source_id": "lane1",
                            "clip_id": "CLIP-BAD", "content_type": "video/mp4",
                            "file_path": r"C:\GeoVision\clips\bad.mp4",
                            "file_url": f"{base}/evidence/clips/CLIP-BAD/file",
                            "fetch_status": "PENDING", "fetch_attempts": 0,
                            "local_path": None, "sha256": "0" * 64}
        r3 = em.fetch_clip("gv-3")
        check("declared sha256 mismatch is refused, not stored",
              r3.get("status") == "SHA256_MISMATCH"
              and db.CLIPS["gv-3"]["fetch_status"] == "UNAVAILABLE", str(r3))

        # size ceiling
        db.CLIPS["gv-4"] = {"event_id": "gv-4", "source_id": "lane1",
                            "clip_id": "CLIP-BIG", "content_type": "video/mp4",
                            "file_path": r"C:\GeoVision\clips\big.mp4",
                            "file_url": f"{base}/evidence/clips/CLIP-BIG/file",
                            "fetch_status": "PENDING", "fetch_attempts": 0,
                            "local_path": None, "sha256": None}
        cfg.settings.GEOVISION_CLIP_MAX_BYTES = 1024
        r4 = em.fetch_clip("gv-4")
        cfg.settings.GEOVISION_CLIP_MAX_BYTES = 200_000_000
        check("a clip over the byte ceiling is refused",
              r4.get("status") == "UNAVAILABLE", str(r4))
        check("no partial file is left behind",
              not any(p.suffix == ".part" for p in em.clip_dir().iterdir()))
    finally:
        server.shutdown()
        server.server_close()

    # -- 5. the edge is gone ----------------------------------------------
    print("\n5. the edge going away is a recorded failure, never an exception")
    db.CLIPS["gv-5"] = {"event_id": "gv-5", "source_id": "lane1",
                        "clip_id": "CLIP-OFFLINE", "content_type": "video/mp4",
                        "file_path": r"C:\GeoVision\clips\off.mp4",
                        "file_url": f"{base}/evidence/clips/CLIP-OFFLINE/file",
                        "fetch_status": "PENDING", "fetch_attempts": 0,
                        "local_path": None, "sha256": None}
    try:
        r5 = em.fetch_clip("gv-5")
        raised = False
    except Exception as exc:  # noqa: BLE001
        r5, raised = {"error": repr(exc)}, True
    check("unreachable edge does not raise", not raised, str(r5))
    check("unreachable edge is recorded UNAVAILABLE with a reason",
          r5.get("status") == "UNAVAILABLE"
          and bool(db.CLIPS["gv-5"].get("fetch_error")), str(r5))
    d = em.describe(db.fetch_one("v_evidence_media", ("EVID-102",)))
    check("an unreachable edge does not disturb a clip already held",
          d["media_status"] == "AVAILABLE")

    print("\n6. a clip already STORED whose file was deleted")
    stored = em.safe_local_path(db.CLIPS["gv-1"]["local_path"])
    stored.unlink()
    d = em.describe(db.fetch_one("v_evidence_media", ("EVID-102",)))
    check("STORED row with no file reports UNAVAILABLE, not AVAILABLE",
          d["media_status"] == "UNAVAILABLE" and d["media_url"] is None, str(d))

    print("\n7. retry_pending sweeps everything not held")
    results = em.retry_pending(limit=50)
    check("retry attempted every unheld clip", len(results) >= 3, str(len(results)))

    print("\n8. fetching disabled")
    cfg.settings.GEOVISION_CLIP_FETCH_ENABLED = False
    r = em.fetch_clip("gv-5")
    check("fetch switched off reports SKIPPED", r.get("status") == "SKIPPED", str(r))
    cfg.settings.GEOVISION_CLIP_FETCH_ENABLED = True

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
