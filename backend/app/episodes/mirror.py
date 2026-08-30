"""The Windows episode-mirror client.

WASTRAQ is authoritative. GeoVision needs to know only ONE thing that
WASTRAQ knows: that an episode is currently live on a given track, so a
second RFID tap from the bound collector has something to point at. That is
the mirror, and it is all the mirror is.

    POST   {base}/episodes/active   {episode_id, track_id, association_status,
                                     collector_id?, session_id?}
    DELETE {base}/episodes/{id}
    GET    {base}/episodes          diagnostic

Three rules this module exists to keep
--------------------------------------
1. **No property authority leaves this machine.** ``_SENDABLE`` is a
   whitelist, not a blacklist: the payload is rebuilt field by field, so a
   property id cannot reach Windows even if someone later adds one to the
   episode object. Windows strips such fields on arrival as well; two
   independent guards, same as on the inbound side.

2. **The network never blocks ingestion.** Calls are queued to one daemon
   worker thread. The GeoVision edge gives up on a POST after 2 seconds, and
   an episode starting inside a TRACK_UPDATE request must not spend that
   budget waiting on a laptop that has gone to sleep.

3. **The network never corrupts state.** Every failure is caught, counted
   and recorded on the episode as ``mirror_status``. A mirror that never
   arrives means the demo loses the second-tap path; it does not mean the
   episode is wrong. The episode still closes SEGREGATED on its own.

Transport is stdlib ``urllib`` on purpose - the same choice the edge made,
and one less dependency to install on demo morning. ``transport`` is
swappable so tests exercise the real queue, retries and bookkeeping without
a socket.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Deque

#: The ONLY keys ever sent. Rebuilt from scratch per request.
_SENDABLE = ("episode_id", "track_id", "association_status",
             "collector_id", "session_id")

Transport = Callable[[str, str, dict | None, float], tuple[int, dict]]


def urllib_transport(method: str, url: str, body: dict | None,
                     timeout: float) -> tuple[int, dict]:
    """One HTTP round trip. Returns (status, decoded-body-or-{})."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WASTRAQ-EpisodeMirror/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read() or b"{}"
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, {"raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw.decode(errors="replace")}


class EpisodeMirror:
    """Fire-and-forget publisher for the Windows episode mirror."""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout_s: float = 2.0,
        enabled: bool = True,
        retries: int = 1,
        retry_delay_s: float = 0.5,
        max_queue: int = 64,
        transport: Transport | None = None,
        on_result: Callable[[str, str, bool, str | None], None] | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        # No base URL is not a misconfiguration, it is the normal state of a
        # laptop-free desk. Mirroring is simply off, and says so.
        self.enabled = bool(enabled and self.base_url)
        self.retries = max(0, retries)
        self.retry_delay_s = retry_delay_s
        self.transport = transport or urllib_transport
        #: called (episode_id, action, ok, error) after each attempt settles
        self.on_result = on_result

        self._queue: Deque[tuple[str, str, dict | None, str, str]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._inflight = 0
        self._max_queue = max_queue

        self.stats = {"queued": 0, "sent": 0, "failed": 0, "dropped": 0,
                      "disabled_skips": 0}
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        #: episode ids we believe Windows currently holds
        self.mirrored: set[str] = set()

    # -- public API ---------------------------------------------------------
    def publish_active(self, episode: dict[str, Any]) -> str:
        """Mirror one live episode. Returns the mirror_status to record."""
        if not self.enabled:
            self.stats["disabled_skips"] += 1
            return "DISABLED"
        payload = {k: episode.get(k) for k in _SENDABLE if episode.get(k) is not None}
        if "episode_id" not in payload or "track_id" not in payload:
            return "MIRROR_FAILED"
        payload.setdefault("association_status", "AUTO_ASSOCIATED")
        self._enqueue("POST", f"{self.base_url}/episodes/active", payload,
                      str(payload["episode_id"]), "PUBLISH")
        return "PENDING"

    def remove(self, episode_id: str) -> str:
        """Close the mirror. Called on every episode exit path."""
        if not self.enabled:
            self.stats["disabled_skips"] += 1
            return "DISABLED"
        self._enqueue("DELETE", f"{self.base_url}/episodes/{episode_id}", None,
                      episode_id, "REMOVE")
        return "PENDING"

    def peek(self) -> tuple[int, dict]:
        """GET /episodes on the edge. Synchronous - diagnostics only."""
        if not self.enabled:
            return 0, {"error": "mirror disabled"}
        try:
            return self.transport("GET", f"{self.base_url}/episodes", None,
                                  self.timeout_s)
        except Exception as exc:  # noqa: BLE001
            return 0, {"error": repr(exc)}

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue drains. For tests and for shutdown."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue and self._inflight == 0:
                    return True
            time.sleep(0.01)
        with self._lock:
            return not self._queue and self._inflight == 0

    def stop(self, timeout: float = 2.0) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            depth = len(self._queue)
        return {
            "enabled": self.enabled,
            "base_url": self.base_url or None,
            "timeout_s": self.timeout_s,
            "queue_depth": depth,
            "mirrored_episode_ids": sorted(self.mirrored),
            "last_error": self.last_error,
            "last_ok_age_s": (round(time.monotonic() - self.last_ok_at, 2)
                              if self.last_ok_at else None),
            **self.stats,
        }

    # -- internals ----------------------------------------------------------
    def _enqueue(self, method: str, url: str, body: dict | None,
                 episode_id: str, action: str) -> None:
        with self._wake:
            if len(self._queue) >= self._max_queue:
                # Oldest first: a stale PUBLISH for an episode that has since
                # closed is worth less than the DELETE behind it.
                self._queue.popleft()
                self.stats["dropped"] += 1
            self._queue.append((method, url, body, episode_id, action))
            self.stats["queued"] += 1
            self._ensure_worker_locked()
            self._wake.notify()

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._stopping = False
            self._worker = threading.Thread(
                target=self._run, name="episode-mirror", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._stopping:
                    self._wake.wait(timeout=1.0)
                if self._stopping and not self._queue:
                    return
                item = self._queue.popleft()
                self._inflight += 1
            try:
                self._deliver(*item)
            finally:
                with self._lock:
                    self._inflight -= 1

    def _deliver(self, method: str, url: str, body: dict | None,
                 episode_id: str, action: str) -> None:
        error: str | None = None
        for attempt in range(self.retries + 1):
            try:
                status, _ = self.transport(method, url, body, self.timeout_s)
            except Exception as exc:  # noqa: BLE001
                error = repr(exc)
            else:
                if 200 <= status < 300 or (action == "REMOVE" and status == 404):
                    # A DELETE for an episode Windows already forgot is a
                    # success: the desired end state is "not mirrored", and
                    # that is exactly where we are.
                    self.stats["sent"] += 1
                    self.last_ok_at = time.monotonic()
                    if action == "PUBLISH":
                        self.mirrored.add(episode_id)
                    else:
                        self.mirrored.discard(episode_id)
                    self._report(episode_id, action, True, None)
                    return
                error = f"HTTP {status}"
            if attempt < self.retries:
                time.sleep(self.retry_delay_s)

        self.stats["failed"] += 1
        self.last_error = f"{action} {episode_id}: {error}"
        if action == "REMOVE":
            # We could not confirm removal. Windows expires its own mirror
            # after EPISODE_MAX_AGE_S, so this degrades rather than sticks.
            self.mirrored.discard(episode_id)
        self._report(episode_id, action, False, error)

    def _report(self, episode_id: str, action: str, ok: bool,
                error: str | None) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(episode_id, action, ok, error)
        except Exception:  # noqa: BLE001
            # Bookkeeping. It must never take the worker thread down.
            pass


# --- process-wide instance ---------------------------------------------------
_mirror: EpisodeMirror | None = None
_mirror_lock = threading.Lock()


def get_mirror() -> EpisodeMirror:
    """The mirror this process uses, built from settings on first use."""
    global _mirror
    with _mirror_lock:
        if _mirror is None:
            from ..config import settings
            # on_result is left unset on purpose: EpisodeEngine attaches its
            # own, so the mirror_status is written through the same store the
            # engine uses rather than through a second one built here.
            _mirror = EpisodeMirror(
                base_url=settings.GEOVISION_EDGE_BASE_URL,
                timeout_s=settings.GEOVISION_EDGE_TIMEOUT_S,
                enabled=settings.GEOVISION_EDGE_MIRROR_ENABLED,
                retries=settings.GEOVISION_EDGE_RETRIES,
            )
        return _mirror


def set_mirror(mirror: EpisodeMirror | None) -> None:
    """Replace the process mirror. Tests and the reset endpoint."""
    global _mirror
    with _mirror_lock:
        _mirror = mirror
