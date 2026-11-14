"""Create a small 'giant' DuckDB database to try Gulliver without any warehouse."""

from __future__ import annotations

import os
from pathlib import Path

from .core import _error

MAX_EVENTS = 50_000_000


def build_demo(path: str | os.PathLike[str] = "gulliver_demo.duckdb", events: int = 5_000_000,
               users: int = 100_000, overwrite: bool = False) -> dict:
    if isinstance(events, bool) or not isinstance(events, int) or events < 1:
        return _error(
            f"--events must be at least 1, got {events}.",
            f"Pass an event count between 1 and {MAX_EVENTS:,} (for example --events 5000000).",
        )
    if events > MAX_EVENTS:
        return _error(
            f"--events cannot exceed {MAX_EVENTS:,}, got {events:,}.",
            f"Pass an event count between 1 and {MAX_EVENTS:,} (default 5,000,000).",
        )

    import duckdb

    target = Path(path)
    if target.exists():
        if not overwrite:
            return {"status": "ok", "path": str(target), "created": False,
                    "next_step": f"Already exists. Try: gulliver --dsn {target} inspect"}
        target.unlink()
    con = duckdb.connect(str(target))
    try:
        con.execute(f"""
            CREATE TABLE users AS
            SELECT i AS user_id,
                   ['NL', 'BE', 'DE', 'FR', 'US', 'GB', 'ES', 'IT'][1 + (i % 8)] AS country,
                   DATE '2020-01-01' + CAST(i % 1500 AS INTEGER) AS signup_date
            FROM range({int(users)}) t(i)
        """)
        con.execute(f"""
            CREATE TABLE events AS
            SELECT i AS event_id,
                   CAST(hash(i) % {int(users)} AS BIGINT) AS user_id,
                   ['view', 'click', 'cart', 'buy'][1 + CAST(hash(i * 7) % 4 AS INTEGER)] AS event_type,
                   round((hash(i * 13) % 10000) / 100.0, 2) AS amount,
                   TIMESTAMP '2025-01-01' + to_seconds(CAST(i % 31536000 AS BIGINT)) AS ts
            FROM range({int(events)}) t(i)
        """)
    finally:
        con.close()
    return {"status": "ok", "path": str(target), "created": True, "tables": {"events": events, "users": users},
            "next_step": f"Try: gulliver --dsn {target} inspect"}
