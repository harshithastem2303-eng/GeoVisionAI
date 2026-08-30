"""Garbage-collector and RFID-card CRUD.

Preserved from the original implementation -- this is working functionality
the frontend depends on. The SQL is unchanged; what changed is that
connections come from :func:`database.db_cursor` (env credentials, closed
deterministically) instead of a module-level connection opened at import.

Signatures stay query-parameter based because the existing dashboard calls
them that way, and redesigning the frontend is out of scope.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from database import DatabaseUnavailable, db_cursor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["collectors"])


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/collectors")
def get_collectors():
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT gc.collector_id, gc.name, gc.phone, gc.area, gc.status,
                       r.rfid_id
                FROM garbage_collectors gc
                LEFT JOIN rfids r
                       ON r.collector_id = gc.collector_id
                      AND r.status = 'ASSIGNED'
                ORDER BY gc.collector_id
                """
            )
            rows = cursor.fetchall()
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("COLLECTORS ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [
        {
            "id": row[0],
            "name": row[1],
            "phone": row[2],
            "area": row[3],
            "status": row[4],
            "rfid": row[5],
        }
        for row in rows
    ]


@router.post("/collectors")
def create_collector(
    collector_id: str,
    name: str,
    phone: str = "",
    area: str = "",
    status: str = "Active",
):
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO garbage_collectors
                    (collector_id, name, phone, area, status)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING collector_id, name, phone, area, status
                """,
                (collector_id, name, phone, area, status),
            )
            row = cursor.fetchone()
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("CREATE COLLECTOR ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "id": row[0],
        "name": row[1],
        "phone": row[2],
        "area": row[3],
        "status": row[4],
        "rfid": None,
    }


@router.get("/collectors/{collector_id}")
def get_collector(collector_id: str):
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT gc.collector_id, gc.name, gc.phone, gc.area, gc.status,
                       r.rfid_id,
                       COALESCE(r.waste_state, 'SEGREGATED') AS waste_state
                FROM garbage_collectors gc
                LEFT JOIN rfids r
                       ON r.collector_id = gc.collector_id
                      AND r.status = 'ASSIGNED'
                WHERE gc.collector_id = %s
                """,
                (collector_id,),
            )
            row = cursor.fetchone()
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("COLLECTOR ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if row is None:
        raise HTTPException(status_code=404, detail="Garbage collector not found")

    return {
        "id": row[0],
        "name": row[1],
        "phone": row[2],
        "area": row[3],
        "status": row[4],
        "rfid": row[5],
        "waste_state": row[6],
    }


@router.get("/rfids")
def get_rfids():
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT rfid_id, collector_id, status, waste_state, assigned_at
                FROM rfids
                ORDER BY rfid_id
                """
            )
            rows = cursor.fetchall()
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("RFIDS ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return [
        {
            "rfid_id": row[0],
            "collector_id": row[1],
            "status": row[2],
            "waste_state": row[3],
            "assigned_at": str(row[4]) if row[4] else None,
        }
        for row in rows
    ]


@router.post("/rfids/assign")
def assign_rfid(collector_id: str, rfid_id: str, register_if_unknown: bool = False):
    """Assign a card to a collector.

    ``register_if_unknown`` exists for the enrolment flow, where the UID comes
    off a physical card the RC522 has just read and therefore cannot already
    be a row. It defaults to **False**, so every pre-existing caller -- the
    manual-entry path in the dashboard included -- keeps the original
    behaviour of 404-ing on an unknown ``rfid_id``, and a typo can still not
    conjure a card into existence. Only the explicit "Scan RFID Card" modal
    passes it.

    Registration is idempotent and happens inside the same transaction as the
    assignment: a card is created ``AVAILABLE`` and then goes through the
    identical ownership checks below, so a UID that is already held by
    someone else still yields the 409 rather than being quietly re-created.
    """

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT collector_id FROM garbage_collectors WHERE collector_id = %s",
                (collector_id,),
            )
            if cursor.fetchone() is None:
                raise HTTPException(
                    status_code=404, detail="Garbage collector not found"
                )

            if register_if_unknown:
                # Insert only when genuinely absent. Written as a guarded
                # INSERT rather than ON CONFLICT so it does not depend on a
                # particular unique-constraint name, and so an existing card
                # is left exactly as it is -- its current owner survives to be
                # checked below.
                cursor.execute(
                    "SELECT 1 FROM rfids WHERE rfid_id = %s",
                    (rfid_id,),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        """
                        INSERT INTO rfids (rfid_id, status, waste_state)
                        VALUES (%s, 'AVAILABLE', 'SEGREGATED')
                        """,
                        (rfid_id,),
                    )

            cursor.execute(
                """
                SELECT rfid_id, collector_id, status
                FROM rfids
                WHERE rfid_id = %s
                FOR UPDATE
                """,
                (rfid_id,),
            )
            rfid = cursor.fetchone()
            if rfid is None:
                raise HTTPException(status_code=404, detail="RFID not found")
            if rfid[1] is not None and rfid[1] != collector_id:
                raise HTTPException(
                    status_code=409,
                    detail="RFID is already assigned to another collector",
                )

            # Release any other card this collector holds.
            cursor.execute(
                """
                UPDATE rfids
                SET collector_id = NULL, status = 'AVAILABLE'
                WHERE collector_id = %s AND rfid_id <> %s
                """,
                (collector_id, rfid_id),
            )

            cursor.execute(
                """
                UPDATE rfids
                SET collector_id = %s,
                    status = 'ASSIGNED',
                    waste_state = 'SEGREGATED',
                    assigned_at = CURRENT_TIMESTAMP
                WHERE rfid_id = %s
                RETURNING rfid_id, collector_id, status, waste_state, assigned_at
                """,
                (collector_id, rfid_id),
            )
            updated = cursor.fetchone()
    except HTTPException:
        raise
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("RFID ASSIGN ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "success": True,
        "rfid_id": updated[0],
        "collector_id": updated[1],
        "status": updated[2],
        "waste_state": updated[3],
        "assigned_at": str(updated[4]),
    }


@router.put("/rfids/{rfid_id}/state")
def update_rfid_state(rfid_id: str, waste_state: str):
    allowed = {"SEGREGATED", "NON_SEGREGATED"}
    waste_state = waste_state.upper()
    if waste_state not in allowed:
        raise HTTPException(
            status_code=400,
            detail="waste_state must be SEGREGATED or NON_SEGREGATED",
        )

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE rfids
                SET waste_state = %s
                WHERE rfid_id = %s
                RETURNING rfid_id, collector_id, status, waste_state
                """,
                (waste_state, rfid_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="RFID not found")
    except HTTPException:
        raise
    except DatabaseUnavailable as exc:
        raise _unavailable(exc) from exc
    except Exception as exc:
        logger.error("RFID STATE ERROR: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "success": True,
        "rfid_id": row[0],
        "collector_id": row[1],
        "status": row[2],
        "waste_state": row[3],
    }
