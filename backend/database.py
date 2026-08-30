"""PostgreSQL access for the collector / RFID CRUD.

Two things changed from the original and both matter:

* **No credentials in source.** Everything comes from ``config``, which
  reads the environment or ``backend/.env``.
* **No connection at import time.** The module used to open a socket while
  being imported, which meant importing anything that touched it -- the API,
  a test, a script -- failed outright without a database. Connections are
  now opened on demand and closed by the caller.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Optional

import config

logger = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """Raised when a connection cannot be established or configured."""


def _connection_kwargs() -> dict:
    if config.DB_PASSWORD is None:
        raise DatabaseUnavailable(
            "GEOVISION_DB_PASSWORD is not set. Copy backend/.env.example to "
            "backend/.env and fill it in."
        )
    return {
        "host": config.DB_HOST,
        "port": config.DB_PORT,
        "database": config.DB_NAME,
        "user": config.DB_USER,
        "password": config.DB_PASSWORD,
    }


def get_db_connection():
    """Open a new connection. The caller is responsible for closing it."""

    try:
        import psycopg2
    except Exception as exc:  # pragma: no cover - depends on environment
        raise DatabaseUnavailable(f"psycopg2 is not available: {exc}") from exc

    try:
        return psycopg2.connect(**_connection_kwargs())
    except Exception as exc:
        raise DatabaseUnavailable(f"Could not connect to PostgreSQL: {exc}") from exc


@contextmanager
def db_cursor(commit: bool = False):
    """Connection + cursor with guaranteed cleanup.

    ``commit=True`` commits on success and rolls back on any exception.
    """

    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        yield cursor
        if commit:
            connection.commit()
    except Exception:
        if commit:
            connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def check_connection() -> dict:
    """Health probe. Never raises -- reports."""

    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"connected": True, "error": None}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# RFID -> collector
# ---------------------------------------------------------------------------


def collector_for_rfid(rfid_id: str) -> Optional[str]:
    """Resolve an RFID tag to its assigned collector, or ``None``.

    This is the identity half of RFID binding: the vision layer receives it
    as an injected callable so it never imports this module.
    """

    if not rfid_id:
        return None

    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT collector_id
                FROM rfids
                WHERE rfid_id = %s
                  AND status = 'ASSIGNED'
                  AND collector_id IS NOT NULL
                """,
                (rfid_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None
    except DatabaseUnavailable as exc:
        logger.warning("RFID lookup unavailable: %s", exc)
        return None
    except Exception as exc:
        logger.error("RFID lookup failed for %s: %s", rfid_id, exc)
        return None
