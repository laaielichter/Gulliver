"""Data-diff: do two queries (or tables) return the same rows?

Works inside the engines, so it scales: each side is reduced to N buckets of (row count, sum of row hashes),
keyed by a hash of the primary key. Only buckets that differ are opened up to list the keys that differ.
The hash is MD5 on every engine, so a DuckDB side can be compared with a BigQuery or Snowflake side
(values are compared as text: cast or round in your queries when engines format numbers differently).
"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from .engines.base import Engine

DEFAULT_BUCKETS = 256


def row_text(engine: Engine, columns: list[str]) -> str:
    parts = [engine.text_part(engine.quote(c)) for c in columns]
    return " || '|' || ".join(parts) if parts else "''"


def bucket_sql(engine: Engine, query: str, key: list[str], columns: list[str], buckets: int,
               within: tuple[int, list[int]] | None = None) -> str:
    """Count and hash-sum per bucket. `within=(parent_modulus, parent_buckets)` limits the scan to rows of
    those coarser buckets, so a second, finer pass (buckets = parent_modulus * k) can zoom in."""
    key_hash = engine.md5_int(row_text(engine, key))
    where = ""
    if within:
        parent, values = within
        where = f" WHERE MOD({key_hash}, {int(parent)}) IN ({', '.join(str(int(v)) for v in values) or '-1'})"
    return (
        f"SELECT gv_bucket, COUNT(*) AS gv_n, SUM(gv_h) AS gv_hsum FROM ("
        f"SELECT MOD({key_hash}, {int(buckets)}) AS gv_bucket, "
        f"{engine.md5_int(row_text(engine, columns))} AS gv_h FROM ({query}) AS gv_q{where}"
        f") AS gv_b GROUP BY gv_bucket"
    )


def keys_sql(engine: Engine, query: str, key: list[str], columns: list[str], buckets: int,
             wanted: list[int], limit: int) -> str:
    key_text = row_text(engine, key)
    in_list = ", ".join(str(int(b)) for b in wanted) or "-1"
    return (
        f"SELECT {key_text} AS gv_key, {engine.md5_int(row_text(engine, columns))} AS gv_h "
        f"FROM ({query}) AS gv_q WHERE MOD({engine.md5_int(key_text)}, {int(buckets)}) IN ({in_list}) "
        f"LIMIT {int(limit)}"
    )


def plan_drill(summary_left: list[dict[str, Any]], summary_right: list[dict[str, Any]], differing: list[int],
               row_budget: int, buckets: int) -> tuple[list[int], int, bool]:
    """Pick differing buckets whose rows fit the row budget. Returns (buckets, modulus, complete)."""
    sizes: dict[int, int] = {}
    for rows in (summary_left, summary_right):
        for r in rows:
            b = int(r["gv_bucket"])
            sizes[b] = max(sizes.get(b, 0), int(r["gv_n"]))
    chosen: list[int] = []
    used = 0
    for b in differing:
        size = sizes.get(b, 0)
        if chosen and used + size > row_budget:
            break
        if size > row_budget:
            break
        chosen.append(b)
        used += size
    return chosen, buckets, len(chosen) == len(differing)


def compare_buckets(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    lmap = {int(r["gv_bucket"]): (int(r["gv_n"]), int(r["gv_hsum"])) for r in left}
    rmap = {int(r["gv_bucket"]): (int(r["gv_n"]), int(r["gv_hsum"])) for r in right}
    differing = sorted(b for b in set(lmap) | set(rmap) if lmap.get(b) != rmap.get(b))
    return {
        "left_rows": sum(n for n, _ in lmap.values()),
        "right_rows": sum(n for n, _ in rmap.values()),
        "buckets_compared": len(set(lmap) | set(rmap)),
        "buckets_different": differing,
    }


def compare_keys(left: list[dict[str, Any]], right: list[dict[str, Any]], max_keys: int) -> dict[str, Any]:
    lk: dict[str, list[int]] = {}
    rk: dict[str, list[int]] = {}
    for row in left:
        lk.setdefault(str(row["gv_key"]), []).append(int(row["gv_h"]))
    for row in right:
        rk.setdefault(str(row["gv_key"]), []).append(int(row["gv_h"]))
    only_left = sorted(set(lk) - set(rk))
    only_right = sorted(set(rk) - set(lk))
    changed = sorted(k for k in set(lk) & set(rk) if sorted(lk[k]) != sorted(rk[k]))
    duplicates = sorted(k for k in set(lk) | set(rk) if len(lk.get(k, [])) > 1 or len(rk.get(k, [])) > 1)
    return {
        "only_in_left": {"count": len(only_left), "sample": only_left[:max_keys]},
        "only_in_right": {"count": len(only_right), "sample": only_right[:max_keys]},
        "changed": {"count": len(changed), "sample": changed[:max_keys]},
        "duplicate_keys": {"count": len(duplicates), "sample": duplicates[:max_keys]},
    }


def translate(sql: str, source: str, target: str) -> dict[str, Any]:
    """Transpile SQL between dialects with sqlglot; unsupported constructs come back as warnings."""
    import logging

    warnings: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warnings.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    logger = logging.getLogger("sqlglot")
    logger.addHandler(handler)
    try:
        out = sqlglot.transpile(sql, read=source, write=target, pretty=True,
                                unsupported_level=sqlglot.ErrorLevel.WARN)
    finally:
        logger.removeHandler(handler)
    statements = [s for s in out if s.strip()]
    unknown = sorted({str(n.name).upper() for n in _dialect_specific(sql, source)})
    if unknown:
        warnings.append("Functions not known to the translator, copied as-is (check them by hand): "
                        + ", ".join(unknown))
    return {"sql": ";\n\n".join(statements), "statements": len(statements), "warnings": sorted(set(warnings)),
            "unknown_functions": unknown}


def _dialect_specific(sql: str, dialect: str) -> list[exp.Expression]:
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return []
    return [n for t in trees if t is not None for n in t.find_all(exp.Anonymous)]
