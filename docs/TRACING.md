# Tracing (LangSmith)

Records the association decision tree - HTTP request -> `gis.lookup_property`
-> the individual PostGIS statements - as one run per request, so a disputed
collection event can be replayed later instead of argued about.

**Off by default.** A checkout with no `LANGSMITH_TRACING` behaves exactly as
it did before this existed: the decorators return the original functions
untouched, the middleware passes the call straight through, and nothing is
sent anywhere.

---

## There is no LLM here, and that is fine

LangSmith is normally pointed at LLM applications. This backend has no model
call anywhere in it. The SDK is used purely as a **run-tree recorder** for
ordinary Python functions (`@traceable`, `trace()`), which is a supported use
of the library. No model-provider key is involved and `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` are not needed - the `langsmith-trace` skill mentions them
because it assumes the LLM case.

What you get out of it is a searchable, timestamped record of *why the engine
decided what it decided*, with the losing candidates included. That is the
same thing the `reason` field in the API response says, except it is kept.

---

## Switching it on

```bash
# 1. install the SDK into the project venv (pure Python, no build step)
.venv/bin/pip install "langsmith>=0.1,<1.0"

# 2. put the key and the switch in backend/.env
LANGSMITH_TRACING=1
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=wastraq-demo

# 3. restart the backend
./scripts/run_backend.sh
```

The backend prints one line at start-up when tracing is live:

```
[tracing] LangSmith project='wastraq-demo' sending=True sql_spans=True
```

and `GET /health/tracing` answers at any time:

```json
{
  "enabled": true,
  "decorators_active": true,
  "api_key_present": true,
  "project": "wastraq-demo",
  "sql_spans": true,
  "import_error": null,
  "reason": null
}
```

`reason` is the useful field. It distinguishes *off*, *package missing* and
*key missing* - three states that otherwise look identical from the outside.

Toggling `LANGSMITH_TRACING` needs a backend restart. The decorators are
applied at import time, deliberately: a process that started untraced should
not begin emitting half a decision tree mid-request.

---

## Settings

| Variable | Default | What it does |
|---|---|---|
| `LANGSMITH_TRACING` | *unset* | The master switch. Everything below is inert without it. |
| `LANGSMITH_API_KEY` | *unset* | Required to actually send. Without it spans are built and dropped. |
| `LANGSMITH_PROJECT` | `wastraq-demo` | LangSmith project the runs land in. |
| `LANGSMITH_ENDPOINT` | *blank* | Set for the EU region or a self-hosted instance. |
| `LANGSMITH_TRACE_SQL` | `1` | One child span per database round trip. |
| `LANGSMITH_TRACE_EXCLUDE` | see `.env.example` | Request path prefixes that are never traced. |

---

## What is traced

**One root run per HTTP request**, from an ASGI middleware in
`backend/app/tracing.py`. Inputs: method, path, query. Outputs: status code.

**`gis.lookup_property`** - the core decision, as a child run. Inputs: the
coordinate and the search radius. Outputs: the whole result dict - decision,
confidence, method, the stated reason, and every candidate zone considered
*including the ones that lost*. A `NO_MATCH` or `AMBIGUOUS` refusal is
recorded exactly as fully as an association; the refusals are the interesting
half.

**Every SQL round trip** (`fetch_all`, `fetch_one`, `execute`) as a `tool`
span, when `LANGSMITH_TRACE_SQL` is on. Records the statement, normalised to
one line and truncated at 600 characters, plus the parameters - and a row
**count**, never the rows. `/summary` alone returns up to 100 joined event
rows, and shipping those to a third party on every dashboard poll is not what
anyone means by "turn on tracing".

**Event identifiers as run metadata.** `POST /collection-events` and
`POST /collection-events/{id}/non-segregated` call `annotate_run()` after the
write with the event id, property id, picker, segregation status and
confidence. This is what makes "show me the run for EVT-000123" work a year
later - the event id does not exist until after the INSERT, so it cannot be a
function argument.

Query them with the CLI (see `.claude/skills/langsmith-trace/SKILL.md`):

```bash
langsmith trace list --project wastraq-demo --limit 10 --api-key $LANGSMITH_API_KEY
langsmith trace list --project wastraq-demo --error --last-n-minutes 60 --api-key $LANGSMITH_API_KEY
langsmith trace list --project wastraq-demo --min-latency 2.0 --api-key $LANGSMITH_API_KEY
```

---

## What is deliberately NOT traced

**The camera loop.** `backend/app/vision/pipeline.py` runs at up to 30 fps.
One span per frame is 108,000 spans an hour, it would cost more than the
detector, and it tells you nothing that the counters in `/vision/status`
already tell you. Phase 1 perception is measured with those counters, not
with traces. If a frame-level trace is ever wanted, it belongs behind its own
sampling flag - not behind this one.

**Dashboard polling and the video stream.** `/vision/tracks`,
`/vision/status`, `/health*`, `/assets/*` and `/vision/stream` are in
`LANGSMITH_TRACE_EXCLUDE`. The dashboards poll every second or two and the
MJPEG response stays open for the life of the page; tracing those buries the
collection events under thousands of identical spans.

**The survey and property-master routes.** They are frozen infrastructure and
were not touched. They still get a root request span from the middleware and
SQL child spans through `database.py`, because those are shared - but no
decorator was added to any of their handlers.

---

## Failure behaviour

Every tracing path is wrapped so that a tracing failure cannot become a
request failure:

- `langsmith` not installed -> `/health/tracing` reports `import_error`, the
  backend serves normally.
- `LANGSMITH_TRACING` set but no API key -> reported in `reason`; spans are
  built and go nowhere.
- An exception inside the tracing machinery -> swallowed, the traced code
  runs anyway. `annotate_run()` in particular never raises: failing to label a
  trace must not fail a collection event.

This is the same rule the vision package follows - a camera that will not
import costs you `/vision/*` and nothing else.

---

## Files

| File | Change |
|---|---|
| `backend/app/tracing.py` | New. The whole tracing layer. |
| `backend/app/config.py` | `LANGSMITH_*` settings. |
| `backend/app/main.py` | Middleware registration, start-up line, `/health/tracing`. |
| `backend/app/database.py` | SQL spans around the three query helpers. |
| `backend/app/gis.py` | `@traceable` on `lookup_property`. |
| `backend/app/routes/collection_events.py` | `annotate_run()` after the two writes. |
| `backend/requirements.txt` | `langsmith>=0.1,<1.0`. |
| `backend/.env.example` | The variables above. |
| `.claude/skills/langsmith-*/` | The three LangSmith agent skills. |
