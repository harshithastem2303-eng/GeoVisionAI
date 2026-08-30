"""Rate-limited, retrying, non-blocking delivery of events to WASTRAQ.

Three properties matter more than anything else here, and each one exists
because of a specific way this integration could break the demo:

**The capture loop must never wait on the network.** ``publish()`` appends to
an in-memory deque and returns. All HTTP happens on a background thread. A
WASTRAQ laptop that is asleep, on another subnet or behind a firewall costs
the RealSense pipeline nothing.

**The network must not be flooded at camera FPS.** Tracking runs at 30 FPS;
publishing runs at ``WASTRAQ_TRACK_PUBLISH_HZ`` *per track*. The throttle is
applied per track id, so two workers in frame produce two streams at 5 Hz
rather than one combined stream that starves whichever track loses the race.

**Memory must be bounded.** The retry queue has a hard cap. When it is full
the oldest event is dropped, because during an outage the newest position of
a picker is worth more than a thirty-second-old one, and an unbounded buffer
on a laptop that has been offline for an hour is a crash, not a feature.

Every event carries a UUID ``event_id``, so a retry that WASTRAQ already
received is deduplicated there rather than double-counted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Optional

from .client import TransportError, WastraqClient
from .events import EVENT_TRACK_UPDATE, heartbeat_event

logger = logging.getLogger(__name__)

#: How long the sender thread sleeps when it has nothing ready to send.
#: Short enough to stay responsive, long enough not to spin a core.
IDLE_POLL_S = 0.2

#: Cap on the per-track throttle bookkeeping so a long run with many
#: short-lived track ids cannot grow it without bound.
MAX_TRACKED_RATE_KEYS = 512


@dataclass
class QueuedEvent:
    """One event waiting to be delivered."""

    payload: dict
    attempts: int = 0
    not_before: float = 0.0
    queued_at: float = field(default_factory=time.time)

    @property
    def event_type(self) -> str:
        return self.payload.get("event_type", "UNKNOWN")

    @property
    def event_id(self) -> str:
        return self.payload.get("event_id", "")


class EventPublisher:
    """Owns the outbound queue and the thread that drains it."""

    def __init__(
        self,
        client: WastraqClient,
        queue_max: int = 500,
        retry_backoff_s: float = 5.0,
        max_attempts: int = 5,
        track_publish_hz: float = 5.0,
        heartbeat_s: float = 0.0,
        source_id: str = "GEOVISION-D455-01",
        status_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.client = client
        self.queue_max = max(1, int(queue_max))
        self.retry_backoff_s = retry_backoff_s
        self.max_attempts = max(1, int(max_attempts))
        self.track_publish_hz = track_publish_hz
        self.heartbeat_s = heartbeat_s
        self.source_id = source_id
        self.status_provider = status_provider

        self._queue: Deque[QueuedEvent] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()

        self._sender: Optional[threading.Thread] = None
        self._heartbeat: Optional[threading.Thread] = None

        self._last_track_publish: Dict[int, float] = {}
        self._rate_lock = threading.Lock()

        # Counters, all read by /integration/status.
        self.accepted = 0
        self.dropped_overflow = 0
        self.dropped_disabled = 0
        self.dropped_exhausted = 0
        self.delivered = 0
        self.failed_attempts = 0
        self.last_sent_at: Optional[float] = None
        self.last_track_sent_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # -- lifecycle --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._sender is not None and self._sender.is_alive()

    def start(self) -> None:
        """Start the sender (and heartbeat) threads. Idempotent."""

        if self.running:
            return
        self._stop.clear()
        self._sender = threading.Thread(
            target=self._sender_loop,
            name="wastraq-sender",
            daemon=True,
        )
        self._sender.start()

        if self.heartbeat_s and self.heartbeat_s > 0:
            self._heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                name="wastraq-heartbeat",
                daemon=True,
            )
            self._heartbeat.start()

        logger.info(
            "WASTRAQ publisher started (endpoint=%s, %.1f Hz/track, queue<=%d)",
            self.client.endpoint or "<unset>",
            self.track_publish_hz,
            self.queue_max,
        )

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in (self._sender, self._heartbeat):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
        self._sender = None
        self._heartbeat = None
        logger.info("WASTRAQ publisher stopped")

    # -- enqueue ----------------------------------------------------------

    def publish(self, payload: dict) -> bool:
        """Queue one event. Returns whether it was accepted.

        Never raises, never blocks on I/O. Safe to call from the camera
        thread, from a request handler, from anywhere.
        """

        if payload is None:
            return False

        if not self.client.configured:
            # Not an error: integration is simply switched off. Counted so
            # /integration/status can say so instead of looking idle.
            self.dropped_disabled += 1
            return False

        item = QueuedEvent(payload=payload)
        with self._lock:
            while len(self._queue) >= self.queue_max:
                discarded = self._queue.popleft()
                self.dropped_overflow += 1
                logger.warning(
                    "Outbound queue full (%d); dropped oldest %s %s",
                    self.queue_max,
                    discarded.event_type,
                    discarded.event_id,
                )
            self._queue.append(item)
            self.accepted += 1
        self._wake.set()
        return True

    # -- track rate limiting ----------------------------------------------

    @property
    def track_interval_s(self) -> float:
        if not self.track_publish_hz or self.track_publish_hz <= 0:
            return float("inf")
        return 1.0 / float(self.track_publish_hz)

    def should_publish_track(
        self,
        track_id: Optional[int],
        now: Optional[float] = None,
    ) -> bool:
        """Whether this track is due for an outbound update.

        A detection with no track id is never published: without a persistent
        id WASTRAQ cannot join it to anything, so sending it would be noise.
        """

        if track_id is None:
            return False
        interval = self.track_interval_s
        if interval == float("inf"):
            return False

        now = time.time() if now is None else now
        key = int(track_id)
        with self._rate_lock:
            last = self._last_track_publish.get(key)
            # Epsilon for the same reason as the evidence buffer: float
            # subtraction must not turn a 5 Hz allowance into 2.5 Hz.
            if last is not None and (now - last) < interval - 1e-9:
                return False
            self._last_track_publish[key] = now
            if len(self._last_track_publish) > MAX_TRACKED_RATE_KEYS:
                oldest = sorted(self._last_track_publish.items(), key=lambda kv: kv[1])
                for stale_key, _ in oldest[: len(oldest) // 2]:
                    self._last_track_publish.pop(stale_key, None)
            return True

    def publish_track_update(
        self,
        payload: dict,
        track_id: Optional[int],
        now: Optional[float] = None,
    ) -> bool:
        """Rate-limited :meth:`publish` for TRACK_UPDATE events."""

        if not self.should_publish_track(track_id, now=now):
            return False
        accepted = self.publish(payload)
        if accepted:
            self.last_track_sent_at = time.time() if now is None else now
        return accepted

    def reset_rate_limiter(self) -> None:
        """Forget per-track timing. Called when a capture session restarts."""

        with self._rate_lock:
            self._last_track_publish.clear()

    # -- sender -----------------------------------------------------------

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            item, wait_for = self._take_ready()

            if item is None:
                self._wake.wait(timeout=min(wait_for, IDLE_POLL_S))
                self._wake.clear()
                continue

            try:
                self.client.send(item.payload)
            except TransportError as exc:
                self._handle_failure(item, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                self._handle_failure(item, f"unexpected: {exc!r}")
            else:
                self.delivered += 1
                self.last_sent_at = time.time()
                self.last_error = None

        logger.debug("WASTRAQ sender thread exited")

    def _take_ready(self):
        """Pop the head if it is due; otherwise report how long until it is."""

        now = time.time()
        with self._lock:
            if not self._queue:
                return None, IDLE_POLL_S
            head = self._queue[0]
            if head.not_before > now:
                return None, max(0.0, head.not_before - now)
            return self._queue.popleft(), 0.0

    def _handle_failure(self, item: QueuedEvent, error: str) -> None:
        item.attempts += 1
        self.failed_attempts += 1
        self.last_error = error

        if item.attempts >= self.max_attempts:
            self.dropped_exhausted += 1
            logger.error(
                "Giving up on %s %s after %d attempt(s): %s",
                item.event_type,
                item.event_id,
                item.attempts,
                error,
            )
            return

        item.not_before = time.time() + self.retry_backoff_s
        with self._lock:
            if len(self._queue) >= self.queue_max:
                # The queue filled while we were away. The retry is older
                # than everything in it, so it is the one that gives way.
                self.dropped_overflow += 1
                logger.warning(
                    "Queue full on retry; dropping %s %s",
                    item.event_type,
                    item.event_id,
                )
                return
            self._queue.appendleft(item)
        logger.warning(
            "Delivery of %s %s failed (attempt %d/%d): %s -- retrying in %.1fs",
            item.event_type,
            item.event_id,
            item.attempts,
            self.max_attempts,
            error,
            self.retry_backoff_s,
        )

    # -- heartbeat --------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            try:
                status = self.status_provider() if self.status_provider else None
                self.publish(heartbeat_event(self.source_id, status=status))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Heartbeat failed to build: %r", exc)

    # -- introspection ----------------------------------------------------

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def drain_once(self, limit: int = 100) -> int:
        """Synchronously attempt the queue. For tests and manual flushing.

        Not used by the running service -- the sender thread does the work --
        but it makes the retry behaviour testable without sleeping.
        """

        sent = 0
        for _ in range(limit):
            item, _wait = self._take_ready()
            if item is None:
                break
            try:
                self.client.send(item.payload)
            except TransportError as exc:
                self._handle_failure(item, str(exc))
                break
            else:
                sent += 1
                self.delivered += 1
                self.last_sent_at = time.time()
        return sent

    def stats(self) -> dict:
        return {
            "running": self.running,
            "pending_events": self.pending(),
            "queue_max": self.queue_max,
            "track_publish_hz": self.track_publish_hz,
            "accepted": self.accepted,
            "delivered": self.delivered,
            "failed_attempts": self.failed_attempts,
            "dropped_overflow": self.dropped_overflow,
            "dropped_disabled": self.dropped_disabled,
            "dropped_exhausted": self.dropped_exhausted,
            "retry_backoff_s": self.retry_backoff_s,
            "max_attempts": self.max_attempts,
            "heartbeat_s": self.heartbeat_s,
            "last_sent_at": self.last_sent_at,
            "last_track_sent_at": self.last_track_sent_at,
            "last_error": self.last_error,
        }
