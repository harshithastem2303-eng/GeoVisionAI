"""YOLO person detection + BoT-SORT tracking.

The detector answers exactly one question: *is this a person, and which
track is it?* It does not decide that anyone is a garbage worker. That
judgement belongs to :mod:`vision.rfid_binding`, which has evidence the
detector does not.

Ultralytics is imported lazily. Importing this module -- and therefore the
whole API and the whole test suite -- must not require the vision stack to be
installed or a model file to be present on disk.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .types import PersonDetection

logger = logging.getLogger(__name__)

COCO_PERSON_CLASS = 0


class PersonDetector:
    """Wraps an ultralytics model. Stateless with respect to identity."""

    def __init__(
        self,
        model_path: str = "yolo11n.pt",
        confidence: float = 0.40,
        tracker: str = "botsort.yaml",
    ) -> None:
        self.model_path = model_path
        self.confidence = confidence
        self.tracker = tracker
        self._model = None
        self._load_error: Optional[str] = None

    # -- model ------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    def load(self):
        """Load the model on first use. Errors are recorded, not raised."""

        if self._model is not None:
            return self._model

        try:
            from ultralytics import YOLO  # imported lazily on purpose
        except Exception as exc:  # pragma: no cover - depends on environment
            self._load_error = f"ultralytics unavailable: {exc}"
            logger.warning(self._load_error)
            return None

        try:
            logger.info("Loading person detection model %s", self.model_path)
            self._model = YOLO(self.model_path)
            self._load_error = None
            logger.info("Person detection model ready")
        except Exception as exc:  # pragma: no cover - depends on environment
            self._load_error = f"failed to load {self.model_path}: {exc}"
            logger.error(self._load_error)
            return None

        return self._model

    def reset_tracker(self) -> None:
        """Forget tracker state so a new run starts from a clean numbering.

        Paired with ``WorkerRegistry.start_session()``: track ids and worker
        bindings must be invalidated together.
        """

        model = self._model
        if model is None:
            return
        predictor = getattr(model, "predictor", None)
        if predictor is not None and hasattr(predictor, "trackers"):
            try:
                for tracker in predictor.trackers:
                    tracker.reset()
                logger.info("Tracker state reset")
            except Exception as exc:  # pragma: no cover
                logger.warning("Could not reset tracker cleanly: %s", exc)

    # -- inference --------------------------------------------------------

    def detect(self, frame) -> Tuple[object, List[PersonDetection]]:
        """Run detection + tracking on one BGR frame.

        Returns ``(annotated_frame, detections)``. If the model is
        unavailable the original frame comes back with an empty list, so the
        stream keeps serving and the failure is visible in ``/health``
        instead of taking the process down.
        """

        model = self.load()
        if model is None:
            return frame, []

        try:
            results = model.track(
                source=frame,
                persist=True,
                tracker=self.tracker,
                classes=[COCO_PERSON_CLASS],
                conf=self.confidence,
                verbose=False,
            )
        except Exception as exc:
            logger.error("Detection failed: %s", exc)
            return frame, []

        result = results[0]

        try:
            annotated = result.plot()
        except Exception:  # pragma: no cover - plotting is cosmetic
            annotated = frame

        detections: List[PersonDetection] = []
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return annotated, detections

        for box in boxes:
            try:
                confidence = float(box.conf.item())
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                track_id = int(box.id.item()) if box.id is not None else None
            except Exception:  # pragma: no cover - malformed row
                continue

            detections.append(
                PersonDetection(
                    track_id=track_id,
                    bbox=(x1, y1, x2, y2),
                    confidence=confidence,
                )
            )

        return annotated, detections


class PersonCropWriter:
    """Optional dataset helper: save one crop per track per interval.

    Preserved from the original implementation because the collected crops
    are useful for later appearance re-identification work, but disabled by
    default -- it wrote a JPEG per track per second into the repository.
    """

    def __init__(
        self,
        directory: Path,
        interval_s: float = 1.0,
        min_confidence: float = 0.60,
        max_per_id: int = 50,
        enabled: bool = False,
    ) -> None:
        self.directory = Path(directory)
        self.interval_s = interval_s
        self.min_confidence = min_confidence
        self.max_per_id = max_per_id
        self.enabled = enabled
        self._last_saved: dict = {}
        self._counts: dict = {}

    def maybe_save(
        self,
        frame,
        detections: Sequence[PersonDetection],
        now: Optional[float] = None,
    ) -> int:
        if not self.enabled or frame is None:
            return 0

        try:
            import cv2
        except Exception:  # pragma: no cover
            return 0

        now = time.time() if now is None else now
        self.directory.mkdir(parents=True, exist_ok=True)
        saved = 0

        height, width = frame.shape[:2]

        for detection in detections:
            track_id = detection.track_id
            if track_id is None:
                continue
            if detection.confidence < self.min_confidence:
                continue
            if self._counts.get(track_id, 0) >= self.max_per_id:
                continue
            if now - self._last_saved.get(track_id, 0.0) < self.interval_s:
                continue

            x1, y1, x2, y2 = detection.bbox
            crop = frame[
                max(0, y1): min(height, y2),
                max(0, x1): min(width, x2),
            ]
            if crop.size == 0 or crop.shape[0] < 80 or crop.shape[1] < 40:
                continue

            filename = f"id_{track_id}_{int(now * 1000)}.jpg"
            if cv2.imwrite(str(self.directory / filename), crop):
                self._last_saved[track_id] = now
                self._counts[track_id] = self._counts.get(track_id, 0) + 1
                saved += 1

        return saved
