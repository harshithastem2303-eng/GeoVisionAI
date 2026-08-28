"""LangSmith tracing for the Wastraq backend - optional, OFF by default.

WHY THIS FILE EXISTS
--------------------
Wastraq makes decisions it has to be able to defend later: which property a
picker coordinate was associated with, on what evidence, and why the engine
refused when it refused. Those decisions currently exist only as a JSON
response and a database row. Tracing records the whole decision tree -
request -> association -> the individual PostGIS queries - as one run in
LangSmith, so a disputed event can be replayed months later.

WHAT THIS IS NOT
----------------
There is no LLM anywhere in this backend. LangSmith is used here purely as a
run-tree recorder for ordinary Python functions (`@traceable` / `trace()`),
which is a supported use of the SDK. Nothing here sends anything to a model
provider, and no model provider key is needed.

DESIGN RULES (the same ones the vision package follows)
-------------------------------------------------------
1. OFF by default. `LANGSMITH_TRACING` is unset in a normal checkout, and the
   decorators below then return the original function untouched - not a
   wrapper, not a no-op call, the function itself. Zero runtime cost.
2. Never load-bearing. If `langsmith` is not installed, or is installed and
   broken, or the API key is missing, the backend serves every route exactly
   as it did before and `/health/tracing` says why tracing is off. An
   observability dependency must never be able to take down the property
   master.
3. Bounded payloads. SQL spans record the statement and a ROW COUNT, never
   the rows. A dashboard query returning 100 joined rows would otherwise ship
   the whole result set to a third party on every page load.
4. Nothing hot. The 30 fps camera loop is deliberately NOT traced - see
   docs/TRACING.md. One span per frame is 108,000 spans an hour and tells you
   nothing the /vision/status counters do not.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from .config import settings

F = TypeVar("F", bound=Callable[..., Any])

# --------------------------------------------------------------------------
# Import the SDK defensively. Same reasoning as main.py's vision guard: a
# dependency that will not import on this machine must cost us tracing and
# nothing else.
# --------------------------------------------------------------------------
LANGSMITH_IMPORT_ERROR: str | None = None
try:
    from langsmith import traceable as _ls_traceable
    from langsmith.run_helpers import trace as _ls_trace
except Exception as _exc:  # noqa: BLE001
    _ls_traceable = None  # type: ignore[assignment]
    _ls_trace = None  # type: ignore[assignment]
    LANGSMITH_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def _normalise_env() -> None:
    """Make the SDK agree with `settings`.

    config.py accepts `on`/`yes`/`1`/`true` for booleans; the LangSmith SDK
    reads the raw environment variable itself and only understands a narrower
    set. Without this, `LANGSMITH_TRACING=on` would enable our decorators and
    silently disable the SDK's own emit path - tracing that looks wired up and
    records nothing, which is worse than tracing that is plainly off.
    """
    if not settings.LANGSMITH_TRACING:
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    if settings.LANGSMITH_PROJECT:
        os.environ.setdefault("LANGSMITH_PROJECT", settings.LANGSMITH_PROJECT)
    if settings.LANGSMITH_ENDPOINT:
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.LANGSMITH_ENDPOINT)


_normalise_env()

# Decided once, at import time, because that is when the decorators below are
# applied. Toggling LANGSMITH_TRACING needs a backend restart - which is what
# you want anyway: a process that started untraced should not begin emitting
# half a decision tree mid-request.
_ENABLED: bool = bool(settings.LANGSMITH_TRACING) and _ls_traceable is not None
_HAVE_KEY: bool = bool(os.getenv("LANGSMITH_API_KEY"))


def tracing_enabled() -> bool:
    return _ENABLED


def tracing_status() -> dict[str, Any]:
    """What /health/tracing reports. Says why it is off, not just that it is."""
    if not settings.LANGSMITH_TRACING:
        reason = "LANGSMITH_TRACING is not set (tracing is off by default)"
    elif _ls_traceable is None:
        reason = f"langsmith package unavailable: {LANGSMITH_IMPORT_ERROR}"
    elif not _HAVE_KEY:
        reason = "LANGSMITH_API_KEY is not set - spans are created but cannot be sent"
    else:
        reason = None
    return {
        "enabled": _ENABLED and _HAVE_KEY,
        "decorators_active": _ENABLED,
        "api_key_present": _HAVE_KEY,
        "project": settings.LANGSMITH_PROJECT if _ENABLED else None,
        "sql_spans": bool(_ENABLED and settings.LANGSMITH_TRACE_SQL),
        "import_error": LANGSMITH_IMPORT_ERROR,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# @traceable
# --------------------------------------------------------------------------
def traceable(*d_args: Any, **d_kwargs: Any) -> Callable[[F], F]:
    """`langsmith.traceable` when tracing is on, the identity function when not.

    Always call it with parentheses - `@traceable(name="...")` - so there is
    one shape to reason about at every call site.
    """
    def decorator(func: F) -> F:
        if not _ENABLED:
            return func
        return _ls_traceable(*d_args, **d_kwargs)(func)  # type: ignore[misc]
    return decorator


# --------------------------------------------------------------------------
# trace_block(): a span for code that is not a whole function
# --------------------------------------------------------------------------
class _NullRun:
    """Stand-in returned when tracing is off, so call sites need no `if`."""

    def end(self, **_kwargs: Any) -> None:
        return None

    def add_metadata(self, *_a: Any, **_k: Any) -> None:
        return None


@contextmanager
def trace_block(
    name: str,
    run_type: str = "chain",
    inputs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Iterator[Any]:
    """Record one span. Yields something with `.end(outputs=...)` either way.

    A failure inside the tracing machinery must not become a failure of the
    thing being traced, so the SDK path is guarded and falls back to the null
    run rather than raising into a request handler.
    """
    if not _ENABLED:
        yield _NullRun()
        return
    try:
        cm = _ls_trace(name=name, run_type=run_type, inputs=inputs or {}, **kwargs)
    except Exception:  # noqa: BLE001
        yield _NullRun()
        return
    with cm as run:
        yield run


# --------------------------------------------------------------------------
# annotate_run(): tag the run a handler is already inside
# --------------------------------------------------------------------------
def annotate_run(**metadata: Any) -> None:
    """Attach searchable metadata to the currently open run.

    This is what makes a trace findable a year later. "Show me the run for
    event EVT-000123" only works if the event id is on the run, and the event
    id does not exist until after the INSERT - too late to be a function
    argument. Never raises: a failure to label a trace is not a failure to
    record a collection event.
    """
    if not _ENABLED:
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        run = get_current_run_tree()
        if run is None:
            return
        clean = {k: _scalar(v) for k, v in metadata.items() if v is not None}
        if not clean:
            return
        if hasattr(run, "add_metadata"):
            run.add_metadata(clean)
        else:  # older SDKs
            run.extra.setdefault("metadata", {}).update(clean)
    except Exception:  # noqa: BLE001
        return


# --------------------------------------------------------------------------
# SQL spans
# --------------------------------------------------------------------------
_SQL_PREVIEW_CHARS = 600


def sql_span(label: str, sql: str, params: Any) -> Any:
    """Context manager for one database round trip.

    Records the statement (truncated) and the parameters, and expects the
    caller to end it with a row COUNT. Deliberately not the rows: see rule 3
    at the top of this file.
    """
    if not (_ENABLED and settings.LANGSMITH_TRACE_SQL):
        return _null_span()
    statement = " ".join(sql.split())
    if len(statement) > _SQL_PREVIEW_CHARS:
        statement = statement[:_SQL_PREVIEW_CHARS] + " ...[truncated]"
    return trace_block(
        label,
        run_type="tool",
        inputs={"sql": statement, "params": _safe_params(params)},
    )


@contextmanager
def _null_span() -> Iterator[Any]:
    yield _NullRun()


def _safe_params(params: Any) -> Any:
    """Parameters are demo coordinates and ids, but keep them JSON-shaped."""
    try:
        if params is None:
            return None
        if isinstance(params, dict):
            return {k: _scalar(v) for k, v in params.items()}
        if isinstance(params, (list, tuple)):
            return [_scalar(v) for v in params]
        return _scalar(params)
    except Exception:  # noqa: BLE001
        return "<unserialisable>"


def _scalar(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    return s if len(s) <= 200 else s[:200] + "..."


# --------------------------------------------------------------------------
# ASGI middleware: one root run per HTTP request
# --------------------------------------------------------------------------
# A plain ASGI middleware, NOT starlette's BaseHTTPMiddleware. BaseHTTPMiddleware
# runs the downstream app in a separate anyio task, and LangSmith carries the
# current run in a contextvar - the root run set there would not be visible to
# the handlers underneath it, and every span would come out as its own
# orphaned trace. This form runs in the caller's context, so `@traceable`
# functions inside a route attach to the request run as children. FastAPI's
# sync `def` handlers run in a threadpool, and anyio copies the context into
# it, so that holds for the sync routes in this backend too.
class LangSmithTraceMiddleware:
    """Wrap each HTTP request in one LangSmith run.

    Excluded paths matter more than they look. `/vision/stream` is an MJPEG
    response that stays open for as long as the page is; `/vision/tracks` and
    `/summary` are polled by the dashboards every second or two. Tracing those
    would bury the collection events - the thing anyone actually opens
    LangSmith to look at - under thousands of identical polling spans.
    """

    def __init__(self, app: Any, exclude_prefixes: tuple[str, ...] | None = None) -> None:
        self.app = app
        self.exclude = tuple(
            exclude_prefixes
            if exclude_prefixes is not None
            else settings.LANGSMITH_TRACE_EXCLUDE
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not _ENABLED:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self.exclude):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        query = (scope.get("query_string") or b"").decode("latin-1")
        status_holder: dict[str, Any] = {}

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status_code"] = message.get("status")
            await send(message)

        with trace_block(
            f"{method} {path}",
            run_type="chain",
            inputs={"method": method, "path": path, "query": query or None},
            project_name=settings.LANGSMITH_PROJECT or None,
        ) as run:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:  # noqa: BLE001
                run.end(outputs={"status_code": 500}, error=f"{type(exc).__name__}: {exc}")
                raise
            run.end(outputs={"status_code": status_holder.get("status_code")})
