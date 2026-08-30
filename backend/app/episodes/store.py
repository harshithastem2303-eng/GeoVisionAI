"""Persistence for episodes, non-segregation triggers and their evidence.

Raw SQL, same as the rest of the demo. The engine holds the state machine;
this module holds the writes, so the engine can be tested against a fake
store with no database in the room.

Everything here is idempotent where it can be. ``mark_non_segregated`` is
conditional in SQL rather than read-then-write, so two triggers racing on the
same episode cannot both "win" and produce two collection events.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from psycopg.errors import UniqueViolation

from ..database import execute, fetch_all, fetch_one, get_conn
from ..evidence_media import clip_uri
from ..models import fake_evidence_path, next_event_id, next_evidence_id


def _next_episode_id() -> str:
    row = fetch_one(
        """
        SELECT COALESCE(MAX(NULLIF(regexp_replace(episode_id, '^E-', ''), '')::int), 0) AS n
        FROM collection_episodes
        WHERE episode_id ~ '^E-[0-9]+$'
        """
    )
    return f"E-{((row or {}).get('n', 0) + 1):03d}"


class EpisodeStore:
    """The database side of the episode engine."""

    # -- episodes -----------------------------------------------------------
    def next_episode_id(self) -> str:
        return _next_episode_id()

    def create_episode(
        self,
        *,
        episode_id: str,
        property_id: str,
        collector_id: str | None,
        picker_id: str | None,
        source_id: str,
        session_id: str,
        track_id: int,
        association_status: str,
        association_confidence: float | None,
        association_method: str | None,
        started_at: datetime,
        last_seen_at: datetime,
        observations: int,
    ) -> dict[str, Any] | None:
        return execute(
            """
            INSERT INTO collection_episodes (
                episode_id, property_id, collector_id, picker_id,
                source_id, session_id, track_id,
                association_status, association_confidence, association_method,
                started_at, last_seen_at, observations, state, mirror_status
            ) VALUES (
                %(episode_id)s, %(property_id)s, %(collector_id)s, %(picker_id)s,
                %(source_id)s, %(session_id)s, %(track_id)s,
                %(association_status)s, %(association_confidence)s, %(association_method)s,
                %(started_at)s, %(last_seen_at)s, %(observations)s, 'ACTIVE', 'PENDING'
            )
            -- Matches uq_episode_active_collector. Returning no row is the
            -- documented "this collector already has a live episode" answer;
            -- raising a UniqueViolation here would only surface after a
            -- restart with an episode still open, which is exactly when the
            -- demo can least afford a traceback.
            ON CONFLICT (collector_id) WHERE state = 'ACTIVE' AND collector_id IS NOT NULL
            DO NOTHING
            RETURNING *;
            """,
            {
                "episode_id": episode_id,
                "property_id": property_id,
                "collector_id": collector_id,
                "picker_id": picker_id,
                "source_id": source_id,
                "session_id": session_id or "",
                "track_id": track_id,
                "association_status": association_status,
                "association_confidence": association_confidence,
                "association_method": association_method,
                "started_at": started_at,
                "last_seen_at": last_seen_at,
                "observations": observations,
            },
        )

    def touch_episode(self, episode_id: str, last_seen_at: datetime,
                      observations: int) -> None:
        execute(
            """
            UPDATE collection_episodes
               SET last_seen_at = GREATEST(last_seen_at, %(last_seen_at)s),
                   observations = %(observations)s,
                   updated_at   = now()
             WHERE episode_id = %(episode_id)s AND state = 'ACTIVE'
            """,
            {"episode_id": episode_id, "last_seen_at": last_seen_at,
             "observations": observations},
        )

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        return fetch_one(
            "SELECT * FROM collection_episodes WHERE episode_id = %s",
            (episode_id,),
        )

    def active_episodes(self) -> list[dict[str, Any]]:
        return fetch_all(
            "SELECT * FROM collection_episodes WHERE state = 'ACTIVE' "
            "ORDER BY started_at"
        )

    def recent_episodes(self, limit: int = 50,
                        property_id: str | None = None) -> list[dict[str, Any]]:
        return fetch_all(
            """
            SELECT * FROM v_episode_summary
             WHERE (%(prop)s::text IS NULL OR property_id = %(prop)s)
             ORDER BY started_at DESC
             LIMIT %(limit)s
            """,
            {"prop": property_id, "limit": limit},
        )

    def mark_non_segregated(self, episode_id: str, trigger_id: str,
                            when: datetime) -> dict[str, Any] | None:
        """Flip one ACTIVE episode. Conditional in SQL, so it happens once.

        Returns the updated row, or None when the episode was not ACTIVE or
        had already been flipped by a different trigger. ``None`` is the
        caller's signal to preserve the signal for review rather than to try
        harder.
        """
        return execute(
            """
            UPDATE collection_episodes
               SET segregation_status         = 'NOT_SEGREGATED',
                   non_segregation_trigger_id = %(trigger_id)s,
                   non_segregated_at          = %(when)s,
                   updated_at                 = now()
             WHERE episode_id = %(episode_id)s
               AND state = 'ACTIVE'
               AND non_segregation_trigger_id IS NULL
            RETURNING *;
            """,
            {"episode_id": episode_id, "trigger_id": trigger_id, "when": when},
        )

    def mark_closed_non_segregated(self, episode_id: str, trigger_id: str,
                                   when: datetime) -> dict | None:
        """Correct a just-closed episode, and the event it already wrote.

        The collector walking away and the second tap can race over a couple
        of seconds of wifi. Inside the late grace the trigger still belongs
        to that episode, so both rows are corrected together rather than the
        signal being dropped or - far worse - applied to whatever is open
        next.

        ONE transaction, deliberately. ``database.execute`` commits per
        statement, which would make this three: the episode flip, the
        collection event the dashboard actually reads, and the proof row. A
        failure after the first would leave the authority saying
        NOT_SEGREGATED while every dashboard query still said SEGREGATED -
        and unrecoverably so, because the flip sets
        ``non_segregation_trigger_id`` and every subsequent trigger, retried
        or fresh, is then correctly refused as already-resolved. Either all
        three land or the episode stays SEGREGATED and the trigger is
        preserved for review, which is a state a human can still fix.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collection_episodes
                   SET segregation_status         = 'NOT_SEGREGATED',
                       non_segregation_trigger_id = %(trigger_id)s,
                       non_segregated_at          = %(when)s,
                       updated_at                 = now()
                 WHERE episode_id = %(episode_id)s
                   AND state = 'CLOSED'
                   AND non_segregation_trigger_id IS NULL
                RETURNING *;
                """,
                {"episode_id": episode_id, "trigger_id": trigger_id,
                 "when": when},
            )
            row = cur.fetchone()
            if row is None:
                # Already resolved, or not CLOSED. Nothing was written.
                conn.rollback()
                return None

            event_id = row.get("collection_event_id")
            if event_id:
                cur.execute(
                    """
                    UPDATE collection_events
                       SET segregation_status = 'NOT_SEGREGATED',
                           rfid_triggered     = TRUE,
                           review_status      = CASE
                                                  WHEN review_status IN ('REVIEWED_OK','REVIEWED_REJECTED')
                                                  THEN review_status ELSE 'NEEDS_REVIEW'
                                                END
                     WHERE event_id = %s
                    """,
                    (event_id,),
                )
                # The evidence id is derived on THIS cursor, not through
                # models.next_evidence_id(): that helper borrows a second
                # pooled connection, which would read outside the
                # transaction we are in the middle of.
                cur.execute(
                    """
                    WITH n AS (
                        SELECT COALESCE(MAX(NULLIF(
                                 regexp_replace(evidence_id, '^EVID-', ''),
                                 '')::int), 0) + 1 AS seq
                          FROM evidence
                         WHERE evidence_id ~ '^EVID-[0-9]+$'
                    )
                    -- greatest(3, ...) so the zero-pad never TRUNCATES the
                    -- number the way a bare lpad(x, 3) would past EVID-999.
                    SELECT 'EVID-' || lpad(seq::text,
                                           greatest(3, length(seq::text)), '0')
                             AS next_id
                      FROM n
                    """
                )
                evidence_id = (cur.fetchone() or {})["next_id"]
                cur.execute(
                    """
                    INSERT INTO evidence (evidence_id, event_id, evidence_type,
                                          file_path, captured_at, verified)
                    VALUES (%s, %s, %s, %s, %s, FALSE)
                    """,
                    (evidence_id, event_id, "NON_SEGREGATION_PROOF",
                     fake_evidence_path(event_id, "NON_SEGREGATION_PROOF", when),
                     when),
                )
            conn.commit()
        return dict(row)

    def close_episode(self, episode_id: str, *, ended_at: datetime,
                      dwell_s: float, observations: int,
                      state: str = "CLOSED") -> dict[str, Any] | None:
        return execute(
            """
            UPDATE collection_episodes
               SET state        = %(state)s,
                   ended_at     = %(ended_at)s,
                   dwell_s      = %(dwell_s)s,
                   observations = GREATEST(observations, %(observations)s),
                   updated_at   = now()
             WHERE episode_id = %(episode_id)s AND state = 'ACTIVE'
            RETURNING *;
            """,
            {"episode_id": episode_id, "ended_at": ended_at,
             "dwell_s": dwell_s, "observations": observations, "state": state},
        )

    def set_mirror_status(self, episode_id: str, status: str,
                          error: str | None = None) -> None:
        execute(
            """
            UPDATE collection_episodes
               SET mirror_status = %(status)s,
                   mirror_error  = %(error)s,
                   updated_at    = now()
             WHERE episode_id = %(episode_id)s
            """,
            {"episode_id": episode_id, "status": status, "error": error},
        )

    def abort_active(self, reason_state: str = "ABORTED") -> list[str]:
        rows = fetch_all(
            """
            UPDATE collection_episodes
               SET state      = %(state)s,
                   ended_at   = COALESCE(ended_at, now()),
                   updated_at = now()
             WHERE state = 'ACTIVE'
            RETURNING episode_id;
            """,
            {"state": reason_state},
        )
        return [r["episode_id"] for r in rows]

    # -- collection events --------------------------------------------------
    def picker_for(self, collector_id: str | None,
                   rfid_uid: str | None = None) -> str | None:
        """Map an edge collector to a WASTRAQ picker. Never invents one.

        The edge roster uses WASTRAQ picker ids for the demo, so the direct
        hit is the common case; the RFID lookup is the fallback for a roster
        that drifts. An unknown collector yields NULL, and the episode is
        still recorded - a serviced property with an unidentified collector
        is a real, reportable state.
        """
        if collector_id:
            row = fetch_one(
                "SELECT picker_id FROM pickers WHERE picker_id = %s",
                (collector_id,),
            )
            if row:
                return row["picker_id"]
        if rfid_uid:
            row = fetch_one(
                "SELECT picker_id FROM pickers WHERE rfid_uid = %s", (rfid_uid,)
            )
            if row:
                return row["picker_id"]
        return None

    def create_collection_event(
        self,
        *,
        episode: dict[str, Any],
        collection_time: datetime,
        review_status: str,
    ) -> dict[str, Any] | None:
        event_id = next_event_id()
        row = execute(
            """
            INSERT INTO collection_events (
                event_id, property_id, picker_id, track_id, collected,
                segregation_status, association_confidence, collection_time,
                rfid_triggered, review_status, episode_id
            ) VALUES (
                %(event_id)s, %(property_id)s, %(picker_id)s, %(track_id)s, TRUE,
                %(segregation_status)s, %(confidence)s, %(collection_time)s,
                %(rfid)s, %(review_status)s, %(episode_id)s
            )
            RETURNING *;
            """,
            {
                "event_id": event_id,
                "property_id": episode["property_id"],
                "picker_id": episode.get("picker_id"),
                "track_id": (str(episode.get("track_id"))
                             if episode.get("track_id") is not None else None),
                "segregation_status": episode.get("segregation_status", "SEGREGATED"),
                "confidence": episode.get("association_confidence"),
                "collection_time": collection_time,
                "rfid": episode.get("segregation_status") == "NOT_SEGREGATED",
                "review_status": review_status,
                "episode_id": episode["episode_id"],
            },
        )
        execute(
            "UPDATE collection_episodes SET collection_event_id = %s, updated_at = now() "
            "WHERE episode_id = %s",
            (event_id, episode["episode_id"]),
        )
        return row

    def add_evidence(self, event_id: str, evidence_type: str,
                     file_path: str | None, captured_at: datetime,
                     clip_event_id: str | None = None) -> str:
        """Insert one evidence row. Returns its id.

        ``clip_event_id`` carries the partial unique index from
        ``database/evidence_media.sql``: one edge clip can produce at most
        one evidence row, enforced by the database rather than by a
        read-then-write check that two retries can both pass.
        """
        evidence_id = next_evidence_id()
        execute(
            """
            INSERT INTO evidence (evidence_id, event_id, evidence_type,
                                  file_path, captured_at, verified, clip_event_id)
            VALUES (%s, %s, %s, %s, %s, FALSE, %s)
            """,
            (evidence_id, event_id, evidence_type,
             file_path or fake_evidence_path(event_id, evidence_type, captured_at),
             captured_at, clip_event_id),
        )
        return evidence_id

    # -- triggers -----------------------------------------------------------
    def get_trigger(self, trigger_id: str) -> dict[str, Any] | None:
        return fetch_one(
            "SELECT * FROM geovision_non_segregation_triggers WHERE trigger_id = %s",
            (trigger_id,),
        )

    def claim_trigger(self, **fields: Any) -> bool:
        """Insert the trigger row. False when this ``trigger_id`` already ran.

        This is the semantic idempotency the transport-level ``event_id``
        dedup cannot provide: the edge can legitimately re-announce the same
        decision under a fresh envelope, and the second announcement must not
        mark a second house.
        """
        row = execute(
            """
            INSERT INTO geovision_non_segregation_triggers (
                trigger_id, event_id, source_id, session_id, event_time,
                claimed_episode_id, collector_id, rfid_uid, track_id,
                trigger_status, edge_duplicate, rfid_event_id
            ) VALUES (
                %(trigger_id)s, %(event_id)s, %(source_id)s, %(session_id)s,
                %(event_time)s, %(claimed_episode_id)s, %(collector_id)s,
                %(rfid_uid)s, %(track_id)s, %(trigger_status)s,
                %(edge_duplicate)s, %(rfid_event_id)s
            )
            ON CONFLICT (trigger_id) DO NOTHING
            RETURNING trigger_id;
            """,
            fields,
        )
        return row is not None

    def resolve_trigger(self, trigger_id: str, *, resolution: str,
                        detail: str | None = None,
                        applied_episode_id: str | None = None,
                        needs_review: bool = False) -> None:
        execute(
            """
            UPDATE geovision_non_segregation_triggers
               SET resolution         = %(resolution)s,
                   resolution_detail  = %(detail)s,
                   applied            = %(applied)s,
                   applied_episode_id = %(episode_id)s,
                   needs_review       = %(needs_review)s
             WHERE trigger_id = %(trigger_id)s
            """,
            {
                "trigger_id": trigger_id,
                "resolution": resolution,
                "detail": detail,
                "applied": resolution == "APPLIED",
                "episode_id": applied_episode_id,
                "needs_review": needs_review,
            },
        )

    def recent_triggers(self, limit: int = 25) -> list[dict[str, Any]]:
        return fetch_all(
            """
            SELECT t.*, ep.property_id AS resolved_property_id
              FROM geovision_non_segregation_triggers t
              LEFT JOIN collection_episodes ep ON ep.episode_id = t.applied_episode_id
             ORDER BY t.event_time DESC
             LIMIT %s
            """,
            (limit,),
        )

    # -- evidence clips -----------------------------------------------------
    def episode_for_rfid_event(self, rfid_event_id: str) -> str | None:
        row = fetch_one(
            """
            SELECT applied_episode_id FROM geovision_non_segregation_triggers
             WHERE rfid_event_id = %s AND applied_episode_id IS NOT NULL
             ORDER BY event_time DESC LIMIT 1
            """,
            (rfid_event_id,),
        )
        return (row or {}).get("applied_episode_id")

    def episode_for_clip(self, *, source_id: str, session_id: str | None,
                         track_id: int | None, clip_time: datetime,
                         window_s: float) -> dict[str, Any] | None:
        """The non-segregated episode a clip most plausibly belongs to.

        Narrow on purpose: same camera, same session, same track, flagged
        NOT_SEGREGATED, and overlapping the clip in time. A clip that matches
        nothing stays an unlinked clip - it is not attached to whichever
        episode happens to be nearest.
        """
        return fetch_one(
            """
            SELECT * FROM collection_episodes
             WHERE source_id = %(source_id)s
               AND session_id = COALESCE(%(session_id)s, '')
               AND (%(track_id)s::int IS NULL OR track_id = %(track_id)s)
               AND segregation_status = 'NOT_SEGREGATED'
               AND started_at <= %(clip_time)s + make_interval(secs => %(window)s)
               AND COALESCE(ended_at, now()) >= %(clip_time)s
                   - make_interval(secs => %(window)s)
             ORDER BY abs(EXTRACT(EPOCH FROM (%(clip_time)s - started_at)))
             LIMIT 1
            """,
            {"source_id": source_id, "session_id": session_id,
             "track_id": track_id, "clip_time": clip_time, "window": window_s},
        )

    def clip_by_identity(self, source_id: str, clip_id: str) -> dict[str, Any] | None:
        """The CANONICAL row for a clip, keyed by what identifies the clip.

        Not by ``event_id``: that identifies the DELIVERY. The edge may
        re-announce one clip under a fresh envelope, and ``_insert_clip``
        deliberately keeps the first row. Everything downstream must
        therefore resolve identity -> row rather than assume the envelope it
        is holding is the one on file, or it will link evidence to a clip
        row that does not exist and silently do nothing.
        """
        return fetch_one(
            """
            SELECT * FROM geovision_evidence_clips
             WHERE source_id = %s AND clip_id = %s
             ORDER BY received_at
             LIMIT 1
            """,
            (source_id, clip_id),
        )

    def evidence_for_clip(self, clip_event_id: str) -> dict[str, Any] | None:
        return fetch_one(
            "SELECT * FROM evidence WHERE clip_event_id = %s", (clip_event_id,)
        )

    def attach_clip(self, event_id: str, episode_id: str, *, clip_event_id: str,
                    file_path: str, captured_at: datetime) -> str:
        """Link one edge clip to one collection event, exactly once.

        Idempotent on ``clip_event_id``. A second EVIDENCE_READY for the
        same clip returns the evidence row the first one made instead of
        creating a duplicate; the operator sees one clip, because there is
        one clip.

        What lands in ``evidence.file_path`` is a ``geovision://`` URI, not
        ``file_path``. ``file_path`` is a WINDOWS path - correct as
        provenance, meaningless as a location on this machine - and it stays
        in ``geovision_evidence_clips`` where the schema says that is what
        it is. Playback resolves through ``/evidence/{id}/media``, which
        serves only bytes this Mac holds.
        """
        existing = self.evidence_for_clip(clip_event_id)
        if existing:
            # Keep the episode tag current (an unlinked clip may have been
            # tagged before the collection event existed), then stop.
            execute(
                """
                UPDATE geovision_evidence_clips
                   SET episode_id = COALESCE(episode_id, %s),
                       linked_evidence_id = %s
                 WHERE event_id = %s
                """,
                (episode_id, existing["evidence_id"], clip_event_id),
            )
            return existing["evidence_id"]

        clip = fetch_one(
            "SELECT source_id, clip_id FROM geovision_evidence_clips WHERE event_id = %s",
            (clip_event_id,),
        ) or {}
        uri = clip_uri(clip.get("source_id") or "edge",
                       clip.get("clip_id") or clip_event_id)

        try:
            evidence_id = self.add_evidence(event_id, "VIDEO_CLIP", uri,
                                            captured_at, clip_event_id=clip_event_id)
        except UniqueViolation:
            # Lost a race with a concurrent retry. The other writer's row is
            # the answer; there is no second clip to represent.
            existing = self.evidence_for_clip(clip_event_id)
            if existing:
                return existing["evidence_id"]
            raise

        execute(
            """
            UPDATE geovision_evidence_clips
               SET episode_id = %s, linked_evidence_id = %s
             WHERE event_id = %s
            """,
            (episode_id, evidence_id, clip_event_id),
        )
        return evidence_id

    def tag_clip_episode(self, clip_event_id: str, episode_id: str) -> None:
        execute(
            "UPDATE geovision_evidence_clips SET episode_id = %s WHERE event_id = %s",
            (episode_id, clip_event_id),
        )

    def pending_clips_for_episode(self, episode_id: str) -> list[dict[str, Any]]:
        return fetch_all(
            """
            SELECT event_id, source_id, clip_id, file_path, event_time
              FROM geovision_evidence_clips
             WHERE episode_id = %s AND linked_evidence_id IS NULL
             ORDER BY event_time
            """,
            (episode_id,),
        )

    # -- reset --------------------------------------------------------------
    def clear_edge_state(self) -> dict[str, int]:
        """Transient perception cache only.

        geovision_track_updates is the CURRENT position of each camera track -
        it is rebuilt within a second of the camera restarting, so clearing it
        between demo runs loses nothing. The raw event log, the bindings, the
        RFID taps and every mapped property, service zone, entrance, frontage
        and past collection event are deliberately NOT touched: they are the
        audit trail and the survey, and a demo restart is not a reason to lose
        either.
        """
        before = (fetch_one("SELECT count(*) AS n FROM geovision_track_updates")
                  or {}).get("n", 0)
        execute("DELETE FROM geovision_track_updates")
        after = (fetch_one("SELECT count(*) AS n FROM geovision_track_updates")
                 or {}).get("n", 0)
        return {"track_rows_deleted": int(before) - int(after),
                "track_rows_remaining": int(after)}


def record_mirror_result(episode_id: str, action: str, ok: bool,
                         error: str | None) -> None:
    """Standalone mirror callback -> collection_episodes.mirror_status.

    Not wired by default: EpisodeEngine attaches its OWN callback so the
    status is written through the engine's store, which is what makes the
    behaviour identical under test and in the live process. Kept for a mirror
    used without an engine (a diagnostic script, a replay tool).
    """
    if action == "PUBLISH":
        status = "MIRRORED" if ok else "MIRROR_FAILED"
    else:
        status = "REMOVED" if ok else "REMOVE_FAILED"
    try:
        EpisodeStore().set_mirror_status(episode_id, status, error)
    except Exception:  # noqa: BLE001
        pass
