#!/usr/bin/env python3
"""STEP 4C unit tests: what the evidence API tells the operator.

    python3 scripts/test_step4c_evidence_api.py

No database, no network, no FastAPI - the real ``backend/app/evidence_media.py``
is loaded beside stub ``config`` and ``database`` modules, exactly as the 4A
suite does, and ``describe()`` is run over rows shaped like ``v_evidence_media``.

STEP 4C is the click-to-video step, and the two claims it has to make good on
below the UI are:

* every evidence row carries a provenance string built from IDENTIFIERS, so
  a Windows path can never reach a screen even by accident - the path stays
  in ``source_ref`` for the audit trail and nothing derives the label from it;
* a demo seed row is FLAGGED as a placeholder, so the dashboard can offer an
  evidence count that means "footage exists" rather than "rows exist".

Plus the clip metadata the modal shows - start, end, duration, frames, track.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "backend" / "app" / "evidence_media.py"

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail and not condition else ""))


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
def fetch_one(sql, params=None): return None
def fetch_all(sql, params=None): return []
def execute(sql, params=None): return None
'''


def build_module(root: Path):
    pkg = root / "wq4c"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "config.py").write_text(textwrap.dedent(STUB_CONFIG))
    (pkg / "database.py").write_text(textwrap.dedent(STUB_DB))
    shutil.copy2(SOURCE, pkg / "evidence_media.py")
    sys.path.insert(0, str(root))
    import wq4c.evidence_media as em  # noqa: E402
    import wq4c.config as cfg         # noqa: E402
    return em, cfg


WIN = r"C:\GeoVision\evidence_clips\CLIP-3f2a1b0c9d8e.mp4"
T0 = datetime(2026, 8, 30, 7, 12, 1, tzinfo=timezone.utc)


def clip_row(**over):
    """A v_evidence_media row for a GeoVision clip this Mac holds."""
    row = {
        "evidence_id": "EVID-042",
        "event_id": "EVENT-006",
        "evidence_type": "NON_SEGREGATION_PROOF",
        "file_path": "geovision://GEOVISION-D455-01/CLIP-3f2a1b0c9d8e",
        "captured_at": T0,
        "verified": False,
        "clip_event_id": "GV-EVT-1",
        "clip_source_id": "GEOVISION-D455-01",
        "clip_id": "CLIP-3f2a1b0c9d8e",
        "remote_file_path": WIN,
        "content_type": "video/mp4",
        "local_path": "geovision/CLIP-3f2a1b0c9d8e.mp4",
        "fetch_status": "STORED",
        "fetch_error": None,
        "media_state": "STORED",
        "clip_start": T0,
        "clip_end": T0 + timedelta(seconds=15),
        "frame_count": 131,
        "clip_track_id": 17,
    }
    row.update(over)
    return row


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wq4c-"))
    try:
        em, cfg = build_module(tmp)
        root = tmp / "evidence"
        (root / "geovision").mkdir(parents=True)
        cfg.settings.EVIDENCE_MEDIA_ROOT = str(root)
        (root / "geovision" / "CLIP-3f2a1b0c9d8e.mp4").write_bytes(b"\x00" * 2048)

        print("\n1. a held clip is playable, and says where it came from without a path")
        d = em.describe(clip_row())
        check("media_status is AVAILABLE", d["media_status"] == "AVAILABLE", str(d))
        check("media_url is the WASTRAQ endpoint",
              d["media_url"] == "/evidence/EVID-042/media", str(d["media_url"]))
        check("source_label names the device and the clip, not a file",
              d["source_label"] == "GeoVision GEOVISION-D455-01 · CLIP-3f2a1b0c9d8e",
              str(d["source_label"]))
        check("source_kind is GEOVISION_EDGE", d["source_kind"] == "GEOVISION_EDGE")
        check("no backslash, drive letter or directory in the label",
              "\\" not in d["source_label"] and "C:" not in d["source_label"]
              and "/" not in d["source_label"], str(d["source_label"]))
        check("the Windows path is still kept for the audit trail",
              d["source_ref"] == WIN, str(d["source_ref"]))
        check("a held clip is not a placeholder", d["is_placeholder"] is False)

        print("\n2. clip metadata the modal shows")
        check("clip_start survives", d["clip_start"] == T0)
        check("clip_seconds is computed from start and end", d["clip_seconds"] == 15.0,
              str(d["clip_seconds"]))
        check("frame_count survives", d["frame_count"] == 131)
        check("clip_track_id survives", d["clip_track_id"] == 17)
        half = em.describe(clip_row(clip_end=None))
        check("a clip missing an end has no duration, rather than a wrong one",
              half["clip_seconds"] is None, str(half["clip_seconds"]))
        backwards = em.describe(clip_row(clip_end=T0 - timedelta(seconds=5)))
        check("an end before the start yields no duration, not a negative one",
              backwards["clip_seconds"] is None, str(backwards["clip_seconds"]))

        print("\n3. an announced clip this Mac does not hold")
        p = em.describe(clip_row(media_state="PENDING", fetch_status="PENDING",
                                 local_path=None))
        check("media_status is PENDING", p["media_status"] == "PENDING", str(p))
        check("there is no url to play", p["media_url"] is None)
        check("it still says which device recorded it",
              p["source_label"] == "GeoVision GEOVISION-D455-01 · CLIP-3f2a1b0c9d8e")
        check("no path in the label", "C:" not in p["source_label"])
        check("clip metadata is available before the bytes are",
              p["clip_seconds"] == 15.0 and p["frame_count"] == 131)

        u = em.describe(clip_row(media_state="UNAVAILABLE", fetch_status="UNAVAILABLE",
                                 local_path=None, fetch_error="URLError: connection refused"))
        check("an undeliverable clip is UNAVAILABLE with the reason",
              u["media_status"] == "UNAVAILABLE" and "refused" in u["fetch_error"])

        print("\n4. a clip whose bytes were deleted underneath us")
        gone = em.describe(clip_row(local_path="geovision/NOT-THERE.mp4"))
        check("STORED with no file reports UNAVAILABLE, never AVAILABLE",
              gone["media_status"] == "UNAVAILABLE", str(gone["media_status"]))
        check("and still offers no url", gone["media_url"] is None)

        print("\n5. a clip with no identifiers still refuses to name a path")
        bare = em.describe(clip_row(clip_source_id=None, clip_id=None,
                                    media_state="PENDING", local_path=None))
        check("falls back to the device class, not the file",
              bare["source_label"] == "GeoVision edge", str(bare["source_label"]))

        print("\n6. demo seed rows are flagged, not dressed up")
        seed = em.describe({
            "evidence_id": "EVID-001", "event_id": "EVENT-001",
            "evidence_type": "COLLECTION_PROOF",
            "file_path": "/evidence/EVENT-001_collection_proof.jpg",
            "captured_at": T0, "verified": True,
            "clip_event_id": None, "media_state": "LOCAL",
        })
        check("status is NONE", seed["media_status"] == "NONE")
        check("is_placeholder is True", seed["is_placeholder"] is True, str(seed))
        check("source_kind is PLACEHOLDER", seed["source_kind"] == "PLACEHOLDER")
        check("the label says nothing was recorded",
              "no file was ever recorded" in seed["source_label"], str(seed["source_label"]))
        check("it offers no url", seed["media_url"] is None)

        print("\n7. a real local capture is not a placeholder")
        (root / "EVENT-009.mp4").write_bytes(b"\x00" * 512)
        local = em.describe({
            "evidence_id": "EVID-009", "event_id": "EVENT-009",
            "evidence_type": "VIDEO_CLIP",
            "file_path": str(root / "EVENT-009.mp4"),
            "captured_at": T0, "verified": False,
            "clip_event_id": None, "media_state": "LOCAL",
        })
        check("a local file on disk is AVAILABLE", local["media_status"] == "AVAILABLE",
              str(local["media_status"]))
        check("it is not a placeholder", local["is_placeholder"] is False)
        check("source_kind is LOCAL_CAPTURE", local["source_kind"] == "LOCAL_CAPTURE")
        check("the label carries the file name and not the directory",
              local["source_label"] == "WASTRAQ capture · EVENT-009.mp4",
              str(local["source_label"]))
        check("the label leaks no directory", "/" not in local["source_label"])

        print("\n8. every branch of describe() answers the same questions")
        for name, row in (("held", clip_row()),
                          ("pending", clip_row(media_state="PENDING", local_path=None)),
                          ("seed", {"evidence_id": "E", "event_id": "V",
                                    "file_path": "nope.jpg", "media_state": "LOCAL"})):
            out = em.describe(row)
            check(f"{name}: source_label, source_kind and is_placeholder are all present",
                  all(k in out for k in ("source_label", "source_kind", "is_placeholder")),
                  str(sorted(out)))
            check(f"{name}: no rendered field contains a Windows path",
                  "C:" not in str(out.get("source_label")) and
                  "C:" not in str(out.get("source_kind")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
