"""HTTP transport to the WASTRAQ backend.

Deliberately built on ``urllib.request`` rather than ``requests``: this is
one POST of one JSON body, the backend already avoids optional dependencies
(see ``config._load_env_file``), and every dependency is another wheel that
has to install on both a Windows laptop and a Mac.

The transport is injectable so the publisher, the retry logic and the tests
can be exercised with no network and no WASTRAQ running.

Nothing here retries and nothing here blocks for long: one attempt, a short
timeout, success or :class:`TransportError`. Retrying is the publisher's job
because only the publisher knows what else is queued.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """One delivery attempt failed. Says nothing about whether to retry."""


class HTTPTransport:
    """Single-shot JSON POST with a hard timeout."""

    def __init__(self, timeout_s: float = 2.0) -> None:
        self.timeout_s = timeout_s

    def post_json(self, url: str, payload: dict) -> int:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "GeoVision-Integration/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a reachable server refusing the event. Surfaced as
            # a TransportError with the status so the publisher can decide.
            raise TransportError(f"HTTP {exc.code} from {url}") from exc
        except Exception as exc:
            raise TransportError(f"{type(exc).__name__}: {exc}") from exc


class WastraqClient:
    """Knows where WASTRAQ is and whether we are allowed to talk to it.

    Holds the outcome of the last attempt so ``/integration/status`` can
    report reachability without making a fresh request -- a status endpoint
    that itself blocks on an unreachable host is worse than useless during a
    field test.
    """

    def __init__(
        self,
        base_url: str = "",
        events_path: str = "/integrations/geovision/events",
        enabled: bool = False,
        timeout_s: float = 2.0,
        transport=None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.events_path = events_path if events_path.startswith("/") else f"/{events_path}"
        self.enabled = bool(enabled)
        self.timeout_s = timeout_s
        self.transport = transport or HTTPTransport(timeout_s=timeout_s)

        self.last_success_at: Optional[float] = None
        self.last_failure_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.sent_count = 0
        self.failure_count = 0

    # -- configuration ----------------------------------------------------

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}{self.events_path}" if self.base_url else ""

    @property
    def configured(self) -> bool:
        """Enabled *and* pointed somewhere. Enabling without a URL is a no-op."""

        return self.enabled and bool(self.base_url)

    @property
    def reachable(self) -> Optional[bool]:
        """Tri-state: ``True`` / ``False`` / ``None`` for "not tried yet".

        ``False`` is only claimed after a real failure, and a later success
        clears it. Never guessed from configuration alone.
        """

        if self.last_success_at is None and self.last_failure_at is None:
            return None
        if self.last_failure_at is None:
            return True
        if self.last_success_at is None:
            return False
        return self.last_success_at >= self.last_failure_at

    # -- delivery ---------------------------------------------------------

    def send(self, payload: dict) -> int:
        """Deliver one event. Raises :class:`TransportError` on any failure."""

        if not self.configured:
            raise TransportError(
                "WASTRAQ integration is disabled or WASTRAQ_BASE_URL is unset."
            )

        try:
            status = self.transport.post_json(self.endpoint, payload)
        except TransportError as exc:
            self.last_failure_at = time.time()
            self.last_error = str(exc)
            self.failure_count += 1
            raise

        self.last_success_at = time.time()
        self.last_error = None
        self.sent_count += 1
        return status

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.base_url or None,
            "endpoint": self.endpoint or None,
            "timeout_s": self.timeout_s,
            "reachable": self.reachable,
            "sent_count": self.sent_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }
