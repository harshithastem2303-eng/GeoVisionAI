#!/usr/bin/env python3
"""
STEP 3 - the Mac side of NON_SEGREGATION_TRIGGER, proved offline.

    python3 scripts/test_step3_non_segregation.py

No database, no FastAPI, no running backend, no Windows laptop, no camera.
pydantic is the only third-party import and only for the event contract.

Why this file exists next to test_episode_engine.py
---------------------------------------------------
That suite proves the episode ENGINE, and it covers the happy trigger path
(claims 8-11, 15). It never exercises the two paths that decide whether a
real second tap on demo day is safe:

  * a trigger that arrives AFTER its episode closed - the collector walking
    away and the tap racing each other over site wifi. Inside the late
    grace this must correct the collection event the episode already wrote,
    IN PLACE; outside it, it must refuse and preserve;
  * every refusal path's effect on the DASHBOARD's row, rather than on the
    episode object.

It also pins two cross-file invariants that no single-module test can see:
the set of resolutions the engine emits against the CHECK constraint that
has to accept them, and the transactional shape of the one store method
that writes to three tables.

The rig - fakes, camera pose, event builders - is imported from
test_episode_engine.py rather than copied, so there is exactly one
definition of "what the edge would have sent".

The claims, one per safety requirement of STEP 3:

  1  A trigger cannot carry or decide a property. (req 1, 8)
  2  No active episode -> no property and no event verdict changes. (req 2)
  3  A stale or aborted episode is preserved for review, never mutated. (req 3)
  4  A mismatched collector, track, session or camera is refused. (req 4, 5)
  5  The same trigger_id is applied exactly once, whatever the envelope. (req 6)
  6  A trigger for a just-closed episode corrects that episode's OWN event
     in place - and one that is too late does not. (req 7)
  7  One episode yields one collection event, trigger or no trigger. (req 9)
  8  The SEGREGATED default is untouched by any of the above.
  9  Cross-file: every resolution the engine emits is storable, and the
     closed-episode correction is one transaction, not three.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Import the STEP 2 rig. It is guarded by __main__, so nothing runs on import.
_spec = importlib.util.spec_from_file_location(
    "step2_rig", os.path.join(HERE, "test_episode_engine.py"))
assert _spec and _spec.loader
rig = importlib.util.module_from_spec(_spec)
sys.modules["step2_rig"] = rig
_spec.loader.exec_module(rig)

ADAPTER = rig.ADAPTER
build = rig.build
bound = rig.bound
track_ev = rig.track
trigger = rig.trigger
at_property = rig.at_property
walk = rig.walk

T0 = datetime(2026, 8, 30, 9, 0, 0, tzinfo=timezone.utc)
FAILURES: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return bool(condition)


def refused(payload: dict) -> bool:
    """True when the envelope validator rejects the payload outright."""
    try:
        ADAPTER.validate_python(payload)
    except Exception:
        return True
    return False


# =============================================================================
# scenario helpers
# =============================================================================
def live_episode(engine, store, t0=T0, prop="PROP-002", track_id=35):
    """Bind a collector and dwell until an episode opens. Returns (id, t)."""
    engine.on_worker_bound(bound(track_id, t0))
    right, forward = at_property(prop)
    _, t = walk(engine, track_id, t0 + timedelta(seconds=1), right, forward, 4.0)
    live = [e for e in store.episodes.values() if e["state"] == "ACTIVE"]
    assert len(live) == 1, f"expected one live episode, got {len(live)}"
    return live[0]["episode_id"], t


def leave(engine, store, t, track_id=35, prop="PROP-002"):
    """Walk out of the zone and let the leave grace expire."""
    away_r, away_f = at_property("PROP-001" if prop != "PROP-001" else "PROP-003")
    _, t = walk(engine, track_id, t + timedelta(seconds=0.5),
                away_r * 3, away_f, 6.0)
    return t


def events_for(store, episode_id):
    return [e for e in store.collection_events.values()
            if e.get("episode_id") == episode_id]


def evidence_for(store, event_id, kind="NON_SEGREGATION_PROOF"):
    return [e for e in store.evidence
            if e["event_id"] == event_id and e["evidence_type"] == kind]


# =============================================================================
# 1. a trigger cannot carry or decide a property            (req 1, req 8)
# =============================================================================
def check_no_property_in_trigger() -> None:
    print("\n1. the trigger carries a signal, never a verdict  (req 1, req 8)")

    base = {
        "event_type": "NON_SEGREGATION_TRIGGER", "event_id": "evt-p1",
        "timestamp": "2026-08-30T09:00:00Z", "source_id": "GEOVISION-D455-01",
        "session_id": "sess-demo", "trigger_id": "TRG-P1",
        "episode_id": "E-001", "collector_id": "PICKER-01",
        "track_id": 35, "trigger_status": "RESOLVED",
    }
    ok("1a. a clean trigger validates", not refused(dict(base)))

    for field, value in [
        ("property_id", "PROP-007"),
        ("segregation_status", "NOT_SEGREGATED"),
        ("house_number", "D007"),
        ("service_zone_id", "SZ-7"),
        ("collection_event_id", "EVENT-011"),
        ("authority_property_id", "MCC/007"),
    ]:
        ok(f"1b. a trigger asserting {field} is refused",
           refused(dict(base, **{field: value})))

    ok("1c. property_id nested in an unknown object is refused",
       refused(dict(base, edge_debug={"association": {"property_id": "PROP-007"}})))
    ok("1d. property_id inside a list is refused",
       refused(dict(base, candidates=[{"property_id": "PROP-007"}])))
    ok("1e. even a NULL property_id is refused - the KEY is the assertion",
       refused(dict(base, property_id=None)))

    model = ADAPTER.validate_python(dict(base))
    ok("1f. no property field exists on the model at all",
       not any(hasattr(model, f) for f in
               ("property_id", "segregation_status", "service_zone_id")))
    ok("1g. a resolved trigger that names no episode is refused",
       refused(dict(base, episode_id=None)))
    ok("1h. but NO_ACTIVE_EPISODE with a null episode is publishable",
       not refused(dict(base, episode_id=None,
                        trigger_status="NO_ACTIVE_EPISODE")))


# =============================================================================
# 2. no active episode -> nothing changes                          (req 2)
# =============================================================================
def check_no_active_episode() -> None:
    print("\n2. with no episode of our own, nothing is decided  (req 2)")

    engine, store, _, _, _ = build()
    res = engine.on_non_segregation_trigger(
        trigger(T0, trigger_id="TRG-NONE", episode_id=None,
                status="NO_ACTIVE_EPISODE"))
    ok("2a. a trigger arriving into an empty engine is not applied",
       res["applied"] is False, str(res))
    ok("2b. it is preserved for review, not dropped",
       res["needs_review"] is True and store.triggers["TRG-NONE"]["needs_review"])
    ok("2c. no episode was created", store.episodes == {})
    ok("2d. no collection event was created", store.collection_events == {})
    ok("2e. no property row was touched", store.assert_properties_untouched())

    engine, store, _, _, _ = build()
    res = engine.on_non_segregation_trigger(
        trigger(T0, trigger_id="TRG-GHOST", episode_id="E-999"))
    ok("2f. a trigger naming an episode that does not exist is refused",
       res["resolution"] == "UNKNOWN_EPISODE" and not res["applied"], str(res))
    ok("2g. and it changed no property", res.get("property_id") is None)

    # The important one: WE have a live episode, but the EDGE says it could
    # not resolve the tap. The edge's veto wins; we do not helpfully supply
    # our own episode to a tap that was never pointed at it.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    res = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-VETO", episode_id=None,
                status="NO_ACTIVE_EPISODE"))
    ok("2h. the edge's own NO_ACTIVE_EPISODE is believed, not overridden",
       res["resolution"] == "EDGE_UNRESOLVED" and not res["applied"], str(res))
    ok("2i. our live episode is still SEGREGATED",
       store.episodes[ep]["segregation_status"] == "SEGREGATED")

    for status in ("AMBIGUOUS", "UNKNOWN_RFID", "NOT_BOUND", "ERROR", "FAILED"):
        engine, store, _, _, _ = build()
        ep, t = live_episode(engine, store)
        res = engine.on_non_segregation_trigger(
            trigger(t, trigger_id=f"TRG-{status}", episode_id=ep, status=status))
        ok(f"2j. edge status {status} vetoes rather than applies",
           res["resolution"] == "EDGE_UNRESOLVED"
           and store.episodes[ep]["segregation_status"] == "SEGREGATED",
           str(res))

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    engine.config.enabled = False
    res = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-OFF", episode_id=ep))
    ok("2k. with the engine switched off the trigger is kept, not applied",
       res["resolution"] == "ENGINE_DISABLED" and res["needs_review"] is True
       and store.episodes[ep]["segregation_status"] == "SEGREGATED", str(res))


# =============================================================================
# 3. stale episode -> preserve, never mutate                       (req 3)
# =============================================================================
def check_stale_episode() -> None:
    print("\n3. a stale episode is preserved for review  (req 3)")

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    t = leave(engine, store, t)
    ok("3a. the episode closed on its own", store.episodes[ep]["state"] == "CLOSED")
    ok("3b. and closed SEGREGATED",
       store.episodes[ep]["segregation_status"] == "SEGREGATED")
    before = events_for(store, ep)
    ok("3c. it wrote exactly one collection event", len(before) == 1)
    event_id = before[0]["event_id"]

    late = store.episodes[ep]["ended_at"] + timedelta(
        seconds=engine.config.trigger_late_grace_s + 5)
    res = engine.on_non_segregation_trigger(
        trigger(late, trigger_id="TRG-LATE", episode_id=ep))
    ok("3d. a trigger past the late grace is not applied",
       res["resolution"] == "EPISODE_NOT_ACTIONABLE" and not res["applied"],
       str(res))
    ok("3e. it is preserved for review", res["needs_review"] is True)
    ok("3f. the episode is still SEGREGATED",
       store.episodes[ep]["segregation_status"] == "SEGREGATED")
    ok("3g. the DASHBOARD row is still SEGREGATED",
       store.collection_events[event_id]["segregation_status"] == "SEGREGATED")
    ok("3h. and not rfid_triggered",
       store.collection_events[event_id]["rfid_triggered"] is False)
    ok("3i. no second event was written", len(events_for(store, ep)) == 1)
    ok("3j. no non-segregation proof was fabricated",
       evidence_for(store, event_id) == [])

    # An ABORTED episode (engine reset mid-collection) is not correctable.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    engine.reset()
    ok("3k. reset leaves the episode ABORTED",
       store.episodes[ep]["state"] == "ABORTED")
    res = engine.on_non_segregation_trigger(
        trigger(t + timedelta(seconds=1), trigger_id="TRG-ABORT", episode_id=ep))
    ok("3l. a trigger for an ABORTED episode is refused, not applied",
       res["resolution"] == "EPISODE_NOT_ACTIONABLE" and not res["applied"],
       str(res))
    ok("3m. the aborted episode wrote no collection event",
       events_for(store, ep) == [])
    ok("3n. no property row was touched", store.assert_properties_untouched())


# =============================================================================
# 4. identity mismatch -> refuse                              (req 4, req 5)
# =============================================================================
def check_identity_mismatch() -> None:
    print("\n4. a trigger whose identity is not ours is refused  (req 4, 5)")

    cases = [
        ("collector", dict(collector="PICKER-02")),
        ("track", dict(track_id=99)),
        ("capture session", dict(session_id="sess-other")),
    ]
    for label, over in cases:
        engine, store, _, _, _ = build()
        ep, t = live_episode(engine, store)
        res = engine.on_non_segregation_trigger(
            trigger(t, trigger_id=f"TRG-{label}", episode_id=ep, **over))
        ok(f"4a. a trigger with the wrong {label} is refused",
           res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"],
           str(res))
        ok(f"4b. the {label} mismatch is preserved for review",
           res["needs_review"] is True)
        ok(f"4c. the episode is untouched after the {label} mismatch",
           store.episodes[ep]["segregation_status"] == "SEGREGATED")

    # A different camera entirely.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    payload = trigger(t, trigger_id="TRG-src", episode_id=ep).model_dump()
    payload["source_id"] = "GEOVISION-OTHER-CAM"
    payload["event_id"] = "evt-src"
    res = engine.on_non_segregation_trigger(ADAPTER.validate_python(payload))
    ok("4d. a trigger from another camera is refused",
       res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"], str(res))

    # Missing information is not a contradiction: a field the edge omitted
    # must not be treated as a mismatch, or a sparse edge build silently
    # stops being able to flag anything.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    res = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-sparse", episode_id=ep,
                collector=None, track_id=None))
    ok("4e. an omitted collector/track is missing info, not a mismatch",
       res["applied"] is True, str(res))
    ok("4f. and it still resolves to OUR property",
       res["property_id"] == store.episodes[ep]["property_id"])

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    res = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-4bad", episode_id="E-999"))
    ok("4g. four bad triggers later the episode is still SEGREGATED",
       store.episodes[ep]["segregation_status"] == "SEGREGATED")


# =============================================================================
# 5. exactly once                                                  (req 6)
# =============================================================================
def check_idempotency() -> None:
    print("\n5. the same decision is applied exactly once  (req 6)")

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)

    first = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-ONE", episode_id=ep))
    ok("5a. the first trigger is applied", first["applied"] is True, str(first))
    prop = store.episodes[ep]["property_id"]
    ok("5b. it resolved to WASTRAQ's property", first["property_id"] == prop)

    # Same decision, FRESH envelope: a retry-queue restart on the edge
    # re-announces the trigger under a new event_id. Transport dedup cannot
    # see this; only trigger_id can.
    second = engine.on_non_segregation_trigger(
        trigger(t + timedelta(seconds=1), trigger_id="TRG-ONE", episode_id=ep))
    ok("5c. the same trigger_id under a new event_id is a no-op",
       second["resolution"] == "DUPLICATE" and second["applied"] is False,
       str(second))
    ok("5d. exactly one trigger row exists for that decision",
       len(store.triggers) == 1, str(list(store.triggers)))
    ok("5e. the episode was flipped once, by the first trigger",
       store.episodes[ep]["non_segregation_trigger_id"] == "TRG-ONE")

    # A DIFFERENT trigger_id for an already-flagged episode must not
    # re-apply either - one tap, one flag.
    third = engine.on_non_segregation_trigger(
        trigger(t + timedelta(seconds=2), trigger_id="TRG-TWO", episode_id=ep))
    ok("5f. a second, different trigger on the same episode is refused",
       third["resolution"] == "EPISODE_NOT_ACTIONABLE" and not third["applied"],
       str(third))
    ok("5g. it is preserved for review", third["needs_review"] is True)
    ok("5h. the episode still names the FIRST trigger",
       store.episodes[ep]["non_segregation_trigger_id"] == "TRG-ONE")

    # A duplicate must not wander onto whatever is live now.
    t = leave(engine, store, t)
    ep2, t = live_episode(engine, store, t0=t + timedelta(seconds=2),
                          prop="PROP-003", track_id=35)
    dup = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-ONE", episode_id=ep2))
    ok("5i. a re-announced trigger cannot flag the NEXT house",
       dup["resolution"] == "DUPLICATE"
       and store.episodes[ep2]["segregation_status"] == "SEGREGATED", str(dup))
    ok("5j. exactly one collection event carries NOT_SEGREGATED",
       len([e for e in store.collection_events.values()
            if e["segregation_status"] == "NOT_SEGREGATED"]) == 1)


# =============================================================================
# 6. the just-closed episode                                       (req 7)
# =============================================================================
def check_closed_episode_correction() -> None:
    print("\n6. a trigger that lost the race with the collector leaving  (req 7)")

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    t = leave(engine, store, t)

    row = store.episodes[ep]
    ok("6a. the episode closed SEGREGATED and wrote its event",
       row["state"] == "CLOSED" and row["segregation_status"] == "SEGREGATED"
       and bool(row.get("collection_event_id")))
    event_id = row["collection_event_id"]
    ok("6b. the dashboard row says SEGREGATED, AUTO_CONFIRMED",
       store.collection_events[event_id]["segregation_status"] == "SEGREGATED"
       and store.collection_events[event_id]["review_status"] == "AUTO_CONFIRMED")

    inside = row["ended_at"] + timedelta(
        seconds=engine.config.trigger_late_grace_s - 5)
    res = engine.on_non_segregation_trigger(
        trigger(inside, trigger_id="TRG-RACE", episode_id=ep))
    ok("6c. inside the late grace the trigger IS applied",
       res["applied"] is True and res["resolution"] == "APPLIED", str(res))
    ok("6d. to the episode WASTRAQ itself chose",
       res["property_id"] == row["property_id"])
    ok("6e. the episode is now NOT_SEGREGATED",
       store.episodes[ep]["segregation_status"] == "NOT_SEGREGATED")

    # This is the whole point of req 7: the row the dashboard reads.
    ev = store.collection_events[event_id]
    ok("6f. the EXISTING collection event was corrected in place",
       ev["segregation_status"] == "NOT_SEGREGATED", str(ev))
    ok("6g. it is now rfid_triggered", ev["rfid_triggered"] is True)
    ok("6h. and flagged NEEDS_REVIEW", ev["review_status"] == "NEEDS_REVIEW")
    ok("6i. no SECOND collection event was created",
       len(events_for(store, ep)) == 1, str(events_for(store, ep)))
    ok("6j. exactly one NON_SEGREGATION_PROOF row was added",
       len(evidence_for(store, event_id)) == 1)
    ok("6k. no property row was touched", store.assert_properties_untouched())

    # Re-announcing it must not add a second proof row.
    again = engine.on_non_segregation_trigger(
        trigger(inside + timedelta(seconds=1), trigger_id="TRG-RACE",
                episode_id=ep))
    ok("6l. re-announcing the correction is a no-op",
       again["resolution"] == "DUPLICATE")
    ok("6m. still exactly one proof row",
       len(evidence_for(store, event_id)) == 1)

    # A different trigger for the same corrected episode.
    other = engine.on_non_segregation_trigger(
        trigger(inside + timedelta(seconds=2), trigger_id="TRG-RACE-2",
                episode_id=ep))
    ok("6n. a different trigger on the corrected episode is refused",
       other["resolution"] == "EPISODE_NOT_ACTIONABLE" and not other["applied"],
       str(other))
    ok("6o. and still exactly one proof row",
       len(evidence_for(store, event_id)) == 1)

    # Identity is checked BEFORE the closed-episode branch, not after.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    t = leave(engine, store, t)
    inside = store.episodes[ep]["ended_at"] + timedelta(seconds=2)
    res = engine.on_non_segregation_trigger(
        trigger(inside, trigger_id="TRG-RACE-WRONG", episode_id=ep,
                collector="PICKER-02"))
    ok("6p. the wrong collector cannot correct a closed episode either",
       res["resolution"] == "IDENTITY_MISMATCH" and not res["applied"], str(res))
    ok("6q. its event is still SEGREGATED",
       store.collection_events[store.episodes[ep]["collection_event_id"]]
       ["segregation_status"] == "SEGREGATED")


# =============================================================================
# 7. one episode, one event                                        (req 9)
# =============================================================================
def check_one_event_per_episode() -> None:
    print("\n7. one episode yields one collection event  (req 9)")

    # Trigger while ACTIVE: the event does not exist yet, so the verdict
    # must ride out on the event the CLOSE writes - not on an extra one.
    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    res = engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-ACT", episode_id=ep))
    ok("7a. the live episode is flagged", res["applied"] is True, str(res))
    ok("7b. no collection event exists yet - it is still open",
       events_for(store, ep) == [])

    t = leave(engine, store, t)
    rows = events_for(store, ep)
    ok("7c. closing writes exactly one event", len(rows) == 1, str(rows))
    ev = rows[0]
    ok("7d. it is NOT_SEGREGATED", ev["segregation_status"] == "NOT_SEGREGATED")
    ok("7e. rfid_triggered", ev["rfid_triggered"] is True)
    ok("7f. NEEDS_REVIEW", ev["review_status"] == "NEEDS_REVIEW")
    ok("7g. it carries the property WASTRAQ associated",
       ev["property_id"] == store.episodes[ep]["property_id"])
    ok("7h. and the episode id, so the dashboard can trace it back",
       ev["episode_id"] == ep)
    ok("7i. exactly one NON_SEGREGATION_PROOF row",
       len(evidence_for(store, ev["event_id"])) == 1)
    ok("7j. walking further away writes no second event",
       len(events_for(store, ep)) == 1)
    ok("7k. no property row was touched", store.assert_properties_untouched())


# =============================================================================
# 8. the SEGREGATED default survives all of this
# =============================================================================
def check_segregated_default_intact() -> None:
    print("\n8. the existing SEGREGATED behaviour is unchanged")

    engine, store, _, _, _ = build()
    ep, t = live_episode(engine, store)
    t = leave(engine, store, t)
    rows = events_for(store, ep)
    ok("8a. an episode with no trigger closes SEGREGATED",
       len(rows) == 1 and rows[0]["segregation_status"] == "SEGREGATED")
    ok("8b. AUTO_CONFIRMED, not NEEDS_REVIEW",
       rows[0]["review_status"] == "AUTO_CONFIRMED")
    ok("8c. and not rfid_triggered", rows[0]["rfid_triggered"] is False)
    ok("8d. with no non-segregation proof attached",
       evidence_for(store, rows[0]["event_id"]) == [])

    # Two houses: only the flagged one changes.
    engine, store, _, _, _ = build()
    ep1, t = live_episode(engine, store, prop="PROP-001")
    t = leave(engine, store, t, prop="PROP-001")
    ep2, t = live_episode(engine, store, t0=t + timedelta(seconds=2),
                          prop="PROP-002")
    engine.on_non_segregation_trigger(
        trigger(t, trigger_id="TRG-H2", episode_id=ep2))
    t = leave(engine, store, t, prop="PROP-002")
    ok("8e. the first house is still SEGREGATED",
       store.episodes[ep1]["segregation_status"] == "SEGREGATED")
    ok("8f. the second is NOT_SEGREGATED",
       store.episodes[ep2]["segregation_status"] == "NOT_SEGREGATED")
    by_prop = {e["property_id"]: e["segregation_status"]
               for e in store.collection_events.values()}
    ok("8g. and the dashboard rows agree, one each",
       by_prop.get(store.episodes[ep1]["property_id"]) == "SEGREGATED"
       and by_prop.get(store.episodes[ep2]["property_id"]) == "NOT_SEGREGATED",
       str(by_prop))
    ok("8h. two episodes, two events, no extras",
       len(store.collection_events) == 2)


# =============================================================================
# 9. cross-file invariants the module tests cannot see
# =============================================================================
def check_cross_file_invariants() -> None:
    print("\n9. the engine's verdicts are storable, and the correction is atomic")

    engine_src = open(os.path.join(ROOT, "backend", "app", "episodes",
                                   "engine.py"), encoding="utf-8").read()
    store_src = open(os.path.join(ROOT, "backend", "app", "episodes",
                                  "store.py"), encoding="utf-8").read()
    sql = open(os.path.join(ROOT, "database", "episodes.sql"),
               encoding="utf-8").read()

    # Every resolution string the engine can write, against the CHECK that
    # has to accept it. A resolution the constraint rejects would make
    # resolve_trigger raise, and the engine swallows that: the trigger row
    # would silently stay PENDING and needs_review FALSE - a lost signal.
    emitted = set(re.findall(r'_resolve\(\s*trigger_id,\s*"([A-Z_]+)"', engine_src))
    emitted.add("DUPLICATE")
    check = re.search(r"resolution\s+TEXT NOT NULL DEFAULT 'PENDING'\s*"
                      r"CHECK \(resolution IN \((.*?)\)\)", sql, re.S)
    allowed = set(re.findall(r"'([A-Z_]+)'", check.group(1) if check else ""))
    ok("9a. the SQL CHECK lists every resolution the engine emits",
       emitted <= allowed, f"missing from SQL: {sorted(emitted - allowed)}")
    ok("9b. the engine emits at least the six refusal paths",
       {"UNKNOWN_EPISODE", "EPISODE_NOT_ACTIONABLE", "IDENTITY_MISMATCH",
        "EDGE_UNRESOLVED", "ENGINE_DISABLED", "ERROR"} <= emitted,
       str(sorted(emitted)))

    # The correction writes to three tables. database.execute() commits per
    # statement, so three execute() calls would be three transactions and a
    # crash between them would leave the dashboard permanently SEGREGATED
    # with no retry path (the first statement sets the idempotency anchor).
    body = store_src[store_src.index("def mark_closed_non_segregated"):
                     store_src.index("def close_episode")]
    ok("9c. the closed-episode correction opens one explicit transaction",
       body.count("get_conn()") == 1, body.count("get_conn()"))
    ok("9d. and uses no per-statement execute() helper inside it",
       "execute(" not in body.replace("cur.execute(", ""))
    ok("9e. it commits exactly once", body.count("conn.commit()") == 1)
    ok("9f. and rolls back rather than committing a partial flip",
       "conn.rollback()" in body)

    # Both flips stay conditional in SQL - this is what makes "exactly once"
    # a property of the database rather than of the engine's memory.
    for name in ("mark_non_segregated", "mark_closed_non_segregated"):
        seg = store_src[store_src.index(f"def {name}"):]
        seg = seg[:seg.index("RETURNING *;")]
        ok(f"9g. {name} is conditional on the episode not being flagged yet",
           "non_segregation_trigger_id IS NULL" in seg)

    ok("9h. the trigger table has no property column of any kind",
       not re.search(r"^\s+(property_id|service_zone_id|house_number)\s",
                     sql[sql.index("CREATE TABLE IF NOT EXISTS "
                                   "geovision_non_segregation_triggers"):
                         sql.index("COMMENT ON TABLE "
                                   "geovision_non_segregation_triggers")],
                     re.M))
    ok("9i. and no segregation_status column either - it holds a signal",
       "segregation_status" not in
       sql[sql.index("CREATE TABLE IF NOT EXISTS "
                     "geovision_non_segregation_triggers"):
           sql.index("COMMENT ON TABLE geovision_non_segregation_triggers")])


# =============================================================================
def main() -> int:
    print("=" * 72)
    print("STEP 3 - NON_SEGREGATION_TRIGGER handling, offline")
    print("=" * 72)

    check_no_property_in_trigger()
    check_no_active_episode()
    check_stale_episode()
    check_identity_mismatch()
    check_idempotency()
    check_closed_episode_correction()
    check_one_event_per_episode()
    check_segregated_default_intact()
    check_cross_file_invariants()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
