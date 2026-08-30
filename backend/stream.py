"""MJPEG video stream over the pipeline's latest annotated frame."""

from __future__ import annotations

import logging
import time

from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

BOUNDARY = "frame"


class VideoStreamer:
    """Yields the newest annotated frame until capture stops."""

    def __init__(self, pipeline=None) -> None:
        self._pipeline = pipeline

    @property
    def pipeline(self):
        # Resolved lazily so this module can be imported before services.py
        # has finished wiring, avoiding an import cycle with the routers.
        if self._pipeline is None:
            from services import pipeline as wired

            self._pipeline = wired
        return self._pipeline

    def generate(self):
        logger.debug("Video stream generator started")
        try:
            while self.pipeline.running:
                frame = self.pipeline.jpeg_frame()
                if frame is None:
                    time.sleep(0.03)
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
        except GeneratorExit:
            logger.debug("Video stream client disconnected")
        except Exception as exc:  # pragma: no cover
            logger.error("Stream error: %r", exc)
        finally:
            logger.debug("Video stream generator exited")

    def response(self) -> StreamingResponse:
        return StreamingResponse(
            self.generate(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        )


video_stream = VideoStreamer()
