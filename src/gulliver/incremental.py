"""Incremental advisor: can a model that rebuilds everything on every run be made incremental?

Classifies the SELECT and generates the incremental version:
  append               no aggregation: only read new rows (merge on unique_key for late updates)
  partition_overwrite  grouped by a time grain: recompute only the recent periods (dbt microbatch)
  merge_recompute      grouped by an entity key: recompute only the keys that got new rows (merge)
  not_safe             LIMIT, global totals, windows over all rows, ... -> keep the full rebuild
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlglot import exp

from .lint import _has_aggregation, _outer_select
from .sqlsafety import base_tables, classify, table_ref

TIME_NAME = re.compile(r"(^|_)(ts|at|time|timestamp|date|day|dt|datetime)$|^(created|updated|event_time|loaded_at|"
                       r"inserted_at|modified)", re.I)
PERIOD_RE = re.compile(r"^\s*(\d+)\s*(m|min|minute|minutes|h|hour|hours|d|day|days|w|week|weeks)\s*$", re.I)


def parse_period(text: str) -> timedelta:
    m = PERIOD_RE.match(text or "")
    if not m:
        raise ValueError("period looks like '1d', '24h', '7 days' or '30 min'")
    n, unit = int(m.group(1)), m.group(2).lower()
    multipliers = {
        "m": {"minutes": n},
        "h": {"hours": n},
        "w": {"weeks": n},
        "d": {"days": n},
    }
    key = unit[0]
    try:
        delta = timedelta(**multipliers[key])
    except OverflowError as exc:
        # timedelta stores its fields as C ints, so a huge input raises. The
        # callers already turn ValueError into a clean status: "error" result.
        raise ValueError(f"period is out of range: {text.strip()}") from exc
    # A value that fits timedelta's C ints can still be an impossible period:
    # 99999999999 minutes is 190,000 years. A period is a recompute window or
    # a freshness bound, so anything past a century is a mistaken input. Cap it
    # once, for every unit, rather than letting the C int decide which units
    # overflow. Valid periods like 30d are far below this.
    if delta > timedelta(days=365 * 100):
        raise ValueError(f"period is out of range: {text.strip()}")
    return delta


def _batch_size(period: timedelta) -> str:
    if period <= timedelta(hours=1):
        return "hour"
    if period <= timedelta(days=1):
        return "day"
    if period <= timedelta(days=31):
        return "month"
    return "year"


def _alias_map(tree: exp.Expression, dialect: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in base_tables(tree, dialect):
        ref = table_ref(t, dialect)
        out[(t.alias_or_name or "").lower()] = ref
        out[t.name.lower()] = ref
    return out


def _find_time_column(tree: exp.Expression, explicit: str | None) -> tuple[exp.Column | None, str]:
    columns = list(tree.find_all(exp.Column))
    if explicit:
        parts = explicit.lower().split(".")
        name, qual = parts[-1], (parts[-2] if len(parts) > 1 else None)
        for col in columns:
            if col.name.lower() == name and (qual is None or col.table.lower() == qual):
                return col, "given"
        return exp.column(name, table=qual), "given"
    scored: dict[str, tuple[int, exp.Column]] = {}
    for col in columns:
        if not TIME_NAME.search(col.name):
            continue
        weight = 1
        if col.find_ancestor(exp.Where):
            weight += 3
        if col.find_ancestor(exp.Group):
            weight += 2
        key = f"{col.table}.{col.name}".lower()
        prev = scored.get(key)
        scored[key] = ((prev[0] if prev else 0) + weight, col)
    if not scored:
        return None, "none"
    best = max(scored.values(), key=lambda item: item[0])
    return best[1], "guessed from the column name"


def _expr_uses(node: exp.Expression, column: str) -> bool:
    return any(c.name.lower() == column for c in node.find_all(exp.Column))


def _resolve_ordinal(outer: exp.Select, node: exp.Expression) -> exp.Expression:
    """GROUP BY 1 -> the first projection (without its alias)."""
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            index = int(node.this) - 1
        except ValueError:
            return node
        if 0 <= index < len(outer.expressions):
            return outer.expressions[index].unalias()
    return node


def _output_name_for(outer: exp.Select, column: str) -> str | None:
    for proj in outer.expressions:
        if isinstance(proj, exp.Column) and proj.name.lower() == column:
            return proj.alias_or_name
        if isinstance(proj, exp.Alias) and isinstance(proj.this, exp.Column) and proj.this.name.lower() == column:
            return proj.alias_or_name
    return None


def analyze(sql: str, dialect: str = "duckdb", time_column: str | None = None,
            unique_key: list[str] | None = None, period: str = "1d", lookback: int = 1) -> dict[str, Any]:
    stmt = classify(sql, dialect)
    if stmt.kind == "block" or stmt.select is None:
        return {"strategy": "not_safe", "reasons": [stmt.reason or "not a query"]}
    tree = stmt.select
    outer = _outer_select(tree)
    if outer is None:
        return {"strategy": "not_safe", "reasons": ["UNION/INTERSECT at the top level: split it into models first"]}
    step = parse_period(period)
    reasons: list[str] = []
    notes: list[str] = []

    col, how = _find_time_column(tree, time_column)
    if col is None:
        return {"strategy": "not_safe",
                "reasons": ["No time column found. Pass --time-column (the column that tells which rows are new)."]}
    tname = col.name.lower()
    aliases = _alias_map(tree, dialect)
    tables = [table_ref(t, dialect) for t in base_tables(tree, dialect)]
    source = aliases.get(col.table.lower()) if col.table else (tables[0] if tables else None)
    if not col.table and len(set(tables)) > 1:
        notes.append(f"'{tname}' is not qualified; assumed it belongs to {source}. Qualify it (e.g. e.{tname}) "
                     "if that is wrong.")
    if how != "given":
        notes.append(f"Time column '{tname}' was {how}; pass --time-column to be sure.")

    group = outer.args.get("group")
    group_exprs = [_resolve_ordinal(outer, g) for g in (group.expressions if group else [])]
    time_grain = any(_expr_uses(g, tname) for g in group_exprs)
    entity_keys = [g for g in group_exprs if not _expr_uses(g, tname)]
    windows = list(tree.find_all(exp.Window))
    aggregated = _has_aggregation(outer)

    if outer.args.get("limit"):
        reasons.append("The model has a LIMIT, so new rows can change which rows are kept.")
    if outer.args.get("distinct") and not aggregated:
        notes.append("SELECT DISTINCT: use a unique_key and merge so duplicates across runs collapse.")
    window_partition_keys: list[exp.Expression] = []
    for w in windows:
        parts = w.args.get("partition_by") or []
        if not parts:
            reasons.append("A window function runs over all rows (no PARTITION BY); every new row can change "
                           "old results.")
        elif any(_expr_uses(p, tname) for p in parts):
            continue
        else:
            window_partition_keys.extend(parts)

    if reasons:
        strategy = "not_safe"
    elif aggregated and time_grain:
        strategy = "partition_overwrite"
    elif aggregated and entity_keys:
        strategy = "merge_recompute"
    elif aggregated:
        strategy = "not_safe"
        reasons.append("Totals over all rows without a GROUP BY: every new row changes the one result row. "
                       "Keep the full rebuild, or group by a time grain.")
    elif window_partition_keys:
        strategy = "merge_recompute"
        entity_keys = window_partition_keys
        notes.append("Window functions are partitioned by an entity key: affected entities are recomputed.")
    else:
        strategy = "append"

    result: dict[str, Any] = {
        "strategy": strategy,
        "time_column": tname,
        "time_column_source": source,
        "period": period,
        "lookback_periods": lookback,
        "reasons": reasons,
        "notes": notes,
    }
    if strategy == "not_safe":
        return result

    key_cols = [k for k in (unique_key or []) if k]
    out_time = _output_name_for(outer, tname)
    if strategy == "merge_recompute":
        key_names = [k.alias_or_name if isinstance(k, exp.Column) else k.sql(dialect=dialect) for k in entity_keys]
        key_cols = key_cols or [k.split(".")[-1] for k in key_names]
        result["entity_keys"] = key_names
    if strategy == "append" and not key_cols:
        notes.append("Without a unique_key, rows that arrive late or get updated are appended again. Pass "
                     "--unique-key to merge instead.")

    # Build the incremental SQL by wrapping the source table in a filtered subquery.
    def filtered(cutoff: str) -> str:
        if strategy == "merge_recompute":
            raw_keys = [k.name if isinstance(k, exp.Column) else k.sql(dialect=dialect) for k in entity_keys]
            key_list = ", ".join(raw_keys)
            pred = f"({key_list}) IN (SELECT DISTINCT {key_list} FROM {source} WHERE {tname} > {cutoff})" \
                if len(raw_keys) > 1 else \
                f"{raw_keys[0]} IN (SELECT DISTINCT {raw_keys[0]} FROM {source} WHERE {tname} > {cutoff})"
        else:
            pred = f"{tname} > {cutoff}"
        return _wrap_source(tree, dialect, source, pred)

    if strategy == "partition_overwrite":
        plain_cutoff = ":cutoff  /* start of the oldest period to recompute */"
    else:
        plain_cutoff = ":last_loaded_at"
    result["incremental_sql"] = filtered(plain_cutoff)

    batch = _batch_size(step)
    if strategy == "append" and out_time:
        dbt_cutoff = f"(SELECT MAX({out_time}) FROM {{{{ this }}}})"
        dbt_filter = filtered(dbt_cutoff)
        cfg = {"materialized": "incremental", "incremental_strategy": "merge" if key_cols else "append"}
        if key_cols:
            cfg["unique_key"] = key_cols if len(key_cols) > 1 else key_cols[0]
        result["dbt_model"] = _dbt(cfg, _jinja_split(result_sql=dbt_filter, full_sql=tree.sql(dialect=dialect)))
    elif strategy == "append":
        notes.append(f"'{tname}' is not in the output, so the model cannot look up its own high-water mark. "
                     f"Add {tname} to the SELECT, or keep the cutoff in a variable.")
    elif strategy == "partition_overwrite":
        cfg = {"materialized": "incremental", "incremental_strategy": "microbatch", "event_time": out_time or tname,
               "batch_size": batch, "lookback": lookback}
        result["dbt_model"] = _dbt(cfg, tree.sql(dialect=dialect)) + (
            f"\n-- microbatch filters upstream models by their own event_time config: set event_time='{tname}' "
            f"on {source}. On older dbt use incremental_strategy='insert_overwrite' (BigQuery) or "
            "'delete+insert' with a date filter.")
    elif strategy == "merge_recompute":
        cfg = {"materialized": "incremental", "incremental_strategy": "merge",
               "unique_key": key_cols if len(key_cols) > 1 else key_cols[0]}
        cutoff = "(SELECT MAX(_gv_loaded_at) FROM {{ this }})"
        notes.append("Add CURRENT_TIMESTAMP AS _gv_loaded_at (or the max source time) to the output so the model "
                      "knows its last run, or replace the cutoff with your own.")
        result["dbt_model"] = _dbt(cfg, _jinja_split(result_sql=filtered(cutoff), full_sql=tree.sql(dialect=dialect)))
    return result


def _wrap_source(tree: exp.Expression, dialect: str, source: str | None, predicate: str) -> str:
    work = tree.copy()
    replacements: dict[str, str] = {}
    for i, table in enumerate(base_tables(work, dialect)):
        ref = table_ref(table, dialect)
        if source is None or ref.lower() != source.lower():
            continue
        alias = table.alias or table.name
        placeholder = f"gvincrplaceholder{i}"
        table.replace(exp.Table(this=exp.to_identifier(placeholder),
                                alias=exp.TableAlias(this=exp.to_identifier(alias))))
        replacements[placeholder] = f"(SELECT * FROM {ref} WHERE {predicate})"
    sql = work.sql(dialect=dialect, pretty=True)
    for ph, sub in replacements.items():
        sql = re.sub(rf"[`\"\[]?{ph}[`\"\]]?", lambda _m, s=sub: s, sql)
    return sql


def _jinja_split(result_sql: str, full_sql: str) -> str:
    return "{% if is_incremental() %}\n" + result_sql + "\n{% else %}\n" + full_sql + "\n{% endif %}"


def _dbt(cfg: dict[str, Any], body: str) -> str:
    def fmt(v: Any) -> str:
        if isinstance(v, list):
            return "[" + ", ".join(repr(x) for x in v) + "]"
        return repr(v) if isinstance(v, str) else str(v)

    args = ",\n    ".join(f"{k}={fmt(v)}" for k, v in cfg.items())
    return "{{\n  config(\n    " + args + "\n  )\n}}\n\n" + body
