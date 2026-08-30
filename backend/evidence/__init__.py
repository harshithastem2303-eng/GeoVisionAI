"""Rolling camera evidence.

GeoVision keeps a short window of recent annotated frames in memory. When
something worth keeping happens -- an RFID tap that resolved to a track, or
an explicit request -- the window around that instant is written to a file
and its *path* is announced to WASTRAQ.

The bytes never leave this machine on their own. Continuously streaming video
to WASTRAQ over site wifi is the fastest available way to destabilise the
capture loop, so WASTRAQ receives a reference and fetches the clip only if a
human actually reviews the claim.

Since STEP 4B the reference is *retrievable*: :mod:`evidence.store` turns a
``clip_id`` back into the file on disk, and ``GET /evidence/clips/{clip_id}/file``
serves it read-only from the configured evidence directory. WASTRAQ still
pulls only when a human reviews the claim -- nothing is pushed.
"""

from .buffer import ClipRequest, RollingClipBuffer
from .store import (
    CLIP_CONTENT_TYPES,
    CLIP_FILE_ROUTE,
    ClipError,
    ClipFile,
    ClipNotAFile,
    ClipNotFound,
    InvalidClipId,
    clip_file_path,
    clip_file_url,
    resolve_clip_file,
    retrieval_metadata,
    sha256_file,
    validate_clip_id,
)

__all__ = [
    "RollingClipBuffer",
    "ClipRequest",
    "CLIP_CONTENT_TYPES",
    "CLIP_FILE_ROUTE",
    "ClipError",
    "ClipFile",
    "ClipNotAFile",
    "ClipNotFound",
    "InvalidClipId",
    "clip_file_path",
    "clip_file_url",
    "resolve_clip_file",
    "retrieval_metadata",
    "sha256_file",
    "validate_clip_id",
]
