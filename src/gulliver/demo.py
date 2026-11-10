"""Create a small 'giant' DuckDB database to try Gulliver without any warehouse."""

from __future__ import annotations

import os
from pathlib import Path

# `--events 99999999999` used to be accepted and handed to DuckDB's range(), which
# happily starts generating rows until the disk fills. 5,000,000 events (the default)
# is 25 MB on this machine and 20,000,000 is 96 MB and 5 s, so 100,000,000 is about
# half a gigabyte and 25 s: still a demo, and still well inside any laptop that can
# run DuckDB at all.
MAX_EVENTS = 100_000_000


def build_demo(path: str | os.PathLike[str] = "gulliver_demo.duckdb", events: int = 5_000_000,
               users: int = 100_000, overwrite: bool = False) -> dict:
    import duckdb

    from .core import _error

    if events < 1:
        return _error(
            f"--events must be 1 or more (got {events}); a demo with no events cannot be inspected or rehearsed",
            next_step="Pass --events 1 or more, or leave it out to use the default of 5,000,000.",
        )
    if events > MAX_EVENTS:
        return _error(
            f"--events must be {MAX_EVENTS:,} or fewer (got {events:,}); that many rows would fill the disk "
            "before the demo could be used",
            next_step=f"Pass --events {MAX_EVENTS:,} or fewer, or use your own warehouse for data that large.",
        )

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
