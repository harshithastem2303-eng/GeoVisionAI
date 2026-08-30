"""Resolving a clip id to the file on disk, safely.

STEP 4B. WASTRAQ (the Mac) knows a clip only by its ``clip_id``; it never
sees, and must never need, a path on this Windows machine. Everything here
turns that id into a file *inside the configured evidence directory* -- or
into a refusal.

Why the lookup is by id and never by path
-----------------------------------------
A path parameter is a directory-traversal bug waiting to be written. There
is no route, no query parameter and no event field on this side that accepts
a filesystem path from the network, so the traversal class of bug has no
entry point rather than a filter in front of it.

Two independent guards, on purpose:

1. ``validate_clip_id`` -- the id must match a strict character class. No
   separators, no ``..``, no drive letters, no NUL, nothing URL-encoded that
   survives decoding into one of those.
2. ``_within`` -- whatever path was produced is ``resolve()``d and must still
   sit under the resolved evidence root. This catches what a character class
   cannot: a symlink or Windows junction *inside* the evidence directory
   pointing out of it.

Stdlib only. The evidence package stays importable with no OpenCV, no
FastAPI and no camera, so all of this is testable on any machine.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Clip ids are minted by :class:`evidence.buffer.RollingClipBuffer` as
#: ``CLIP-<12 hex>``. The pattern is slightly wider than that so a future id
#: scheme does not silently 400, but it still admits no path syntax: no ``/``,
#: no ``\``, no ``:``, no ``..``, and never a leading dot.
CLIP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Extensions the clip writer can produce, best first. Also the allow-list:
#: a file in the evidence directory with any other extension is not served.
CLIP_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

#: Preference order when several candidates share a clip id. Video first --
#: the Mac plays this in a ``<video>`` element.
_EXTENSION_ORDER = (".mp4", ".m4v", ".webm", ".mkv", ".avi", ".jpg", ".jpeg", ".png")

DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: The stable, path-free retrieval route. One place, so the event builder and
#: the router cannot drift apart.
CLIP_FILE_ROUTE = "/evidence/clips/{clip_id}/file"


class ClipError(Exception):
    """A clip could not be served. ``status`` is the HTTP status to answer.

    ``code`` is a short machine token; ``detail`` is for a human reading a
    log or a curl output. Neither ever contains a local path -- an error
    message is not a reason to leak the layout of this filesystem.
    """

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class InvalidClipId(ClipError):
    def __init__(self, detail: str = "Malformed clip id.") -> None:
        super().__init__(400, "INVALID_CLIP_ID", detail)


class ClipNotFound(ClipError):
    def __init__(self, detail: str = "No evidence clip with that id.") -> None:
        super().__init__(404, "CLIP_NOT_FOUND", detail)


class ClipNotAFile(ClipError):
    """The clip exists but is not one servable file.

    The writer falls back to a *directory* of numbered JPEGs when no video
    codec is available. That is still usable evidence, but it is not a
    response to ``GET .../file``, and answering 200 with something that is
    not the clip would be worse than saying so.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(409, "CLIP_NOT_A_SINGLE_FILE", detail)


@dataclass(frozen=True)
class ClipFile:
    """One resolved, in-root, servable clip."""

    clip_id: str
    path: Path
    filename: str
    content_type: str
    size_bytes: int


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_clip_id(clip_id: object) -> str:
    """Return ``clip_id`` unchanged, or raise :class:`InvalidClipId`.

    Rejects rather than sanitises. Silently rewriting ``../../secret`` into
    something acceptable would hide an attempt worth seeing in the log.
    """

    if not isinstance(clip_id, str):
        raise InvalidClipId()
    candidate = clip_id.strip()
    if not candidate or ".." in candidate or not CLIP_ID_PATTERN.match(candidate):
        raise InvalidClipId()
    return candidate


def _within(path: Path, root: Path) -> bool:
    """Is ``path`` inside ``root`` after both are fully resolved?

    ``resolve()`` on both sides is what makes this a symlink/junction check
    and not just a string prefix check.
    """

    try:
        return path.resolve(strict=False).is_relative_to(root.resolve(strict=False))
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _candidates(clip_id: str, root: Path) -> list:
    """Files in ``root`` that belong to ``clip_id``, best extension first.

    Not a recursive walk: clips are written flat into the evidence directory,
    and refusing to descend means a nested tree cannot be probed through this
    endpoint.
    """

    found = []
    for extension in _EXTENSION_ORDER:
        candidate = root / f"{clip_id}{extension}"
        if candidate.is_file():
            found.append(candidate)
    bare = root / clip_id
    if bare.is_file():
        found.append(bare)
    return found


def resolve_clip_file(
    clip_id: object,
    root,
    hint: Optional[str] = None,
) -> ClipFile:
    """``clip_id`` -> the file to send, or a :class:`ClipError`.

    ``hint`` is the path the buffer recorded when it wrote the clip. It is
    treated as a *shortcut, not an authority*: it is accepted only if it
    still resolves inside ``root`` and its name belongs to this clip id. A
    stale or tampered hint therefore degrades to the directory scan rather
    than escaping the root.
    """

    clip_id = validate_clip_id(clip_id)
    root_path = Path(root)

    chosen: Optional[Path] = None

    if hint:
        candidate = Path(hint)
        if (
            candidate.is_file()
            and candidate.name.startswith(clip_id)
            and _within(candidate, root_path)
        ):
            chosen = candidate
        elif candidate.is_dir() and candidate.name.startswith(clip_id):
            raise ClipNotAFile(
                "This clip was written as a directory of still frames, not a "
                "single video file."
            )

    if chosen is None:
        matches = _candidates(clip_id, root_path)
        if matches:
            chosen = matches[0]

    if chosen is None:
        # A stills directory left by the codec fallback: exists, but is not a
        # file. Reported distinctly so the Mac can say why rather than
        # retrying a 404 forever.
        stills = root_path / clip_id
        if stills.is_dir():
            raise ClipNotAFile(
                "This clip was written as a directory of still frames, not a "
                "single video file."
            )
        raise ClipNotFound()

    if not _within(chosen, root_path):
        # Reported as not-found on purpose: whether a path outside the
        # evidence directory exists is not this endpoint's news to give.
        logger.warning(
            "Refusing evidence clip %s: resolved outside the evidence directory.",
            clip_id,
        )
        raise ClipNotFound()

    extension = chosen.suffix.lower()
    if extension and extension not in CLIP_CONTENT_TYPES:
        raise ClipNotFound()

    try:
        size = chosen.stat().st_size
    except OSError as exc:
        raise ClipNotFound() from exc

    return ClipFile(
        clip_id=clip_id,
        path=chosen,
        filename=chosen.name,
        content_type=CLIP_CONTENT_TYPES.get(extension, DEFAULT_CONTENT_TYPE),
        size_bytes=size,
    )


# ---------------------------------------------------------------------------
# Retrieval metadata for EVIDENCE_READY
# ---------------------------------------------------------------------------


def clip_file_path(clip_id: str) -> str:
    """The relative, path-free retrieval endpoint for a clip."""

    return CLIP_FILE_ROUTE.format(clip_id=clip_id)


def clip_file_url(clip_id: str, base_url: str = "") -> str:
    """The URL announced to WASTRAQ.

    Absolute when this node has been told its own reachable address
    (``GEOVISION_PUBLIC_BASE_URL``); otherwise the relative endpoint, which
    WASTRAQ resolves against the GeoVision base URL it already holds. A
    guessed hostname would be worse than none: WASTRAQ knows how it reached
    this machine and this machine does not.
    """

    path = clip_file_path(clip_id)
    base = (base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


def sha256_file(path, max_bytes: Optional[int] = None) -> Optional[str]:
    """Digest of the file, or ``None`` if it is too large or unreadable.

    Bounded because this runs on the evidence thread just before announcing:
    an unbounded hash of a large clip would delay the announcement for no
    proportional benefit. ``None`` means "not computed", never "mismatch".
    """

    file_path = Path(path)
    try:
        if max_bytes is not None and file_path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        logger.debug("Could not hash evidence clip %s: %r", file_path.name, exc)
        return None


def retrieval_metadata(
    clip_id: str,
    root,
    hint: Optional[str] = None,
    base_url: str = "",
    hash_max_bytes: Optional[int] = None,
) -> dict:
    """The fields ``EVIDENCE_READY`` carries so WASTRAQ can fetch the bytes.

    Returns ``{}`` when the clip cannot be resolved. Announcing a URL for a
    file this node cannot serve would be a promise it cannot keep; the event
    still goes out with its existing fields, and WASTRAQ records the clip
    without media rather than chasing a 404.
    """

    try:
        clip = resolve_clip_file(clip_id, root, hint=hint)
    except ClipError as exc:
        logger.info(
            "No retrievable file for evidence clip %s (%s); announcing without "
            "a media URL.",
            clip_id,
            exc.code,
        )
        return {}

    return {
        "file_url": clip_file_url(clip.clip_id, base_url),
        "file_name": clip.filename,
        "content_type": clip.content_type,
        "size_bytes": clip.size_bytes,
        "sha256": sha256_file(clip.path, hash_max_bytes),
    }
