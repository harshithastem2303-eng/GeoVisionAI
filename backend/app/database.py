"""Thin psycopg3 access layer.

Deliberately raw SQL: the interesting part of this demo IS the PostGIS
query, and hiding it behind an ORM would defeat the purpose.
"""

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings
from .tracing import sql_span

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=settings.dsn,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as conn:
        yield conn


# Each of the three below opens a LangSmith child span when tracing is on, so
# a traced request shows the exact PostGIS statements it ran and how many rows
# each returned. `sql_span` is a no-op context manager when tracing is off, so
# the untraced path is the same two lines it always was.
#
# Row COUNTS, never rows: /summary alone returns up to 100 joined event rows,
# and shipping those to a third party on every dashboard poll is not what
# anyone is asking for when they turn on tracing.


def fetch_all(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with sql_span("sql.fetch_all", sql, params) as span:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        span.end(outputs={"rows": len(rows)})
        return rows


def fetch_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    with sql_span("sql.fetch_one", sql, params) as span:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        span.end(outputs={"rows": 0 if row is None else 1})
        return row


def execute(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    """Execute a statement; returns the first row if the statement RETURNs."""
    with sql_span("sql.execute", sql, params) as span:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            conn.commit()
        span.end(outputs={"rows": 0 if row is None else 1, "returning": row is not None})
        return row


def healthcheck() -> dict[str, Any]:
    row = fetch_one("SELECT PostGIS_Version() AS postgis, version() AS pg;")
    return row or {}
