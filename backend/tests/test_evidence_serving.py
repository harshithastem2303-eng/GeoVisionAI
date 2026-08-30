"""STEP 4B: resolving a clip id to bytes, and refusing everything else.

Stdlib only -- no FastAPI, no camera, no WASTRAQ. The FastAPI handler in
``routes/evidence.py`` is a thin translation of :func:`resolve_clip_file`
into a response, so the decisions that matter (which id resolves, which is
refused, with what status, and what content type comes back) are all tested
here.

:class:`ClipHttpTests` then drives the *same* resolver over a real socket
with ``http.server``, so "a known clip fetch works", "an unknown clip 404s"
and "traversal is blocked" are measured end to end over HTTP rather than
asserted about a function.
"""

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evidence import RollingClipBuffer, store
from integration import events
from evidence.store import (
    ClipNotAFile,
    ClipNotFound,
    InvalidClipId,
    clip_file_url,
    resolve_clip_file,
    retrieval_metadata,
    sha256_file,
    validate_clip_id,
)

CLIP_ID = "CLIP-abc123def456"
CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"NOT-REALLY-VIDEO" * 64


class EvidenceRoot:
    """A throwaway evidence directory with one clip in it."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="gv-serve-"))
        self.clip = self.root / f"{CLIP_ID}.mp4"
        self.clip.write_bytes(CLIP_BYTES)
        # A secret one directory *above* the evidence root: the thing every
        # traversal attempt below is trying to reach.
        self.outside = self.root.parent / "gv-outside-secret.txt"
        self.outside.write_text("WASTRAQ_DB_PASSWORD=hunter2", encoding="utf-8")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.outside.unlink(missing_ok=True)


class ClipIdValidationTests(unittest.TestCase):
    def test_a_normal_clip_id_is_accepted(self):
        self.assertEqual(validate_clip_id(CLIP_ID), CLIP_ID)

    def test_traversal_shapes_are_rejected(self):
        for hostile in [
            "..",
            "../secret",
            "../../etc/passwd",
            "..\\..\\windows\\win.ini",
            "CLIP-1/../../../secret",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "\\\\server\\share\\x",
            ".hidden",
            "clip\x00.mp4",
            "clip id",
            "",
            "   ",
            "%2e%2e%2fsecret",  # only dangerous if decoded, and even then
        ]:
            with self.subTest(clip_id=hostile):
                with self.assertRaises(InvalidClipId):
                    validate_clip_id(hostile)

    def test_a_non_string_is_rejected_rather_than_coerced(self):
        for value in [None, 17, b"CLIP-1", ["CLIP-1"]]:
            with self.subTest(value=value):
                with self.assertRaises(InvalidClipId):
                    validate_clip_id(value)

    def test_an_over_long_id_is_rejected(self):
        with self.assertRaises(InvalidClipId):
            validate_clip_id("C" * 200)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = EvidenceRoot()
        self.addCleanup(self.fixture.cleanup)

    def test_a_known_clip_resolves_to_its_file(self):
        clip = resolve_clip_file(CLIP_ID, self.fixture.root)
        self.assertEqual(clip.path, self.fixture.clip)
        self.assertEqual(clip.content_type, "video/mp4")
        self.assertEqual(clip.size_bytes, len(CLIP_BYTES))
        self.assertEqual(clip.filename, f"{CLIP_ID}.mp4")

    def test_an_unknown_clip_is_not_found(self):
        with self.assertRaises(ClipNotFound):
            resolve_clip_file("CLIP-doesnotexist", self.fixture.root)

    def test_traversal_never_resolves_to_a_file_outside_the_root(self):
        for hostile in ["../gv-outside-secret", "../gv-outside-secret.txt", ".."]:
            with self.subTest(clip_id=hostile):
                with self.assertRaises(InvalidClipId):
                    resolve_clip_file(hostile, self.fixture.root)
        self.assertTrue(self.fixture.outside.is_file(), "fixture must survive")

    def test_a_hint_pointing_outside_the_root_is_ignored_not_followed(self):
        """The recorded path is a shortcut, never an authority."""

        with self.assertRaises(ClipNotFound):
            resolve_clip_file(
                "CLIP-nothinghere",
                self.fixture.root,
                hint=str(self.fixture.outside),
            )

    def test_a_hint_inside_the_root_is_used(self):
        clip = resolve_clip_file(
            CLIP_ID, self.fixture.root, hint=str(self.fixture.clip)
        )
        self.assertEqual(clip.path, self.fixture.clip)

    def test_a_stale_hint_falls_back_to_the_directory_scan(self):
        clip = resolve_clip_file(
            CLIP_ID,
            self.fixture.root,
            hint=str(self.fixture.root / f"{CLIP_ID}.deleted.mp4"),
        )
        self.assertEqual(clip.path, self.fixture.clip)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "no symlink support")
    def test_a_symlink_out_of_the_root_is_refused(self):
        link = self.fixture.root / "CLIP-escape.mp4"
        try:
            link.symlink_to(self.fixture.outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted on this machine")
        with self.assertRaises(ClipNotFound):
            resolve_clip_file("CLIP-escape", self.fixture.root)

    def test_a_stills_directory_is_reported_as_not_a_single_file(self):
        stills = self.fixture.root / "CLIP-stills"
        stills.mkdir()
        (stills / "00000.jpg").write_bytes(b"jpeg")
        with self.assertRaises(ClipNotAFile):
            resolve_clip_file("CLIP-stills", self.fixture.root)

    def test_an_unlisted_extension_is_not_served(self):
        (self.fixture.root / "CLIP-config.env").write_text("SECRET=1")
        with self.assertRaises(ClipNotFound):
            resolve_clip_file("CLIP-config", self.fixture.root)

    def test_video_wins_over_a_still_of_the_same_id(self):
        (self.fixture.root / f"{CLIP_ID}.jpg").write_bytes(b"jpeg")
        clip = resolve_clip_file(CLIP_ID, self.fixture.root)
        self.assertEqual(clip.content_type, "video/mp4")

    def test_the_scan_does_not_descend_into_subdirectories(self):
        nested = self.fixture.root / "nested"
        nested.mkdir()
        (nested / "CLIP-nested.mp4").write_bytes(b"x")
        with self.assertRaises(ClipNotFound):
            resolve_clip_file("CLIP-nested", self.fixture.root)

    def test_a_missing_evidence_directory_is_not_found_not_a_crash(self):
        with self.assertRaises(ClipNotFound):
            resolve_clip_file(CLIP_ID, self.fixture.root / "never-created")


class RetrievalMetadataTests(unittest.TestCase):
    def setUp(self):
        self.fixture = EvidenceRoot()
        self.addCleanup(self.fixture.cleanup)

    def test_url_is_relative_when_this_node_does_not_know_its_address(self):
        self.assertEqual(
            clip_file_url(CLIP_ID), f"/evidence/clips/{CLIP_ID}/file"
        )

    def test_url_is_absolute_when_a_public_base_is_configured(self):
        self.assertEqual(
            clip_file_url(CLIP_ID, "http://192.168.1.42:8000/"),
            f"http://192.168.1.42:8000/evidence/clips/{CLIP_ID}/file",
        )

    def test_metadata_describes_what_the_url_will_return(self):
        meta = retrieval_metadata(CLIP_ID, self.fixture.root)
        self.assertEqual(meta["file_url"], f"/evidence/clips/{CLIP_ID}/file")
        self.assertEqual(meta["content_type"], "video/mp4")
        self.assertEqual(meta["size_bytes"], len(CLIP_BYTES))
        self.assertEqual(meta["sha256"], hashlib.sha256(CLIP_BYTES).hexdigest())
        self.assertEqual(meta["file_name"], f"{CLIP_ID}.mp4")

    def test_metadata_carries_no_local_path(self):
        blob = json.dumps(retrieval_metadata(CLIP_ID, self.fixture.root))
        self.assertNotIn(str(self.fixture.root), blob)
        self.assertNotIn("\\\\", blob)

    def test_an_unresolvable_clip_yields_no_url_rather_than_a_broken_one(self):
        self.assertEqual(retrieval_metadata("CLIP-gone", self.fixture.root), {})

    def test_hashing_is_capped_and_reports_null_rather_than_stalling(self):
        self.assertIsNone(sha256_file(self.fixture.clip, max_bytes=8))
        meta = retrieval_metadata(CLIP_ID, self.fixture.root, hash_max_bytes=8)
        self.assertIsNone(meta["sha256"])
        # The rest of the metadata still stands.
        self.assertEqual(meta["size_bytes"], len(CLIP_BYTES))


# ---------------------------------------------------------------------------
# The same resolver, over a real socket
# ---------------------------------------------------------------------------


class _ClipHandler(BaseHTTPRequestHandler):
    """A minimal stand-in for the FastAPI route, using the same resolver.

    FastAPI is not importable in every environment this suite runs in, so the
    HTTP-level claims are proved against ``resolve_clip_file`` directly. The
    status mapping mirrors ``routes/evidence.py`` exactly: that mapping is
    the only thing the real handler adds.
    """

    root = None  # set by the test

    def log_message(self, *_args):  # keep the test output clean
        pass

    def do_GET(self):  # noqa: N802 - stdlib naming
        prefix, suffix = "/evidence/clips/", "/file"
        if not (self.path.startswith(prefix) and self.path.endswith(suffix)):
            self.send_error(404)
            return
        # Starlette hands the handler a *decoded* path parameter, so decode
        # here too -- otherwise this test would only prove that percent
        # escapes survive, which is not the interesting claim.
        clip_id = urllib.parse.unquote(self.path[len(prefix): -len(suffix)])
        try:
            clip = resolve_clip_file(clip_id, self.root)
        except store.ClipError as exc:
            body = json.dumps({"code": exc.code, "message": exc.detail}).encode()
            self.send_response(exc.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = clip.path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", clip.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)


class ClipHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = EvidenceRoot()
        self.addCleanup(self.fixture.cleanup)

        handler = type("Handler", (_ClipHandler,), {"root": self.fixture.root})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def fetch(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def test_a_known_clip_fetch_returns_the_bytes_and_the_right_type(self):
        response = self.fetch(f"/evidence/clips/{CLIP_ID}/file")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "video/mp4")
        self.assertEqual(response.read(), CLIP_BYTES)

    def test_an_unknown_clip_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.fetch("/evidence/clips/CLIP-nosuchthing/file")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(
            json.loads(caught.exception.read())["code"], "CLIP_NOT_FOUND"
        )

    def test_traversal_is_blocked_and_leaks_nothing(self):
        for hostile in [
            "..%2f..%2fgv-outside-secret.txt",
            "..\\gv-outside-secret.txt",
            "....//gv-outside-secret.txt",
            "%2e%2e%2fgv-outside-secret.txt",
        ]:
            with self.subTest(clip_id=hostile):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.fetch(f"/evidence/clips/{hostile}/file")
                self.assertIn(caught.exception.code, (400, 404))
                body = caught.exception.read().decode()
                self.assertNotIn("hunter2", body)
                self.assertNotIn(str(self.fixture.root), body)

    def test_an_absolute_path_is_not_a_clip_id(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.fetch("/evidence/clips/C:%5CWindows%5Cwin.ini/file")
        self.assertIn(caught.exception.code, (400, 404))


# ---------------------------------------------------------------------------
# The EVIDENCE_READY contract WASTRAQ consumes
# ---------------------------------------------------------------------------


class EvidenceReadyContractTests(unittest.TestCase):
    """What the Mac's STEP 4A ingestion reads off the envelope."""

    def setUp(self):
        self.fixture = EvidenceRoot()
        self.addCleanup(self.fixture.cleanup)

    def build(self, **overrides):
        meta = retrieval_metadata(
            CLIP_ID, self.fixture.root, base_url="http://192.168.1.42:8000"
        )
        payload = dict(
            source_id="GEOVISION-D455-01",
            clip_id=CLIP_ID,
            file_path=str(self.fixture.clip),
            start_time=1787900402.0,
            end_time=1787900415.0,
            track_id=17,
            rfid_event_id="rfid-evt-1",
            episode_id="EP-42",
            frame_count=130,
            session_id="sess123456ab",
            **meta,
        )
        payload.update(overrides)
        return events.evidence_ready_event(**payload)

    def test_it_carries_everything_needed_to_retrieve_the_clip(self):
        event = self.build()
        self.assertEqual(event["event_type"], "EVIDENCE_READY")
        self.assertEqual(
            event["file_url"],
            f"http://192.168.1.42:8000/evidence/clips/{CLIP_ID}/file",
        )
        self.assertEqual(event["content_type"], "video/mp4")
        self.assertEqual(event["size_bytes"], len(CLIP_BYTES))
        self.assertEqual(event["sha256"], hashlib.sha256(CLIP_BYTES).hexdigest())
        self.assertEqual(event["clip_id"], CLIP_ID)

    def test_the_retrieval_url_is_derived_from_the_id_not_the_path(self):
        event = self.build()
        self.assertIn(event["clip_id"], event["file_url"])
        self.assertNotIn("evidence_clips", event["file_url"])
        self.assertNotIn("\\", event["file_url"])

    def test_provenance_and_playback_are_separate_fields(self):
        event = self.build()
        self.assertNotEqual(event["file_path"], event["file_url"])
        # file_path stays the local path WASTRAQ shows as provenance only.
        self.assertTrue(event["file_path"].endswith(".mp4"))

    def test_episode_track_and_rfid_linkage_survive(self):
        event = self.build()
        self.assertEqual(event["episode_id"], "EP-42")
        self.assertEqual(event["track_id"], 17)
        self.assertEqual(event["rfid_event_id"], "rfid-evt-1")
        self.assertEqual(event["session_id"], "sess123456ab")

    def test_existing_metadata_is_preserved(self):
        event = self.build()
        for key in [
            "event_id",
            "timestamp",
            "source_id",
            "clip_id",
            "file_path",
            "start_time",
            "end_time",
            "frame_count",
        ]:
            with self.subTest(field=key):
                self.assertIn(key, event)
                self.assertIsNotNone(event[key])

    def test_it_is_still_a_reference_and_still_small(self):
        serialised = json.dumps(self.build())
        self.assertNotIn("base64", serialised)
        self.assertLess(len(serialised), 1024)

    def test_it_still_names_no_property(self):
        self.assertEqual(events.find_property_fields(self.build()), [])

    def test_an_unfetchable_clip_is_announced_without_a_url(self):
        meta = retrieval_metadata("CLIP-gone", self.fixture.root)
        event = events.evidence_ready_event(
            source_id="S",
            clip_id="CLIP-gone",
            file_path="C:\\evidence_clips\\CLIP-gone.mp4",
            start_time=1.0,
            end_time=2.0,
            **meta,
        )
        self.assertIsNone(event["file_url"])
        self.assertIsNone(event["sha256"])
        self.assertEqual(event["clip_id"], "CLIP-gone")

    def test_the_builder_still_works_with_no_retrieval_fields_at_all(self):
        """Backwards compatible: every new field is optional."""

        event = events.evidence_ready_event("S", "CLIP-1", "/tmp/x.mp4", 1.0, 2.0)
        self.assertEqual(event["event_type"], "EVIDENCE_READY")
        self.assertIsNone(event["file_url"])
        self.assertIsNone(event["episode_id"])


class ClipLinkageTests(unittest.TestCase):
    """The buffer carries the ids the clip must stay joined to."""

    def test_episode_id_is_carried_from_request_to_payload(self):
        buffer = RollingClipBuffer(
            directory=tempfile.mkdtemp(prefix="gv-linkage-"),
            encoder=lambda frame: f"F{frame}".encode(),
            writer=lambda frames, path, fps: f"{path}.mp4",
        )
        for index in range(20):
            buffer.add_frame(index, timestamp=1000.0 + index * 0.1)
        clip = buffer.request_clip(
            trigger_time=1001.0,
            track_id=17,
            rfid_event_id="rfid-evt-1",
            episode_id="EP-42",
            session_id="sess-1",
            blocking=True,
        )
        payload = clip.to_dict()
        self.assertEqual(payload["episode_id"], "EP-42")
        self.assertEqual(payload["track_id"], 17)
        self.assertEqual(payload["rfid_event_id"], "rfid-evt-1")
        self.assertEqual(payload["session_id"], "sess-1")

    def test_episode_id_defaults_to_null_and_is_never_invented(self):
        buffer = RollingClipBuffer(
            directory=tempfile.mkdtemp(prefix="gv-linkage-"),
            encoder=lambda frame: f"F{frame}".encode(),
            writer=lambda frames, path, fps: f"{path}.mp4",
        )
        buffer.add_frame(1, timestamp=1000.0)
        clip = buffer.request_clip(trigger_time=1000.0, blocking=True)
        self.assertIsNone(clip.to_dict()["episode_id"])


if __name__ == "__main__":
    unittest.main()
