"""Data quality checks in one aggregate scan: unique, not null, accepted values, range, freshness, row count.

The checks become a single SELECT of counters, so a table is read once, and that SELECT goes through
the same budget gate as every other query.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .core import UsageError
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

@dataclass(frozen=True)
class CheckConfig:
    name: str
    target: str
    spec: CheckSpec
    connection: str | None = None
    where: str | None = None
    samples: int = 0


KNOWN_CHECK_KEYS = frozenset({
    "target", "connection", "where", "unique", "not_null", "accepted",
    "range", "ranges", "fresh", "min_rows", "max_change_pct", "samples",
})


def _parse_bound(name: str, column: str, val: Any) -> float | None:
    from .config import ConfigError

    if val is None or val == "":
        return None
    if isinstance(val, bool):
        raise ConfigError(f"range for {column!r} needs numbers, got boolean {val!r}")
    try:
        num = float(val)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"range for {column!r} needs numbers, got {val!r}") from exc
    if not math.isfinite(num):
        raise ConfigError(f"range for {column!r} needs finite numbers, got {val!r}")
    return num


def parse_check_config(name: str, data: dict[str, Any]) -> CheckConfig:
    from .config import ConfigError

    unknown = set(data) - KNOWN_CHECK_KEYS
    if unknown:
        raise ConfigError(f"Unknown check keys in [checks.{name}]: {', '.join(sorted(unknown))}")

    raw_target = data.get("target", name)
    if not isinstance(raw_target, str) or not raw_target.strip():
        raise ConfigError(f"[checks.{name}] target must be a non-empty string, got {raw_target!r}")
    target = raw_target.strip()

    raw_conn = data.get("connection")
    if raw_conn is not None and not isinstance(raw_conn, str):
        raise ConfigError(f"[checks.{name}] connection must be a string, got {raw_conn!r}")
    connection = raw_conn.strip() if raw_conn else None

    raw_where = data.get("where")
    if raw_where is not None and not isinstance(raw_where, str):
        raise ConfigError(f"[checks.{name}] where must be a string, got {raw_where!r}")
    where = raw_where.strip() if raw_where else None

    raw_samples = data.get("samples", 0)
    if isinstance(raw_samples, bool) or not isinstance(raw_samples, int) or not (0 <= raw_samples <= 20):
        raise ConfigError(f"[checks.{name}] samples must be an integer from 0 to 20, got {raw_samples!r}")
    samples = raw_samples

    raw_min_rows = data.get("min_rows")
    if raw_min_rows is not None and (
        isinstance(raw_min_rows, bool) or not isinstance(raw_min_rows, int) or raw_min_rows < 0
    ):
        raise ConfigError(f"[checks.{name}] min_rows must be a non-negative integer, got {raw_min_rows!r}")
    min_rows = raw_min_rows

    raw_max_change = data.get("max_change_pct")
    if raw_max_change is not None:
        if (
            isinstance(raw_max_change, bool)
            or not isinstance(raw_max_change, (int, float))
            or raw_max_change < 0
            or not math.isfinite(raw_max_change)
        ):
            raise ConfigError(f"[checks.{name}] max_change_pct must be a non-negative number, got {raw_max_change!r}")
        max_change_pct = float(raw_max_change)
    else:
        max_change_pct = None

    raw_unique = data.get("unique")
    unique_keys: list[list[str]] = []
    if raw_unique is not None:
        if isinstance(raw_unique, str):
            raw_unique = [raw_unique]
        if not isinstance(raw_unique, list):
            raise ConfigError(f"[checks.{name}] unique must be a list or string, got {type(raw_unique).__name__}")
        for u in raw_unique:
            if isinstance(u, str):
                cols = [c.strip() for c in u.split(",") if c.strip()]
                if not cols:
                    raise ConfigError(f"[checks.{name}] unique column cannot be empty")
                unique_keys.append(cols)
            elif isinstance(u, (list, tuple)):
                cols = [str(c).strip() for c in u if str(c).strip()]
                if not cols:
                    raise ConfigError(f"[checks.{name}] composite unique key cannot be empty")
                unique_keys.append(cols)
            else:
                raise ConfigError(f"[checks.{name}] unique key entry must be string or list of strings, got {u!r}")

    raw_not_null = data.get("not_null")
    not_null_cols: list[str] = []
    if raw_not_null is not None:
        if isinstance(raw_not_null, str):
            raw_not_null = [raw_not_null]
        if not isinstance(raw_not_null, list):
            raise ConfigError(f"[checks.{name}] not_null must be a list or string, got {type(raw_not_null).__name__}")
        for n in raw_not_null:
            if not isinstance(n, str):
                raise ConfigError(f"[checks.{name}] not_null column must be a string, got {n!r}")
            cols = [c.strip() for c in n.split(",") if c.strip()]
            not_null_cols.extend(cols)

    raw_accepted = data.get("accepted")
    accepted_dict: dict[str, list[str]] = {}
    if raw_accepted is not None:
        if isinstance(raw_accepted, dict):
            for col, vals in raw_accepted.items():
                if isinstance(vals, str):
                    accepted_dict[col.strip()] = [v for v in vals.split("|")]
                elif isinstance(vals, (list, tuple)):
                    if not vals:
                        raise ConfigError(f"[checks.{name}] accepted values list for {col!r} cannot be empty")
                    accepted_dict[col.strip()] = [str(v) for v in vals]
                else:
                    raise ConfigError(f"[checks.{name}] accepted values for {col!r} must be list or string, got {vals!r}")
        elif isinstance(raw_accepted, list):
            for item in raw_accepted:
                if not isinstance(item, str) or "=" not in item:
                    raise ConfigError(f"[checks.{name}] accepted list entry must be 'col=a|b', got {item!r}")
                col, _, vals = item.partition("=")
                accepted_dict[col.strip()] = [v for v in vals.split("|")]
        else:
            raise ConfigError(f"[checks.{name}] accepted must be a table or list, got {type(raw_accepted).__name__}")

    if "range" in data and "ranges" in data:
        raise ConfigError(f"[checks.{name}] use either 'range' or 'ranges', not both")
    raw_range = data.get("range") if "range" in data else data.get("ranges")
    ranges_dict: dict[str, tuple[float | None, float | None]] = {}
    if raw_range is not None:
        if isinstance(raw_range, dict):
            for col, bounds in raw_range.items():
                column = col.strip()
                lo: float | None = None
                hi: float | None = None
                if isinstance(bounds, (list, tuple)):
                    if len(bounds) == 1:
                        lo = _parse_bound(name, column, bounds[0])
                    elif len(bounds) == 2:
                        lo = _parse_bound(name, column, bounds[0])
                        hi = _parse_bound(name, column, bounds[1])
                    else:
                        raise ConfigError(f"[checks.{name}] range for {column!r} needs [min, max], got {bounds!r}")
                elif isinstance(bounds, dict):
                    unknown_b = set(bounds) - {"min", "max"}
                    if unknown_b:
                        raise ConfigError(
                            f"[checks.{name}] range bounds for {column!r} only take 'min' and 'max', got {unknown_b}"
                        )
                    if "min" in bounds:
                        lo = _parse_bound(name, column, bounds["min"])
                    if "max" in bounds:
                        hi = _parse_bound(name, column, bounds["max"])
                elif isinstance(bounds, str):
                    if ":" not in bounds:
                        raise ConfigError(f"[checks.{name}] range for {column!r} string must be 'min:max', got {bounds!r}")
                    lo_text, _, hi_text = bounds.partition(":")
                    lo = _parse_bound(name, column, lo_text) if lo_text.strip() else None
                    hi = _parse_bound(name, column, hi_text) if hi_text.strip() else None
                else:
                    raise ConfigError(f"[checks.{name}] range for {column!r} must be [min, max] or table, got {bounds!r}")
                if lo is None and hi is None:
                    raise ConfigError(f"[checks.{name}] range for {column!r} must specify at least one bound")
                if lo is not None and hi is not None and lo > hi:
                    raise ConfigError(f"range for {column!r} has min {lo} > max {hi}")
                ranges_dict[column] = (lo, hi)
        elif isinstance(raw_range, list):
            for r in raw_range:
                if not isinstance(r, str) or "=" not in r:
                    raise ConfigError(f"[checks.{name}] range list entry must be 'col=min:max', got {r!r}")
                col, _, bounds = r.partition("=")
                column = col.strip()
                if ":" not in bounds:
                    raise ConfigError(f"[checks.{name}] range bounds must be 'min:max', got {bounds!r}")
                lo_text, _, hi_text = bounds.partition(":")
                lo = _parse_bound(name, column, lo_text) if lo_text.strip() else None
                hi = _parse_bound(name, column, hi_text) if hi_text.strip() else None
                if lo is not None and hi is not None and lo > hi:
                    raise ConfigError(f"range for {column!r} has min {lo} > max {hi}")
                ranges_dict[column] = (lo, hi)
        else:
            raise ConfigError(f"[checks.{name}] range must be a table or list, got {type(raw_range).__name__}")

    raw_fresh = data.get("fresh")
    fresh_dict: dict[str, str] = {}
    if raw_fresh is not None:
        if isinstance(raw_fresh, dict):
            for col, age in raw_fresh.items():
                if not isinstance(age, str):
                    raise ConfigError(
                        f"[checks.{name}] freshness age for {col!r} must be a string (e.g. '24h'), got {age!r}"
                    )
                parse_period(age)
                fresh_dict[col.strip()] = age.strip()
        elif isinstance(raw_fresh, list):
            for f in raw_fresh:
                if not isinstance(f, str) or "=" not in f:
                    raise ConfigError(f"[checks.{name}] fresh list entry must be 'col=age', got {f!r}")
                col, _, age = f.partition("=")
                parse_period(age)
                fresh_dict[col.strip()] = age.strip()
        else:
            raise ConfigError(f"[checks.{name}] fresh must be a table or list, got {type(raw_fresh).__name__}")

    spec = CheckSpec(
        unique=unique_keys,
        not_null=not_null_cols,
        accepted=accepted_dict,
        ranges=ranges_dict,
        fresh=fresh_dict,
        min_rows=min_rows,
        max_change_pct=max_change_pct,
    )
    if spec.is_empty():
        raise ConfigError(
            f"[checks.{name}] has no checks configured; give at least one check "
            "(unique, not_null, accepted, range, fresh, min_rows, max_change_pct)"
        )
    return CheckConfig(
        name=name,
        target=target,
        spec=spec,
        connection=connection,
        where=where,
        samples=samples,
    )


def merge_specs(base: CheckSpec, override: CheckSpec) -> CheckSpec:
    """Merge CLI overrides into a base CheckSpec. CLI flags add to or override base."""
    unique = [list(u) for u in base.unique]
    for u in override.unique:
        if u not in unique:
            unique.append(list(u))
    not_null = list(base.not_null)
    for n in override.not_null:
        if n not in not_null:
            not_null.append(n)
    accepted = {k: list(v) for k, v in base.accepted.items()}
    accepted.update({k: list(v) for k, v in override.accepted.items()})
    ranges = dict(base.ranges)
    ranges.update(override.ranges)
    fresh = dict(base.fresh)
    fresh.update(override.fresh)
    min_rows = override.min_rows if override.min_rows is not None else base.min_rows
    max_change_pct = override.max_change_pct if override.max_change_pct is not None else base.max_change_pct
    return CheckSpec(
        unique=unique,
        not_null=not_null,
        accepted=accepted,
        ranges=ranges,
        fresh=fresh,
        min_rows=min_rows,
        max_change_pct=max_change_pct,
    )



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
            raise UsageError(f"--accepted needs col=a|b|c, got '{a}'",
                             "Pass --accepted col=a|b|c, e.g. --accepted status=new|paid.")
        spec.accepted[col.strip()] = [v for v in values.split("|")]
    for r in ranges or []:
        col, sep, bounds = r.partition("=")
        if not sep or ":" not in bounds:
            raise UsageError(f"--range needs col=min:max (either side may be empty), got '{r}'",
                             "Pass --range col=min:max, e.g. --range amount=0:1000.")
        column = col.strip()
        lo_text, _, hi_text = bounds.partition(":")
        lo = _range_bound(column, lo_text) if lo_text.strip() else None
        hi = _range_bound(column, hi_text) if hi_text.strip() else None
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"range for {column!r} has min {lo_text.strip()} > max {hi_text.strip()}")
        spec.ranges[column] = (lo, hi)
    for f in fresh or []:
        col, sep, age = f.partition("=")
        example = "Pass --fresh col=age, e.g. --fresh ts=24h."
        if not sep or not age.strip():
            raise UsageError(f"--fresh needs col=age, got '{f}'", example)
        try:
            parse_period(age)  # validate early
        except ValueError as exc:
            raise UsageError(f"--fresh: {exc}", example) from exc
        spec.fresh[col.strip()] = age.strip()
    return spec


def _range_bound(column: str, text: str) -> float:
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"range for {column!r} needs numbers, got {text.strip()!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"range for {column!r} needs finite numbers, got {text.strip()!r}")
    return number


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
            text = " || '|' || ".join(engine.text_part(q(c)) for c in key)
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
    conditions = [f"({where}\n)"] if where else []
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
