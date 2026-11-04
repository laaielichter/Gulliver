"""Data quality checks in one aggregate scan: unique, not null, accepted values, range, freshness, row count.

The checks become a single SELECT of counters, so a table is read once, and that SELECT goes through
the same budget gate as every other query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .engines.base import Engine
from .incremental import parse_period


@dataclass
class CheckSpec:
    unique: list[list[str]] = field(default_factory=list)       # each entry is a (composite) key
    not_null: list[str] = field(default_factory=list)
    accepted: dict[str, list[str]] = field(default_factory=dict)
    ranges: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    fresh: dict[str, str] = field(default_factory=dict)         # column -> max age, e.g. "24h"
    min_rows: int | None = None
    max_change_pct: float | None = None

    def is_empty(self) -> bool:
        return not (self.unique or self.not_null or self.accepted or self.ranges or self.fresh
                    or self.min_rows is not None or self.max_change_pct is not None)


def parse_spec(unique: list[str] | None = None, not_null: list[str] | None = None,
               accepted: list[str] | None = None, ranges: list[str] | None = None,
               fresh: list[str] | None = None, min_rows: int | None = None,
               max_change_pct: float | None = None) -> CheckSpec:
    """Parse CLI-style strings: unique 'id' or 'a,b'; accepted 'status=new|paid'; range 'amount=0:1000';
    fresh 'ts=24h'."""
    spec = CheckSpec(min_rows=min_rows, max_change_pct=max_change_pct)
    for u in unique or []:
        spec.unique.append([c.strip() for c in u.split(",") if c.strip()])
    for n in not_null or []:
        spec.not_null.extend(c.strip() for c in n.split(",") if c.strip())
    for a in accepted or []:
        col, _, values = a.partition("=")
        if not values:
            raise ValueError(f"accepted needs col=a|b|c, got '{a}'")
        spec.accepted[col.strip()] = [v for v in values.split("|")]
    for r in ranges or []:
        col, _, bounds = r.partition("=")
        lo, _, hi = bounds.partition(":")
        if not _:
            raise ValueError(f"range needs col=min:max (either side may be empty), got '{r}'")
        spec.ranges[col.strip()] = (float(lo) if lo.strip() else None, float(hi) if hi.strip() else None)
    for f in fresh or []:
        col, _, age = f.partition("=")
        parse_period(age)  # validate early
        spec.fresh[col.strip()] = age.strip()
    return spec


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_sql(engine: Engine, source: str, spec: CheckSpec, where: str | None = None) -> tuple[str, list[dict]]:
    """Return (sql, plan). plan maps each output column of the SQL to the check it serves."""
    q = engine.quote
    parts = ["COUNT(*) AS gv_rows"]
    plan: list[dict[str, Any]] = [{"check": "row_count", "column": None, "alias": "gv_rows"}]
    i = 0

    def add(expr: str, check: str, column: str | None, extra: dict | None = None) -> str:
        nonlocal i
        alias = f"gv_c{i}"
        i += 1
        parts.append(f"{expr} AS {alias}")
        plan.append({"check": check, "column": column, "alias": alias, **(extra or {})})
        return alias

    for key in spec.unique:
        if len(key) == 1:
            col = q(key[0])
            add(f"COUNT({col}) - COUNT(DISTINCT {col})", "unique", key[0])
        else:
            text = " || '|' || ".join(engine.text(q(c)) for c in key)
            add(f"COUNT(*) - COUNT(DISTINCT {text})", "unique", ",".join(key))
    for col in spec.not_null:
        add(f"SUM(CASE WHEN {q(col)} IS NULL THEN 1 ELSE 0 END)", "not_null", col)
    for col, values in spec.accepted.items():
        in_list = ", ".join(_lit(v) for v in values)
        add(f"SUM(CASE WHEN {q(col)} IS NOT NULL AND CAST({q(col)} AS {engine.text_type}) NOT IN ({in_list}) "
            "THEN 1 ELSE 0 END)", "accepted_values", col, {"values": values})
    for col, (lo, hi) in spec.ranges.items():
        conds = []
        if lo is not None:
            conds.append(f"{q(col)} < {lo}")
        if hi is not None:
            conds.append(f"{q(col)} > {hi}")
        add(f"SUM(CASE WHEN {' OR '.join(conds) or 'FALSE'} THEN 1 ELSE 0 END)", "range", col,
            {"min": lo, "max": hi})
    for col, age in spec.fresh.items():
        add(f"MAX({q(col)})", "freshness", col, {"max_age": age})
    sql = f"SELECT {', '.join(parts)} FROM {source}"
    if where:
        sql += f" WHERE {where}"
    return sql, plan


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sample_sql(engine: Engine, source: str, spec: CheckSpec, outcome: dict[str, Any],
               limit: int, where: str | None = None) -> tuple[str, str] | None:
    """Group offending values, keeping the aggregate check's NULL and filter semantics.

    Return the SQL and a count alias that cannot collide with a key column.
    Composite grouping deliberately keeps the actual columns rather than serializing keys.
    """
    q = engine.quote
    kind = outcome["check"]
    column = outcome.get("column")
    conditions = [f"({where})"] if where else []
    having = ""
    if kind == "unique":
        columns = next(key for key in spec.unique if ",".join(key) == column)
        if len(columns) == 1:
            conditions.append(f"{q(columns[0])} IS NOT NULL")
        having = " HAVING COUNT(*) > 1"
    elif kind == "accepted_values":
        columns = [column]
        values = ", ".join(_lit(v) for v in spec.accepted[column])
        conditions.append(f"{q(column)} IS NOT NULL AND CAST({q(column)} AS {engine.text_type}) NOT IN ({values})")
    elif kind == "range":
        columns = [column]
        lo, hi = spec.ranges[column]
        bounds = []
        if lo is not None:
            bounds.append(f"{q(column)} < {lo}")
        if hi is not None:
            bounds.append(f"{q(column)} > {hi}")
        conditions.append("(" + (" OR ".join(bounds) or "FALSE") + ")")
    else:
        return None
    alias = "gv_sample_count"
    while alias.lower() in {c.lower() for c in columns}:
        alias += "_"
    keys = ", ".join(q(c) for c in columns)
    sql = f"SELECT {keys}, COUNT(*) AS {q(alias)} FROM {source}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" GROUP BY {keys}{having} ORDER BY {q(alias)} DESC LIMIT {limit}"
    return sql, alias


def evaluate(row: dict[str, Any], plan: list[dict], spec: CheckSpec, previous_rows: int | None,
             now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    total = int(row.get("gv_rows") or 0)
    for item in plan:
        value = row.get(item["alias"])
        check = item["check"]
        out: dict[str, Any] = {"check": check}
        if item["column"]:
            out["column"] = item["column"]
        if check == "row_count":
            out["rows"] = total
            ok = True
            if spec.min_rows is not None:
                ok = total >= spec.min_rows
                out["min_rows"] = spec.min_rows
            if previous_rows is not None:
                out["previous_rows"] = previous_rows
                if previous_rows > 0:
                    change = (total - previous_rows) / previous_rows * 100
                    out["change_pct"] = round(change, 2)
                    if spec.max_change_pct is not None and abs(change) > spec.max_change_pct:
                        ok = False
                        out["max_change_pct"] = spec.max_change_pct
            no_row_rule = spec.min_rows is None and (spec.max_change_pct is None or previous_rows is None)
            if no_row_rule and total == 0:
                ok = False
                out["message"] = "The table or query returned no rows."
            out["passed"] = ok
        elif check in ("unique", "not_null", "accepted_values", "range"):
            bad = int(value or 0)
            out["failing_rows"] = bad
            out["passed"] = bad == 0
            for key in ("values", "min", "max"):
                if key in item:
                    out[key] = item[key]
        elif check == "freshness":
            latest = _as_utc(value)
            limit = parse_period(item["max_age"])
            out["latest"] = latest.isoformat() if latest else None
            out["max_age"] = item["max_age"]
            if latest is None:
                out["passed"] = False
                out["message"] = "No value (empty table, or the column is all NULL)."
            else:
                age = now - latest
                out["age_hours"] = round(age / timedelta(hours=1), 2)
                out["passed"] = age <= limit
        results.append(out)
    return results
