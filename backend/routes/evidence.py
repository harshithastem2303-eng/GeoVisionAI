"""Read-only retrieval of evidence clips by clip id.

STEP 4B. The Mac already holds ``clip_id`` from an ``EVIDENCE_READY`` event;
this is the one route that turns it back into bytes::

    GET /evidence/clips/{clip_id}/file   ->  200 video/mp4 (Range-capable)
                                             400 malformed id
                                             404 unknown clip
                                             409 clip is a stills directory
                                             503 serving disabled

Deliberately narrow:

* **Read only.** No POST, no DELETE, no write of any kind. The only mutating
  evidence route in this service remains ``POST /integration/evidence``,
  which asks the buffer for a capture and touches no path.
* **By id, never by path.** Nothing here accepts a filename, a directory or
  a path fragment from the network, so there is no traversal to filter --
  ``evidence.store`` maps the id to a file and re-checks that the result is
  still inside the configured evidence directory.
* **Nothing but the clip.** Errors carry a code and a sentence, never a
  local path, and the response advertises no absolute filesystem location.
* **No credentials.** The Mac sends none and this asks for none; on a demo
  LAN, adding a shared secret that would have to live in two ``.env`` files
  buys less than it costs. Bind the API to the LAN interface, not the world.

``FileResponse`` gives correct ``Accept-Ranges`` / 206 handling, so the Mac's
``<video>`` element can seek without this route implementing byte ranges by
hand.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import config
from evidence import ClipError, ClipFile, resolve_clip_file

logger = logging.getLogger(__name__)

router = APIRouter(tags=["evidence"])


def lookup_clip(clip_id: str, root=None, hint=None) -> ClipFile:
    """Resolve a clip id, translating a refusal into an HTTP error.

    Split out from the handler so the decision -- which id resolves, which is
    rejected and with what status -- is testable without a running server.
    """

    try:
        return resolve_clip_file(
            clip_id,
            config.EVIDENCE_DIR if root is None else root,
            hint=hint,
        )
    except ClipError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": exc.detail},
        ) from exc


@router.get(
    "/evidence/clips/{clip_id}/file",
    response_class=FileResponse,
    summary="Fetch one evidence clip by clip id",
)
def get_clip_file(clip_id: str):
    """The bytes of one finished clip.

    ``HEAD`` comes along for free -- Starlette registers it alongside every
    ``GET`` -- so WASTRAQ can check size and content type before committing
    to a download.
    """

    if not config.EVIDENCE_SERVE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EVIDENCE_SERVING_DISABLED",
                "message": (
                    "Clip serving is off on this node "
                    "(GEOVISION_EVIDENCE_SERVE_ENABLED=false)."
                ),
            },
        )

    # Imported here rather than at module scope: the buffer pulls in the
    # whole application singleton graph, and this module stays importable --
    # and therefore testable -- without a camera or a database.
    from services import clip_buffer

    known = clip_buffer.find(clip_id) if _is_probably_an_id(clip_id) else None
    clip = lookup_clip(clip_id, hint=known.file_path if known else None)

    return FileResponse(
        path=clip.path,
        media_type=clip.content_type,
        filename=clip.filename,
        # Inline: a reviewer clicking a clip should see it play, not get a
        # download prompt. Explicit because FileResponse's ``filename``
        # otherwise implies ``attachment``.
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Clip-Id": clip.clip_id,
        },
    )


def _is_probably_an_id(clip_id: str) -> bool:
    """Cheap guard so a hostile id never reaches the in-memory scan."""

    return isinstance(clip_id, str) and ".." not in clip_id and len(clip_id) <= 128
