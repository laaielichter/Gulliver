"""The other workflows from the research, on top of the same engines, budget gate and receipts:

  impact / lineage   blast radius of a table or column change across SQL models
  snapshot / drift   schema drift of a connection, mapped to impacted models
  translate          SQL from one dialect to another, optionally preflighted on the target
  diff               do two queries / tables return the same rows (in-engine, bucketed hashing)
  incremental        can a full-rebuild model become incremental, and what does it save
  check              data quality checks in one aggregate scan
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from . import checks as checks_mod
from . import diffing, guard, lineage, runaway
from . import incremental as incr
from .config import ConfigError, Connection, Settings
from .core import _engine, _error, _fmt_bytes, _fmt_usd, _gate, _store, _table_infos
from .engines import Engine, EngineError, dialect_for
from .lint import lint
from .security import policy_for
from .sqlsafety import classify, sql_fingerprint
from .store import Store, iso, now_utc

QUERY_TIMEOUT_CAP = 900.0


# -- shared: run one internal query through the budget gate ------------------------------
class Blocked(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__(result.get("next_step", "blocked"))
        self.result = result


def _gated_run(settings: Settings, conn_name: str, engine: Engine, sql: str, purpose: str,
               preview_rows: int = 10_000, ignore_rules: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Estimate + gate + execute one query Gulliver generated itself. Raises Blocked when it may not run."""
    store = _store(settings)
    dialect = dialect_for(engine.name)
    stmt = classify(sql, dialect, policy_for(engine.connection))
    if stmt.kind != "read":
        raise Blocked({"status": "block", "next_step": f"Gulliver refused its own {purpose} query: {stmt.reason}"})
    infos, _ = _table_infos(engine, stmt)
    findings = [f for f in lint(stmt.select, dialect, {k: v.as_dict() for k, v in infos.items()})
                if f.severity == "error" and f.rule not in ignore_rules]
    try:
        estimate = engine.estimate(sql)
    except EngineError as exc:
        raise Blocked({"status": "error", "error": str(exc),
                       "next_step": f"The engine rejected the {purpose} query. Check table and column names."})
    spent = store.spent_today()["usd"]
    gate = _gate(settings, engine, estimate, findings, spent)
    if gate["status"] == "block":
        raise Blocked({"status": "block", "gate": gate, "estimate": estimate.as_dict(),
                       "next_step": f"The {purpose} query is over budget or unsafe: " + "; ".join(gate["blocks"])
                                    + ". Add a WHERE filter (for example on the partition column)."})
    cost = estimate.cost_usd
    if cost is not None and cost > settings.budget.approval_over_usd:
        sql_hash = sql_fingerprint(sql)
        if store.find_approval(sql_hash, conn_name, cost=cost) is None:
            rec = {"action": purpose, "status": "needs_approval", "connection": conn_name, "engine": engine.name,
                   "sql": sql, "sql_hash": sql_hash, "estimate": estimate.as_dict(),
                   "gate": {**gate, "cost_for_gate_usd": cost}}
            rid = store.save_receipt(rec)
            raise Blocked({"status": "needs_approval", "receipt_id": rid, "estimate": estimate.as_dict(),
                           "next_step": f"The {purpose} query costs {_fmt_usd(cost)}. Ask the human to run "
                                        f"`gulliver approve {rid}`, then call again."})
    rid = store.new_id(purpose)
    loop_guard = runaway.check(settings, engine, sql, connection=conn_name, sql_hash=sql_fingerprint(sql), kind=purpose,
                          cost=cost, bytes_=estimate.bytes, receipt_id=rid,
                          receipt={"action": purpose, "connection": conn_name, "engine": engine.name, "sql": sql,
                                   "sql_hash": sql_fingerprint(sql), "estimate": estimate.as_dict(),
                                   "gate": {**gate, "cost_for_gate_usd": cost}})
    if loop_guard["status"] != "ok":
        raise Blocked({"status": loop_guard["status"], "next_step": f"The {purpose} query: {loop_guard['next_step']}",
                       **loop_guard["extra"]})
    budget = settings.budget
    res = engine.execute(sql, preview_rows=preview_rows, timeout_s=min(budget.max_runtime_s, QUERY_TIMEOUT_CAP),
                         max_bytes_billed=engine.max_bytes_for_cost(budget.max_query_usd))
    paid = res.cost_usd if res.cost_usd is not None else engine.cost_for(res.bytes_processed, res.elapsed_s)
    store.record_spend({"kind": purpose, "connection": conn_name, "engine": engine.name,
                        "sql_hash": sql_fingerprint(sql), "cost_usd": paid, "bytes": res.bytes_processed,
                        "elapsed_s": round(res.elapsed_s, 4), **loop_guard["ledger"]})
    return {"rows": res.preview, "columns": res.columns, "elapsed_s": res.elapsed_s, "cost_usd": paid,
            "bytes": res.bytes_processed, "estimate": estimate.as_dict(), "truncated": res.truncated}


# -- lineage and impact ---------------------------------------------------------------------
def _models(models_dir: str | None, manifest: str | None) -> dict[str, lineage.Model]:
    if not models_dir and not manifest:
        raise ValueError("Give a models folder (--models) or a dbt manifest.json (--manifest).")
    return lineage.load_models(models_dir, manifest)


def lineage_overview(models_dir: str | None = None, manifest: str | None = None,
                     dialect: str = "duckdb") -> dict[str, Any]:
    try:
        models = _models(models_dir, manifest)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        return _error(str(exc))
    graph = lineage.build(models, dialect)
    sources = sorted({t for m in models.values() for t in m.tables if t not in models})
    return {"status": "ok" if not graph["errors"] else "warn", "models": [m.summary() for m in models.values()],
            "sources": sources, "edges": graph["edges"], "cycles": graph["cycles"], "errors": graph["errors"],
            "next_step": "Call gulliver_impact with a table or table.column to see what a change would break."}


def impact(target: str, models_dir: str | None = None, manifest: str | None = None,
           dialect: str = "duckdb") -> dict[str, Any]:
    try:
        models = _models(models_dir, manifest)
        table, column = lineage.parse_target(target)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        return _error(str(exc))
    graph = lineage.build(models, dialect)
    known_tables = {t for m in models.values() for t in m.tables} | set(models)
    whole = target.strip().strip('`"').lower()
    if any(t == whole or t.endswith("." + whole) for t in known_tables):
        table, column = whole, "*"  # 'raw.users' names a table, not table 'raw' column 'users'
    result = lineage.impact(models, [(table, column)])
    status = "warn" if result["count"] else "ok"
    return {
        "status": status,
        "target": {"table": table, "column": None if column == "*" else column},
        "affected": result["affected"],
        "count": result["count"],
        "errors": graph["errors"],
        "next_step": ("Update or test these models before changing the source." if result["count"] else
                      "Nothing downstream uses it in these models.")
        + (" Some models could not be parsed; see errors." if graph["errors"] else ""),
    }


# -- schema snapshot and drift -------------------------------------------------------------
def _snapshot_path(store: Store, conn_name: str):
    return store.root / "schemas" / f"{conn_name}.json"


def snapshot(settings: Settings, connection: str | None = None) -> dict[str, Any]:
    store = _store(settings)
    try:
        with _engine(settings, connection) as (conn, engine):
            tables = lineage.snapshot_from_engine(engine)
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))
    path = _snapshot_path(store, conn.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    store._ensure()
    path.write_text(json.dumps({"taken_at": iso(now_utc()), "connection": conn.name, "tables": tables}, indent=2))
    return {"status": "ok", "connection": conn.name, "tables": len(tables), "taken_at": iso(now_utc()),
            "next_step": "Later, call gulliver_drift to see what changed since this snapshot."}


def drift(settings: Settings, connection: str | None = None, models_dir: str | None = None,
          manifest: str | None = None, save: bool = False) -> dict[str, Any]:
    store = _store(settings)
    try:
        with _engine(settings, connection) as (conn, engine):
            current = lineage.snapshot_from_engine(engine)
            dialect = dialect_for(engine.name)
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))
    path = _snapshot_path(store, conn.name)
    if not path.is_file():
        snap = snapshot(settings, connection)
        snap["next_step"] = "No earlier snapshot existed, so one was saved now. Call gulliver_drift again later."
        snap["status"] = "ok"
        return snap
    old = json.loads(path.read_text())
    diff = lineage.compare(old.get("tables") or {}, current)
    result: dict[str, Any] = {"status": "ok", "connection": conn.name, "since": old.get("taken_at"), **diff}
    breaking = [c for c in diff["changes"] if c["change"] in ("removed", "type_changed")]
    breaking_tables = diff["tables_removed"]
    if models_dir or manifest:
        try:
            models = _models(models_dir, manifest)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            return _error(str(exc))
        lineage.build(models, dialect)
        targets = [(c["table"], c["column"]) for c in breaking] + [(t, "*") for t in breaking_tables]
        result["impact"] = lineage.impact(models, targets)["affected"] if targets else []
    if breaking or breaking_tables:
        result["status"] = "warn"
        result["next_step"] = ("Columns or tables were removed or changed type. Fix the impacted models, then "
                               "call gulliver_drift with save=true to accept the new schema.")
    elif diff["changes"] or diff["tables_added"]:
        result["next_step"] = "Only additive changes. Call gulliver_drift with save=true to accept them."
    else:
        result["next_step"] = "No schema drift since the last snapshot."
    if save:
        path.write_text(json.dumps({"taken_at": iso(now_utc()), "connection": conn.name, "tables": current},
                                   indent=2))
        result["saved"] = True
    return result


# -- translate -------------------------------------------------------------------------------
def translate(settings: Settings | None, sql: str, target: str, source: str = "duckdb",
              connection: str | None = None) -> dict[str, Any]:
    dialects = ("duckdb", "postgres", "bigquery", "snowflake")
    if source not in dialects or target not in dialects:
        return _error(f"dialects must be one of {', '.join(dialects)}")
    try:
        out = diffing.translate(sql, source, target)
    except Exception as exc:
        return _error(f"Could not translate: {str(exc).splitlines()[0]}")
    result: dict[str, Any] = {"status": "warn" if out["warnings"] else "ok", "from": source, "to": target, **out}
    if connection and settings is not None:
        from .core import preflight

        conn = settings.connection(connection)
        if dialect_for(conn.engine) != target:
            result["check"] = {"status": "skipped", "reason": f"connection {conn.name} is {conn.engine}, not {target}"}
        else:
            pre = preflight(settings, out["sql"], connection=connection)
            result["check"] = {k: pre.get(k) for k in ("status", "error", "estimate", "next_step", "receipt_id")
                               if pre.get(k) is not None}
            if pre.get("status") in ("error", "block"):
                result["status"] = "warn"
    result["next_step"] = ("Review the warnings, then preflight the translated SQL on the target." if out["warnings"]
                           else "Preflight the translated SQL on the target, then compare results with gulliver_diff.")
    return result


# -- data diff -------------------------------------------------------------------------------
def diff(settings: Settings, left_sql: str, right_sql: str, key: list[str], connection: str | None = None,
         right_connection: str | None = None, columns: list[str] | None = None,
         buckets: int = diffing.DEFAULT_BUCKETS, max_keys: int = 20) -> dict[str, Any]:
    if not key:
        return _error("Give the key column(s) that identify a row, e.g. key=['order_id'].")
    left_sql = _as_query(left_sql.strip().rstrip(";"))
    right_sql = _as_query(right_sql.strip().rstrip(";"))
    store = _store(settings)
    result: dict[str, Any] = {"action": "diff", "key": key}
    try:
        with _engine(settings, connection) as (lconn, left), \
                _engine(settings, right_connection or connection) as (rconn, right):
            result.update(left_connection=lconn.name, right_connection=rconn.name)
            lcols = [c.lower() for c in left.query_columns(left_sql)]
            rcols = [c.lower() for c in right.query_columns(right_sql)]
            missing_key = [k for k in key if k.lower() not in lcols or k.lower() not in rcols]
            if missing_key:
                return _error(f"Key column(s) not in both results: {', '.join(missing_key)}",
                              "Pick key columns that both queries return.")
            if columns:
                compare_cols = [c.lower() for c in columns]
            else:
                compare_cols = sorted(set(lcols) & set(rcols))
            result["columns_compared"] = compare_cols
            only_l, only_r = sorted(set(lcols) - set(rcols)), sorted(set(rcols) - set(lcols))
            if only_l or only_r:
                result["column_differences"] = {"only_in_left": only_l, "only_in_right": only_r}
            if lconn.engine != rconn.engine:
                result["notes"] = ["Cross-engine diff compares values as text; numbers and timestamps may be "
                                   "formatted differently. Cast or round them in both queries if they show up "
                                   "as changed."]
            lb = _gated_run(settings, lconn.name, left, diffing.bucket_sql(left, left_sql, key, compare_cols, buckets),
                            "diff")
            rb = _gated_run(settings, rconn.name, right,
                            diffing.bucket_sql(right, right_sql, key, compare_cols, buckets), "diff")
            summary = diffing.compare_buckets(lb["rows"], rb["rows"])
            result.update(left_rows=summary["left_rows"], right_rows=summary["right_rows"],
                          buckets=buckets, buckets_different=len(summary["buckets_different"]))
            cost = (lb["cost_usd"] or 0) + (rb["cost_usd"] or 0)
            if not summary["buckets_different"]:
                result.update(status="ok", identical=True, cost_usd=round(cost, 6),
                              next_step="Identical: same keys and same values in the compared columns.")
            else:
                differing = summary["buckets_different"]
                row_budget = 20_000
                chosen, modulus, complete = diffing.plan_drill(lb["rows"], rb["rows"], differing, row_budget, buckets)
                if not chosen:
                    # Coarse buckets are too big to list: zoom in with a finer second pass on a few of them.
                    sizes = {int(r["gv_bucket"]): int(r["gv_n"]) for r in lb["rows"] + rb["rows"]}
                    focus = differing[:8]
                    factor = min(4096, max(2, -(-max(sizes.get(b, 1) for b in focus) // 1000)))
                    modulus = buckets * factor
                    lf = _gated_run(settings, lconn.name, left, diffing.bucket_sql(
                        left, left_sql, key, compare_cols, modulus, within=(buckets, focus)), "diff")
                    rf = _gated_run(settings, rconn.name, right, diffing.bucket_sql(
                        right, right_sql, key, compare_cols, modulus, within=(buckets, focus)), "diff")
                    cost += (lf["cost_usd"] or 0) + (rf["cost_usd"] or 0)
                    fine = diffing.compare_buckets(lf["rows"], rf["rows"])["buckets_different"]
                    chosen, _, fine_complete = diffing.plan_drill(lf["rows"], rf["rows"], fine, row_budget, modulus)
                    complete = fine_complete and len(focus) == len(differing)
                lk = _gated_run(settings, lconn.name, left, diffing.keys_sql(
                    left, left_sql, key, compare_cols, modulus, chosen, row_budget + 1), "diff", preview_rows=row_budget + 1)
                rk = _gated_run(settings, rconn.name, right, diffing.keys_sql(
                    right, right_sql, key, compare_cols, modulus, chosen, row_budget + 1), "diff", preview_rows=row_budget + 1)
                details = diffing.compare_keys(lk["rows"], rk["rows"], max_keys, columns=compare_cols)
                cost += (lk["cost_usd"] or 0) + (rk["cost_usd"] or 0)
                notes = result.get("notes", [])
                if not complete and details.get("changed", {}).get("count", 0) > 0:
                    sample_note = "Column counts cover the sampled buckets only."
                    if "note" in details["changed"]:
                        details["changed"]["note"] += f" {sample_note}"
                    else:
                        details["changed"]["note"] = sample_note
                if "note" in details.get("changed", {}) and details["changed"]["note"] not in notes:
                    notes.append(details["changed"]["note"])
                if notes:
                    result["notes"] = notes

                cols_diff = details.get("changed", {}).get("columns", {})
                if cols_diff:
                    col_parts = [f"{c} ({n} {'key' if n == 1 else 'keys'})" for c, n in list(cols_diff.items())[:5]]
                    differ_prefix = f"Rows differ in: {', '.join(col_parts)}."
                else:
                    differ_prefix = "Rows differ."

                result.update(status="warn", identical=False, differences=details, cost_usd=round(cost, 6),
                              details_complete=complete,
                              next_step=(differ_prefix + " "
                                         + ("Counts above are complete." if complete else
                                            "Counts above cover a sample of the differing rows; left_rows and "
                                            "right_rows are the full totals.")
                                         + " Inspect the sample keys."))
    except Blocked as blocked:
        return {**result, **blocked.result}
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))
    result["sql_hash"] = sql_fingerprint(left_sql + " <-> " + right_sql)
    result["sql"] = f"-- left\n{left_sql}\n-- right\n{right_sql}"
    result["connection"] = result.get("left_connection")
    result["engine"] = "diff"
    store.save_receipt(result)
    return result


def _as_query(text: str) -> str:
    """A bare table name becomes SELECT * FROM it."""
    stripped = text.strip()
    if stripped and all(ch.isalnum() or ch in "._`\"[]-" for ch in stripped):
        return f"SELECT * FROM {stripped}"
    return stripped


# -- incremental advisor -------------------------------------------------------------------------
def incremental(settings: Settings | None, sql: str, connection: str | None = None, time_column: str | None = None,
                unique_key: list[str] | None = None, period: str = "1d", lookback: int = 1,
                estimate_savings: bool = True) -> dict[str, Any]:
    dialect = "duckdb"
    if settings is not None:
        try:
            dialect = dialect_for(settings.connection(connection).engine)
        except ConfigError as exc:
            return _error(str(exc))
    from .sqlsafety import classify as _classify

    rendered = lineage.render(sql)[0] if "{{" in sql or "{%" in sql else sql
    if _classify(rendered, dialect).kind == "block":
        return _error("Give the model's SELECT (dbt Jinja is fine).")
    try:
        advice = incr.analyze(rendered, dialect, time_column=time_column, unique_key=unique_key,
                              period=period, lookback=lookback)
    except ValueError as exc:
        return _error(str(exc))
    result: dict[str, Any] = {"status": "ok" if advice["strategy"] != "not_safe" else "warn", **advice}
    if advice["strategy"] == "not_safe":
        result["next_step"] = "Keep the full rebuild: " + "; ".join(advice["reasons"])
        return result
    if settings is not None and estimate_savings and advice.get("time_column_source"):
        try:
            result["savings"] = _savings(settings, connection, advice, dialect)
        except Blocked as blocked:
            result["savings"] = {"status": blocked.result.get("status"), "reason": blocked.result.get("next_step")}
        except (EngineError, ConfigError) as exc:
            result["savings"] = {"status": "error", "reason": str(exc)}
    result["next_step"] = ("Review the generated SQL, then prove it with gulliver_diff: compare the full model "
                           "with the incremental result for the same period.")
    return result


def _savings(settings: Settings, connection: str | None, advice: dict[str, Any], dialect: str) -> dict[str, Any]:
    source, tcol = advice["time_column_source"], advice["time_column"]
    step = incr.parse_period(advice["period"]) * (1 + int(advice.get("lookback_periods") or 0))
    with _engine(settings, connection) as (conn, engine):
        q = engine.quote
        # These two queries read only the time column on purpose, so a missing partition filter is expected.
        latest = _gated_run(settings, conn.name, engine, f"SELECT MAX({q(tcol)}) AS gv_max FROM {source}",
                            "incremental", ignore_rules=frozenset({"GV004"}))["rows"]
        max_value = latest[0]["gv_max"] if latest else None
        if max_value is None:
            return {"status": "skipped", "reason": "the source has no rows"}
        cutoff, ctype = _cutoff_literal(max_value, step)
        count_sql = (f"SELECT COUNT(*) AS gv_total, SUM(CASE WHEN {q(tcol)} > CAST('{cutoff}' AS {ctype}) "
                     f"THEN 1 ELSE 0 END) AS gv_recent FROM {source}")
        row = _gated_run(settings, conn.name, engine, count_sql, "incremental",
                         ignore_rules=frozenset({"GV004"}))["rows"][0]
        total, recent = int(row["gv_total"] or 0), int(row["gv_recent"] or 0)
        out: dict[str, Any] = {
            "status": "ok",
            "source_rows": total,
            "rows_per_incremental_run": recent,
            "cutoff_used": cutoff,
            "rows_saved_pct": round((1 - recent / total) * 100, 1) if total else None,
        }
        if advice["strategy"] == "merge_recompute":
            out["note"] = ("merge_recompute re-reads the full history of affected keys, so the real saving is "
                           "lower than the row share above.")
        if engine.billed:
            full_sql = f"SELECT * FROM {source}"
            inc_sql = f"SELECT * FROM {source} WHERE {q(tcol)} > CAST('{cutoff}' AS {ctype})"
            full_est, inc_est = engine.estimate(full_sql), engine.estimate(inc_sql)
            out["bytes_full"] = full_est.bytes
            out["bytes_incremental"] = inc_est.bytes
            out["cost_full_usd"] = full_est.cost_usd
            out["cost_incremental_usd"] = inc_est.cost_usd
            if full_est.bytes and inc_est.bytes is not None:
                out["bytes_saved_pct"] = round((1 - inc_est.bytes / full_est.bytes) * 100, 1)
                if out["bytes_saved_pct"] < 5:
                    out["note"] = ("The engine still scans the whole table for the recent slice: partition or "
                                   f"cluster {source} by {tcol} to turn the row saving into a cost saving.")
        out["summary"] = (f"Each run would read about {recent:,} of {total:,} source rows "
                          f"({out['rows_saved_pct']}% fewer)." if total else "Source is empty.")
        return out


def _cutoff_literal(max_value: Any, step: timedelta) -> tuple[str, str]:
    if isinstance(max_value, str):
        try:
            parsed: Any = datetime.fromisoformat(max_value.replace("Z", "+00:00"))
            if len(max_value.strip()) == 10:
                parsed = parsed.date()
        except ValueError:
            raise Blocked({"status": "skipped", "next_step": f"time column value {max_value!r} is not a date/time"})
    else:
        parsed = max_value
    if isinstance(parsed, datetime):
        return (parsed.replace(tzinfo=None) - step).isoformat(sep=" ", timespec="seconds"), "TIMESTAMP"
    if isinstance(parsed, date):
        return (parsed - step).isoformat(), "DATE"
    raise Blocked({"status": "skipped", "next_step": f"time column value {max_value!r} is not a date/time"})


# -- data quality checks --------------------------------------------------------------------------
def _check_samples(settings: Settings, conn: Connection, engine: Engine, source: str, spec: checks_mod.CheckSpec,
                   outcomes: list[dict[str, Any]], limit: int, where: str | None) -> list[dict[str, Any]]:
    runs = []
    for outcome in outcomes:
        if outcome.get("passed"):
            continue
        query = checks_mod.sample_sql(engine, source, spec, outcome, limit, where)
        if query is None:
            continue
        sql, count_column = query
        try:
            dialect = dialect_for(engine.name)
            stmt = classify(sql, dialect, policy_for(conn))
            infos, _ = _table_infos(engine, stmt)
            _, shield = guard.check_query(settings, conn, stmt, dialect, infos, [])
            if shield["blocks"]:
                raise Blocked({"status": "block", "next_step": "; ".join(shield["blocks"])})
            res = _gated_run(settings, conn.name, engine, sql, "check_samples",
                             preview_rows=min(limit, settings.budget.max_output_rows))
            runs.append({"cost_usd": res["cost_usd"], "bytes": res["bytes"]})
            protected = guard.guard_preview(settings, conn, res["columns"], res["rows"], {"sql_hash": stmt.sql_hash})
            rows = protected.pop("preview")
            outcome["samples"] = [{"values": {k: v for k, v in row.items() if k != count_column},
                                   "rows": row.get(count_column)} for row in rows]
            outcome["samples_guard"] = protected
            outcome["samples_cost_usd"] = res["cost_usd"]
            outcome["samples_scanned"] = _fmt_bytes(res["bytes"]) if res["bytes"] is not None else None
        except Blocked as blocked:
            skipped = dict(blocked.result)
            if "error" in skipped:
                skipped["error"] = guard.scrub_error(settings, conn, sql, dialect_for(engine.name), skipped["error"])
            outcome["samples_skipped"] = skipped
        except EngineError as exc:
            outcome["samples_skipped"] = {
                "status": "error", "next_step": "The sample query failed; the check result is unchanged.",
                "error": guard.scrub_error(settings, conn, sql, dialect_for(engine.name), str(exc))}
    return runs


def check(settings: Settings, target: str, connection: str | None = None, where: str | None = None,
          unique: list[str] | None = None, not_null: list[str] | None = None, accepted: list[str] | None = None,
          ranges: list[str] | None = None, fresh: list[str] | None = None, min_rows: int | None = None,
          max_change_pct: float | None = None, samples: int = 0, name: str | None = None,
          spec: checks_mod.CheckSpec | None = None) -> dict[str, Any]:
    if isinstance(samples, bool) or not isinstance(samples, int) or not 0 <= samples <= 20:
        return _error("samples must be an integer from 0 to 20.")
    base_spec = spec
    if name is not None:
        if name not in settings.checks:
            known = ", ".join(sorted(settings.checks)) or "(none)"
            return _error(f"Unknown configured check '{name}'. Configured checks: {known}.")
        cfg = settings.checks[name]
        target = cfg.target if target == name else target
        connection = connection or cfg.connection
        if where is None:
            where = cfg.where
        if samples == 0:
            samples = cfg.samples
        base_spec = cfg.spec
    elif target in settings.checks and base_spec is None:
        cfg = settings.checks[target]
        name = target
        target = cfg.target
        connection = connection or cfg.connection
        if where is None:
            where = cfg.where
        if samples == 0:
            samples = cfg.samples
        base_spec = cfg.spec
    try:
        cli_spec = checks_mod.parse_spec(unique, not_null, accepted, ranges, fresh, min_rows, max_change_pct)
    except ValueError as exc:
        return _error(str(exc))
    spec = checks_mod.merge_specs(base_spec, cli_spec) if base_spec is not None else cli_spec
    store = _store(settings)
    source = target.strip().rstrip(";")
    if not all(ch.isalnum() or ch in "._`\"[]-" for ch in source):
        source = f"({_as_query(source)}) AS gv_src"
    try:
        conn_name = settings.connection(connection).name
    except ConfigError as exc:
        return _error(str(exc))
    history_key = sql_fingerprint(f"{conn_name}|{source}|{where or ''}")
    history_path = store.root / "checks" / f"{history_key}.json"
    read_path = history_path
    if connection is None and not history_path.is_file():
        # A history written before the key used the resolved name spells the
        # omitted connection as None; keep reading it and migrate on this run.
        legacy_key = sql_fingerprint(f"None|{source}|{where or ''}")
        legacy_path = store.root / "checks" / f"{legacy_key}.json"
        if legacy_path.is_file():
            read_path = legacy_path
    previous = json.loads(read_path.read_text()) if read_path.is_file() else None
    result: dict[str, Any] = {"action": "check", "target": target, "where": where}
    if name:
        result["name"] = name
    try:
        with _engine(settings, connection) as (conn, engine):
            sql, plan = checks_mod.build_sql(engine, source, spec, where)
            res = _gated_run(settings, conn.name, engine, sql, "check", preview_rows=1)
            row = res["rows"][0] if res["rows"] else {}
            outcomes = checks_mod.evaluate(row, plan, spec, previous.get("rows") if previous else None)
            sample_runs = _check_samples(settings, conn, engine, source, spec, outcomes, samples, where) if samples else []
    except Blocked as blocked:
        return {**result, **blocked.result}
    except (EngineError, ConfigError) as exc:
        return _error(str(exc))
    failed = [o for o in outcomes if not o.get("passed")]
    store._ensure()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps({"rows": int(row.get("gv_rows") or 0), "at": iso(now_utc())}))
    costs = [r["cost_usd"] for r in [res, *sample_runs]]
    scans = [r["bytes"] for r in [res, *sample_runs]]
    result.update(
        status="fail" if failed else "ok",
        connection=conn.name,
        engine=conn.engine,
        sql=sql,
        sql_hash=sql_fingerprint(sql),
        checks=outcomes,
        failed=len(failed),
        cost_usd=sum(costs) if all(c is not None for c in costs) else None,
        scanned=_fmt_bytes(sum(scans)) if all(b is not None for b in scans) else None,
        next_step=("All checks passed." if not failed else
                   f"{len(failed)} check(s) failed: " + ", ".join(
                       f"{o['check']}" + (f" on {o['column']}" if o.get("column") else "") for o in failed)
                   + ". Fix the data or the pipeline before using it."),
    )
    if not samples and any(o["check"] in ("unique", "accepted_values", "range") for o in failed):
        result["next_step"] += " Use --samples N (MCP samples, 1-20) for budget-checked, guarded examples."
    if any("samples_guard" in o for o in outcomes):
        result.update(untrusted=True, data_notice=guard.DATA_NOTICE)
    store.save_receipt(result)
    return result



def check_all(settings: Settings, connection: str | None = None, samples: int = 0) -> dict[str, Any]:
    """Run all data checks configured in gulliver.toml under [checks.*]."""
    if not settings.checks:
        return _error("No checks configured in gulliver.toml under [checks.*].")
    target_summaries: list[dict[str, Any]] = []
    full_results: list[dict[str, Any]] = []
    for name, cfg in settings.checks.items():
        conn = connection or cfg.connection
        smpls = samples if samples > 0 else cfg.samples
        res = check(
            settings,
            target=cfg.target,
            connection=conn,
            where=cfg.where,
            samples=smpls,
            spec=cfg.spec,
            name=name,
        )
        full_results.append(res)
        target_summaries.append({
            "name": name,
            "target": cfg.target,
            "connection": res.get("connection") or conn or settings.default_connection,
            "status": res.get("status", "error"),
            "failed": res.get("failed", 0),
            "receipt_id": res.get("receipt_id"),
            "next_step": res.get("next_step", ""),
        })

    any_fail = any(r.get("status") == "fail" for r in full_results)
    any_block = any(r.get("status") == "block" for r in full_results)
    any_error = any(r.get("status") == "error" for r in full_results)
    if any_fail:
        status = "fail"
    elif any_block:
        status = "block"
    elif any_error:
        status = "error"
    else:
        status = "ok"

    failed_count = sum(1 for t in target_summaries if t["status"] == "fail")
    error_count = sum(1 for t in target_summaries if t["status"] == "error")
    blocked_count = sum(1 for t in target_summaries if t["status"] == "block")
    total_count = len(target_summaries)
    if status == "ok":
        next_step = f"All checks passed across {total_count} target(s)."
    else:
        parts: list[str] = []
        if error_count:
            parts.append(f"{error_count} errored")
        if blocked_count:
            parts.append(f"{blocked_count} blocked")
        parts.append(f"{failed_count} failed")
        counts_str = ", ".join(parts)
        next_step = f"{counts_str} of {total_count} target(s). Fix the data or pipeline."

    return {
        "action": "check",
        "all": True,
        "status": status,
        "targets": target_summaries,
        "results": full_results,
        "total_targets": total_count,
        "failed_targets": failed_count,
        "errored_targets": error_count,
        "blocked_targets": blocked_count,
        "next_step": next_step,
    }
