"""gulliver top: which repeated queries cost the most, and what a proven rewrite would save.

Gulliver normally looks at one new query at a time. `top` looks back at what already ran:

  receipts            Gulliver's own run receipts (free, no history query needed)
  pg_stat_statements  Postgres statement statistics (needs the extension)
  bigquery_jobs       region-<location>.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  snowflake_history   SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY (+ QUERY_ATTRIBUTION_HISTORY when readable)
  file                CSV / JSONL / JSON with query, calls, bytes, runtime_s, cost_usd

Every history query Gulliver sends goes through the budget gate (features._gated_run), because reading
query history costs money too.

Queries are grouped by fingerprint: literals and parameters become `?`, IN lists collapse to one `?`,
comments and case are ignored. When the SQL cannot be parsed, the engine's own hash is used
(BigQuery normalized_literals, Snowflake query_parameterized_hash, Postgres queryid).

For the top groups it runs scale lint and the safe rewrite (fixer.rewrite, same rows only), estimates the
original and the rewrite (dry run / EXPLAIN, no data read), and prices the saving per month:

  saving per month = (1 - estimate_after / estimate_before) x observed cost per month

It is labeled `exact` only when both estimates are exact (BigQuery dry runs); otherwise `heuristic`.
It always assumes next month repeats the window that was read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from .config import ConfigError, Settings
from .core import _engine, _error, _fmt_bytes, _fmt_usd, _store, _table_infos
from .engines import Engine, EngineError, dialect_for
from .engines.base import EXACT, HEURISTIC
from .features import Blocked, _gated_run
from .lint import _has_aggregation, _outer_select, lint
from .sqlsafety import classify
from .store import now_utc, parse_iso

SOURCES = ("auto", "receipts", "pg_stat_statements", "bigquery_jobs", "snowflake_history", "file")
RANK_BY = ("auto", "cost", "runtime", "bytes", "calls")
HISTORY_ROWS = 1000          # query groups read from a warehouse history
MAX_SQL_CHARS = 2000         # sample SQL handed back per group
DAYS_PER_MONTH = 30.0
MIN_PROJECTION_DAYS = 1.0    # shorter windows are too short to scale to a month or to suggest summary tables
SNOWFLAKE_CREDITS_PER_HOUR = {
    "X-Small": 1, "Small": 2, "Medium": 4, "Large": 8, "X-Large": 16, "2X-Large": 32, "3X-Large": 64,
    "4X-Large": 128, "5X-Large": 256, "6X-Large": 512,
}


# -- one history row and one fingerprint group ---------------------------------------------
@dataclass
class Entry:
    sql: str
    calls: int = 1
    bytes: int | None = None
    runtime_s: float | None = None
    cost_usd: float | None = None
    engine_hash: str | None = None
    bytes_min: int | None = None     # per call, when the source knows it
    bytes_max: int | None = None
    variants: int | None = None      # distinct SQL texts behind this row, when the source knows it


@dataclass
class Group:
    fingerprint: str
    sql: str
    calls: int = 0
    bytes: int | None = None
    runtime_s: float | None = None
    cost_usd: float | None = None
    bytes_min: int | None = None
    bytes_max: int | None = None
    spread_known: bool = True
    texts: set[str] = field(default_factory=set)
    engine_variants: int = 0
    engine_hashes: set[str] = field(default_factory=set)
    _sample_weight: float = -1.0

    @property
    def variants(self) -> int:
        return max(len(self.texts), self.engine_variants)


def _add(a: float | int | None, b: float | int | None) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


# -- fingerprinting ----------------------------------------------------------------------------
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w$])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\b", re.I)
_PARAM = re.compile(r"\$\d+|@\w+|:\w+|\?")
_IN_LIST = re.compile(r"\bin\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.I)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def normalize(sql: str, dialect: str = "duckdb") -> str | None:
    """The query's shape: literals and parameters as ?, IN lists collapsed, no comments, lower case.
    None when sqlglot cannot parse it as exactly one statement."""
    try:
        trees = [t for t in sqlglot.parse(sql.strip().rstrip(";"), read=dialect) if t is not None]
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        return None
    if len(trees) != 1:
        return None

    def strip(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.In) and node.expressions:
            node.set("expressions", [exp.var("?")])
            return node
        if isinstance(node, (exp.Literal, exp.Placeholder, exp.Parameter)):
            return exp.var("?")
        return node

    try:
        tree = trees[0].transform(strip)
        text = tree.sql(dialect=dialect, comments=False)
    except Exception:  # an odd construct sqlglot cannot regenerate: fall back to the text form
        return None
    return " ".join(text.split()).lower()


def _text_shape(sql: str) -> str:
    """Fallback shape for SQL sqlglot cannot parse: regex literal stripping."""
    text = _COMMENT.sub(" ", sql)
    text = _STRING.sub("?", text)
    text = _NUMBER.sub("?", text)
    text = _PARAM.sub("?", text)
    text = _IN_LIST.sub("in (?)", text)
    return " ".join(text.split()).rstrip(";").lower()


def fingerprint(sql: str, dialect: str = "duckdb", engine_hash: str | None = None) -> str:
    """Our own normalized shape when it parses (so it works the same for every source); otherwise the
    engine's hash; otherwise a regex-normalized text shape."""
    shape = normalize(sql, dialect)
    if shape is not None:
        return _digest(shape)
    if engine_hash:
        return _digest("engine:" + str(engine_hash))
    return _digest("text:" + _text_shape(sql))


def group_entries(entries: list[Entry], dialect: str) -> list[Group]:
    groups: dict[str, Group] = {}
    seen: dict[tuple[str, str | None], str] = {}  # the same text is parsed once
    for e in entries:
        key = (" ".join(e.sql.split()), e.engine_hash)
        fp = seen.get(key)
        if fp is None:
            fp = seen[key] = fingerprint(e.sql, dialect, e.engine_hash)
        g = groups.get(fp)
        if g is None:
            g = groups[fp] = Group(fingerprint=fp, sql=e.sql)
        g.calls += max(int(e.calls or 0), 0)
        g.bytes = _add(g.bytes, e.bytes)
        g.runtime_s = _add(g.runtime_s, e.runtime_s)
        g.cost_usd = _add(g.cost_usd, e.cost_usd)
        g.texts.add(" ".join(e.sql.split()))
        g.engine_variants += int(e.variants or 0)
        if e.engine_hash:
            g.engine_hashes.add(str(e.engine_hash))
        low, high = e.bytes_min, e.bytes_max
        if low is None and e.bytes is not None and e.calls == 1:
            low = high = e.bytes
        if low is None or high is None:
            g.spread_known = False
        else:
            g.bytes_min = low if g.bytes_min is None else min(g.bytes_min, low)
            g.bytes_max = high if g.bytes_max is None else max(g.bytes_max, high)
        weight = float(e.cost_usd or 0) * 1e6 + float(e.runtime_s or 0)
        if weight > g._sample_weight:  # keep the most expensive variant as the example
            g.sql, g._sample_weight = e.sql, weight
    return list(groups.values())


# -- sources --------------------------------------------------------------------------------
class SourceError(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__(result.get("error", "source error"))
        self.result = result


@dataclass
class History:
    entries: list[Entry]
    window_days: float | None
    cost_basis: str
    notes: list[str] = field(default_factory=list)
    history_cost_usd: float = 0.0


def read_receipts(settings: Settings, connection: str, days: float) -> History:
    """Full runs Gulliver made on this connection (gulliver_run receipts with status ok)."""
    cutoff = now_utc() - timedelta(days=days)
    entries: list[Entry] = []
    for rec in _store(settings).receipts():
        if rec.get("action") != "run" or rec.get("status") != "ok" or rec.get("connection") != connection:
            continue
        try:
            if parse_iso(rec["created_at"]) < cutoff:
                continue
        except (KeyError, ValueError):
            continue
        actual = rec.get("actual") or {}
        entries.append(Entry(sql=str(rec.get("sql") or ""), calls=1, bytes=actual.get("bytes"),
                             runtime_s=actual.get("runtime_s"), cost_usd=actual.get("cost_usd")))
    return History(entries, days, "measured by Gulliver at run time",
                   notes=["Only full runs through gulliver_run are counted; queries run elsewhere are not."])


_PG_SQL = ("SELECT s.queryid AS gv_hash, s.query AS gv_query, s.calls AS gv_calls, "
           "s.total_exec_time AS gv_ms, s.shared_blks_hit + s.shared_blks_read AS gv_blocks "
           "FROM pg_stat_statements AS s "
           "WHERE s.dbid = (SELECT d.oid FROM pg_database AS d WHERE d.datname = current_database()) "
           f"ORDER BY s.total_exec_time DESC LIMIT {HISTORY_ROWS}")
_PG_SINCE_SQL = "SELECT i.stats_reset AS gv_since FROM pg_stat_statements_info AS i"
PG_BLOCK_BYTES = 8192


def read_pg_stat_statements(settings: Settings, conn_name: str, engine: Engine) -> History:
    try:
        res = _gated_run(settings, conn_name, engine, _PG_SQL, "top", preview_rows=HISTORY_ROWS)
    except Blocked as blocked:
        err = str(blocked.result.get("error") or "")
        if blocked.result.get("status") == "error" and ("pg_stat_statements" in err or "does not exist" in err):
            raise SourceError(_error(
                f"pg_stat_statements is not available: {err}",
                "Ask the database admin to add pg_stat_statements to shared_preload_libraries, restart Postgres "
                "and run CREATE EXTENSION pg_stat_statements. Until then use source='receipts' or a file."))
        raise
    cost = float(res["cost_usd"] or 0)
    window: float | None = None
    notes = ["pg_stat_statements counts since its last reset, not per day; bytes are shared buffer blocks "
             "touched x 8 KiB.", "Postgres has no per-query price, so queries are ranked by runtime."]
    try:
        since = _gated_run(settings, conn_name, engine, _PG_SINCE_SQL, "top", preview_rows=1)
        cost += float(since["cost_usd"] or 0)
        value = since["rows"][0]["gv_since"] if since["rows"] else None
        if value:
            started = parse_iso(str(value).replace(" ", "T", 1))
            if started.tzinfo is None:
                started = started.replace(tzinfo=now_utc().tzinfo)
            window = max((now_utc() - started).total_seconds() / 86400.0, 1 / 24)
            notes.append(f"Statistics since {value}.")
    except (Blocked, EngineError, ValueError, KeyError, IndexError):
        pass
    if window is None:
        notes.append("The reset time is unknown (pg_stat_statements_info needs Postgres 14+), so there is no "
                     "per-month projection.")
    entries: list[Entry] = []
    hidden = internal = 0
    for row in _lowered(res["rows"]):
        query = str(row.get("gv_query") or "")
        if not query or query.startswith("<insufficient privilege>"):
            hidden += 1
            continue
        if _pg_not_workload(query):
            internal += 1
            continue
        blocks = row.get("gv_blocks")
        entries.append(Entry(sql=query, calls=int(row.get("gv_calls") or 0),
                             runtime_s=float(row.get("gv_ms") or 0) / 1000.0,
                             bytes=int(blocks) * PG_BLOCK_BYTES if blocks is not None else None,
                             cost_usd=0.0, engine_hash=str(row.get("gv_hash"))))
    if hidden:
        notes.append(f"{hidden} statement(s) hidden: the role needs pg_read_all_stats to see their text.")
    if internal:
        notes.append(f"{internal} statement(s) left out: session and utility statements (SET, EXPLAIN, ...), "
                     "system catalog lookups and reads of pg_stat_statements itself, including Gulliver's own.")
    return History(entries, window, "Postgres is self-hosted: runtime, not money", notes, cost)


_PG_UTILITY = {"SET", "RESET", "SHOW", "BEGIN", "COMMIT", "ROLLBACK", "END", "START", "SAVEPOINT", "RELEASE",
               "EXPLAIN", "DISCARD", "DEALLOCATE", "PREPARE", "LISTEN", "UNLISTEN", "NOTIFY"}
_PG_CATALOG_SCHEMAS = {"pg_catalog", "information_schema"}
_PG_SESSION_FUNCS = {"set_config", "current_setting", "version", "current_database", "current_schema"}


def _pg_not_workload(query: str) -> bool:
    """Statements that are not the database's workload: session and utility statements, system catalog
    lookups and statistics reads (Gulliver's own introspection, estimates and history query among them)."""
    text = _COMMENT.sub(" ", query).strip().lstrip("(").strip()
    first = text.split(None, 1)[0].upper().rstrip(";") if text else ""
    if first in _PG_UTILITY or "pg_stat_statements" in text.lower():
        return True
    if first not in ("SELECT", "WITH"):
        return False
    try:
        trees = [t for t in sqlglot.parse(text.rstrip(";"), read="postgres") if t is not None]
    except Exception:  # cannot tell: keep it
        return False
    if len(trees) != 1:
        return False
    tables = [t for t in trees[0].find_all(exp.Table) if t.name]
    ctes = {c.alias_or_name.lower() for c in trees[0].find_all(exp.CTE)}
    real = [t for t in tables if not (t.name.lower() in ctes and not t.db)]
    if real:
        return all(t.db.lower() in _PG_CATALOG_SCHEMAS or (not t.db and t.name.lower().startswith("pg_"))
                   for t in real)
    names = {(f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
             for f in trees[0].find_all(exp.Func)}
    return bool(names) and all(n.startswith("pg_") or n in _PG_SESSION_FUNCS for n in names)


def bigquery_history_sql(location: str | None, days: int) -> str:
    region = f"region-{(location or 'US').lower()}"
    return (
        "SELECT COALESCE(query_info.query_hashes.normalized_literals, TO_HEX(MD5(query))) AS gv_hash, "
        "ANY_VALUE(query) AS gv_query, COUNT(*) AS gv_calls, COUNT(DISTINCT query) AS gv_variants, "
        "SUM(IFNULL(total_bytes_billed, 0)) AS gv_bytes, MIN(IFNULL(total_bytes_billed, 0)) AS gv_bytes_min, "
        "MAX(IFNULL(total_bytes_billed, 0)) AS gv_bytes_max, "
        "SUM(TIMESTAMP_DIFF(end_time, start_time, MILLISECOND)) / 1000 AS gv_runtime_s, "
        "SUM(IFNULL(total_slot_ms, 0)) AS gv_slot_ms "
        f"FROM `{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
        f"WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(days)} DAY) "
        "AND job_type = 'QUERY' AND IFNULL(statement_type, '') <> 'SCRIPT' AND state = 'DONE' "
        "AND error_result IS NULL "
        f"GROUP BY gv_hash ORDER BY gv_bytes DESC LIMIT {HISTORY_ROWS}"
    )


def read_bigquery_jobs(settings: Settings, conn_name: str, engine: Engine, days: int) -> History:
    location = engine.params.get("region") or engine.params.get("location")
    sql = bigquery_history_sql(location, days)
    try:
        res = _gated_run(settings, conn_name, engine, sql, "top", preview_rows=HISTORY_ROWS)
    except Blocked as blocked:
        err = str(blocked.result.get("error") or "")
        if blocked.result.get("status") == "error":
            raise SourceError(_error(
                f"Could not read INFORMATION_SCHEMA.JOBS_BY_PROJECT: {err}",
                "The role needs bigquery.jobs.listAll on the project (e.g. roles/bigquery.resourceViewer), and "
                "the connection's location must match where the jobs ran (set location = 'EU' etc.)."))
        raise
    entries = []
    for row in _lowered(res["rows"]):
        billed = row.get("gv_bytes")
        entries.append(Entry(
            sql=str(row.get("gv_query") or ""), calls=int(row.get("gv_calls") or 0),
            bytes=int(billed) if billed is not None else None,
            bytes_min=_int(row.get("gv_bytes_min")), bytes_max=_int(row.get("gv_bytes_max")),
            runtime_s=_float(row.get("gv_runtime_s")),
            cost_usd=engine.cost_for(int(billed), None) if billed is not None else None,
            engine_hash=row.get("gv_hash"), variants=_int(row.get("gv_variants"))))
    price = getattr(engine, "price_per_tib", None)
    notes = [f"Cost is bytes billed x the on-demand price ({price} USD/TiB). With capacity (slot) pricing the "
             "money follows slot time instead; cached runs bill 0 bytes.",
             f"Jobs in region-{(location or 'US').lower()} only; set location on the connection for another region."]
    return History(entries, float(days), "bytes billed (on-demand price)", notes, float(res["cost_usd"] or 0))


def snowflake_history_sql(days: int, attribution: bool = True) -> str:
    size_case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in SNOWFLAKE_CREDITS_PER_HOUR.items())
    estimate = f"q.execution_time / 3600000.0 * CASE q.warehouse_size {size_case} ELSE 0 END"
    # a query without an attribution row (attribution lags up to 8 hours and skips very short queries) counts
    # with its estimate, so a group is never priced from only the attributed part of its runs
    credits = (f"SUM(COALESCE(a.credits_attributed_compute, {estimate})) AS gv_credits, "
               "COUNT(a.credits_attributed_compute) AS gv_attributed, " if attribution else
               "NULL AS gv_credits, NULL AS gv_attributed, ")
    join = ("LEFT JOIN SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY AS a ON a.query_id = q.query_id "
            if attribution else "")
    return (
        "SELECT q.query_parameterized_hash AS gv_hash, ANY_VALUE(q.query_text) AS gv_query, COUNT(*) AS gv_calls, "
        "COUNT(DISTINCT q.query_hash) AS gv_variants, SUM(q.bytes_scanned) AS gv_bytes, "
        "MIN(q.bytes_scanned) AS gv_bytes_min, MAX(q.bytes_scanned) AS gv_bytes_max, "
        "SUM(q.execution_time) / 1000 AS gv_runtime_s, " + credits +
        f"SUM({estimate}) AS gv_credits_est "
        "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY AS q " + join +
        f"WHERE q.start_time >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP()) "
        "AND q.execution_status = 'SUCCESS' AND q.warehouse_size IS NOT NULL "
        f"GROUP BY q.query_parameterized_hash ORDER BY gv_credits_est DESC LIMIT {HISTORY_ROWS}"
    )


def read_snowflake_history(settings: Settings, conn_name: str, engine: Engine, days: int) -> History:
    notes: list[str] = []
    try:
        res = _gated_run(settings, conn_name, engine, snowflake_history_sql(days), "top", preview_rows=HISTORY_ROWS)
    except Blocked as blocked:
        if blocked.result.get("status") != "error":
            raise
        notes.append("QUERY_ATTRIBUTION_HISTORY was not readable, so credits are estimated from execution time x "
                     "warehouse size.")
        try:
            res = _gated_run(settings, conn_name, engine, snowflake_history_sql(days, attribution=False), "top",
                             preview_rows=HISTORY_ROWS)
        except Blocked as again:
            if again.result.get("status") != "error":
                raise
            raise SourceError(_error(
                f"Could not read SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY: {again.result.get('error')}",
                "ACCOUNT_USAGE needs a privileged role: GRANT DATABASE ROLE SNOWFLAKE.GOVERNANCE_VIEWER (query "
                "history) and SNOWFLAKE.USAGE_VIEWER (attribution) to the connection's role, or IMPORTED "
                "PRIVILEGES on the SNOWFLAKE database.")) from None
    credit_usd = float(getattr(engine, "credit_usd", 3.0))
    entries = []
    attributed = 0
    for row in _lowered(res["rows"]):
        credits = _float(row.get("gv_credits"))
        covered = _int(row.get("gv_attributed"))
        if credits is not None and (covered is None or covered >= int(row.get("gv_calls") or 0)):
            attributed += 1   # every run of the group has attributed credits
        if credits is None:
            credits = _float(row.get("gv_credits_est"))
        entries.append(Entry(
            sql=str(row.get("gv_query") or ""), calls=int(row.get("gv_calls") or 0),
            bytes=_int(row.get("gv_bytes")), bytes_min=_int(row.get("gv_bytes_min")),
            bytes_max=_int(row.get("gv_bytes_max")), runtime_s=_float(row.get("gv_runtime_s")),
            cost_usd=credits * credit_usd if credits is not None else None,
            engine_hash=row.get("gv_hash"), variants=_int(row.get("gv_variants"))))
    basis = ("credits attributed per query" if attributed and attributed == len(entries) else
             "credits attributed per query where available, else execution time x warehouse size")
    notes += [f"Money = credits x {credit_usd} USD (credit_usd on the connection). Attributed credits leave out "
              "warehouse idle time.", "ACCOUNT_USAGE lags behind by up to 45 minutes (attribution up to 8 hours)."]
    return History(entries, float(days), basis, notes, float(res["cost_usd"] or 0))


_FILE_COLUMNS = {
    "sql": ("query", "sql", "query_text", "statement"),
    "calls": ("calls", "count", "executions", "runs"),
    "bytes": ("bytes", "bytes_scanned", "bytes_billed", "total_bytes_billed", "bytes_processed"),
    "runtime_s": ("runtime_s", "runtime", "elapsed_s", "seconds", "duration_s"),
    "cost_usd": ("cost_usd", "cost", "usd"),
    "engine_hash": ("hash", "query_hash", "fingerprint", "queryid"),
}


def read_file(path: str, days: float) -> History:
    target = Path(path).expanduser()
    if not target.is_file():
        raise SourceError(_error(f"File not found: {path}", "Give the path of a CSV or JSONL export."))
    text = target.read_text(encoding="utf-8-sig")   # Excel's "CSV UTF-8" starts with a byte order mark
    suffix = target.suffix.lower()
    rows: list[dict[str, Any]]
    try:
        if suffix in (".jsonl", ".ndjson"):
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        elif suffix == ".json":
            loaded = json.loads(text)
            rows = loaded if isinstance(loaded, list) else list(loaded.get("queries") or [])
        else:
            rows = list(csv.DictReader(io.StringIO(text, newline="")))  # keeps newlines inside quoted SQL
    except (json.JSONDecodeError, csv.Error, AttributeError) as exc:
        raise SourceError(_error(f"Could not read {path}: {exc}",
                                 "Use CSV with a header, or JSON lines, with at least a 'query' column.")) from None
    entries: list[Entry] = []
    skipped = 0
    for raw in rows:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        row = {str(k).strip().lower(): v for k, v in raw.items() if k is not None}
        got = {key: next((row[n] for n in names if row.get(n) not in (None, "")), None)
               for key, names in _FILE_COLUMNS.items()}
        if not got["sql"]:
            skipped += 1
            continue
        try:
            entries.append(Entry(sql=str(got["sql"]), calls=int(float(got["calls"])) if got["calls"] else 1,
                                 bytes=_int(got["bytes"]), runtime_s=_float(got["runtime_s"]),
                                 cost_usd=_float(got["cost_usd"]),
                                 engine_hash=str(got["engine_hash"]) if got["engine_hash"] else None))
        except (TypeError, ValueError, OverflowError):
            skipped += 1
    notes = [f"The file is taken to cover {days:g} day(s) (days); per-month numbers scale from that."]
    if skipped:
        notes.append(f"{skipped} row(s) skipped: no query text or numbers that do not parse.")
    if not entries and rows:
        raise SourceError(_error(f"No usable rows in {path}.", "The file needs a 'query' column (or sql)."))
    return History(entries, days, "from the file", notes)


def _lowered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Snowflake returns unquoted aliases in upper case."""
    return [{str(k).lower(): v for k, v in row.items()} for row in rows]


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))   # OverflowError for inf, ValueError for nan


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"not a finite number: {value!r}")
    return number


# -- analysis of one group: lint, safe rewrite, priced saving ----------------------------------
def _accuracy(before: dict[str, Any], after: dict[str, Any]) -> str:
    return EXACT if before.get("accuracy") == EXACT and after.get("accuracy") == EXACT else HEURISTIC


def analyze_group(group: Group, dialect: str, engine: Engine | None, month_factor: float | None) -> dict[str, Any]:
    from .fixer import rewrite

    statement = classify(group.sql, dialect)
    if statement.kind == "block" or statement.select is None:
        return {"kind": "block", "reason": statement.reason or "not a single SELECT",
                "note": "Gulliver only lints and rewrites SELECT and CREATE/INSERT ... SELECT."}
    out: dict[str, Any] = {"kind": statement.kind, "tables": statement.tables}
    infos: dict[str, dict] = {}
    if engine is not None:
        found, _ = _table_infos(engine, statement)
        infos = {k: v.as_dict(with_columns=True) for k, v in found.items()}
    findings = lint(statement.select, dialect, infos or None)
    out["lint"] = [{"rule": f.rule, "severity": f.severity, "name": f.name, "fix": f.fix} for f in findings]
    try:
        fixed = rewrite(group.sql, dialect, mode="safe", table_info=infos or None)
    except Exception as exc:  # a rewrite problem must not break the whole report
        out["fix_error"] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return out
    if not fixed.get("changes"):
        return out
    out["fix"] = {"fixed_sql": fixed["fixed_sql"], "same_result": fixed["same_result"],
                  "rules": sorted({c["rule"] for c in fixed["changes"]}),
                  "changes": [{"rule": c["rule"], "before": c["before"], "after": c["after"]}
                              for c in fixed["changes"]]}
    if engine is None:
        out["saving"] = {"status": "unknown", "reason": "no connection of this dialect to estimate before vs after"}
        return out
    estimates: dict[str, dict[str, Any]] = {}
    for label, text in (("before", group.sql), ("after", fixed["fixed_sql"])):
        st = classify(text, dialect)
        try:
            estimates[label] = engine.estimate(st.select.sql(dialect=dialect)).as_dict()
        except EngineError as exc:
            estimates[label] = {"error": str(exc)}
    out["estimate"] = estimates
    out["saving"] = price_saving(group, estimates["before"], estimates["after"], month_factor)
    return out


def price_saving(group: Group, before: dict[str, Any], after: dict[str, Any],
                 month_factor: float | None) -> dict[str, Any]:
    if before.get("error") or after.get("error"):
        return {"status": "unknown", "reason": "the engine could not estimate the query: "
                + str(before.get("error") or after.get("error"))}
    ratio = basis = None
    if before.get("bytes") and after.get("bytes") is not None:
        ratio, basis = after["bytes"] / before["bytes"], "bytes the engine expects to scan, before vs after"
    elif before.get("cost_usd") and after.get("cost_usd") is not None:
        ratio, basis = after["cost_usd"] / before["cost_usd"], "estimated cost, before vs after"
    if ratio is None:
        return {"status": "unknown", "reason": "the estimate has no bytes or cost to compare"}
    share = max(0.0, min(1.0, 1.0 - ratio))
    out: dict[str, Any] = {"status": "ok", "scan_saved_pct": round(share * 100, 1), "basis": basis,
                           "accuracy": _accuracy(before, after)}
    if group.cost_usd:
        out["observed_usd"] = round(share * group.cost_usd, 6)
    if month_factor is not None:
        if group.cost_usd:
            out["per_month_usd"] = round(share * group.cost_usd * month_factor, 2)
        if group.runtime_s:
            out["runtime_s_per_month"] = round(share * group.runtime_s * month_factor, 1)
            out["runtime_accuracy"] = HEURISTIC  # runtime is assumed to fall with the bytes scanned
    if share <= 0:
        out["note"] = ("The estimate does not change here (no partitions, or the engine already prunes); the "
                       "rewrite matters on partitioned tables, indexes and Parquet files.")
    return out


# -- repeated queries worth a summary table -----------------------------------------------------
def materialize_candidates(groups: list[Group], dialect: str, window_days: float | None,
                           min_calls_per_day: float, refresh_per_day: float) -> list[dict[str, Any]]:
    if not window_days or window_days < MIN_PROJECTION_DAYS:
        return []
    out = []
    for g in groups:
        per_day = g.calls / window_days
        if per_day < min_calls_per_day or per_day <= refresh_per_day:
            continue
        if g.spread_known and g.bytes_min is not None and g.bytes_max is not None and g.bytes_max > 1.5 * max(
                g.bytes_min, 1):
            continue  # each run scans a different amount: not the same work repeated
        statement = classify(g.sql, dialect)
        outer = _outer_select(statement.select) if statement.kind == "read" and statement.select is not None else None
        if outer is None or not _has_aggregation(outer):
            continue  # only aggregates: reading a small summary must be much cheaper than the scan
        runs_per_month = per_day * DAYS_PER_MONTH
        refreshes = refresh_per_day * DAYS_PER_MONTH
        item: dict[str, Any] = {"fingerprint": g.fingerprint, "calls": g.calls, "calls_per_day": round(per_day, 1),
                                "accuracy": HEURISTIC, "tables": statement.tables}
        if g.cost_usd:
            per_run = g.cost_usd / g.calls
            item["cost_per_month_usd"] = round(per_run * runs_per_month, 2)
            item["refresh_cost_per_month_usd"] = round(per_run * refreshes, 2)
            item["saving_per_month_usd"] = round(per_run * (runs_per_month - refreshes), 2)
        if g.runtime_s:
            per_run_s = g.runtime_s / g.calls
            item["runtime_s_per_month"] = round(per_run_s * runs_per_month, 1)
            item["runtime_s_saved_per_month"] = round(per_run_s * (runs_per_month - refreshes), 1)
        if "saving_per_month_usd" not in item and "runtime_s_saved_per_month" not in item:
            continue
        item["suggestion"] = (f"Build this aggregate once as a summary table or materialized view, refresh it "
                              f"{refresh_per_day:g}x a day, and point the {per_day:.0f} daily runs at it.")
        notes = ["Assumes one refresh costs about one run and reading the summary is close to free; results are "
                 "only as fresh as the last refresh."]
        if g.variants > 1:
            notes.append(f"{g.variants} literal variants: keep the filtered columns as GROUP BY keys of the "
                         "summary so every variant can read it; such a refresh can scan more than one run does.")
        if not g.spread_known:
            notes.append("The source does not say whether every run scans the same data.")
        item["notes"] = notes
        item["sql"] = _clip(g.sql)
        out.append(item)
    out.sort(key=lambda i: (i.get("saving_per_month_usd") or 0, i.get("runtime_s_saved_per_month") or 0),
             reverse=True)
    return out


# -- the command -----------------------------------------------------------------------------------
def _clip(sql: str) -> str:
    sql = sql.strip()
    return sql if len(sql) <= MAX_SQL_CHARS else sql[:MAX_SQL_CHARS] + " ..."


def _metric(g: Group, rank_by: str) -> float:
    return float({"cost": g.cost_usd, "runtime": g.runtime_s, "bytes": g.bytes, "calls": g.calls}[rank_by] or 0)


def _resolve_source(source: str, path: str | None, engine_name: str) -> tuple[str, str | None]:
    for prefix in ("csv:", "jsonl:", "json:", "file:"):
        if source.startswith(prefix):
            return "file", source[len(prefix):]
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)} (or csv:FILE / jsonl:FILE)")
    if source == "auto":
        if path:
            return "file", path
        return {"postgres": "pg_stat_statements", "bigquery": "bigquery_jobs",
                "snowflake": "snowflake_history"}.get(engine_name, "receipts"), None
    if source == "file" and not path:
        raise ValueError("source 'file' needs a path (CSV, JSONL or JSON).")
    return source, path


def top(settings: Settings, source: str = "auto", connection: str | None = None, days: int = 7,
        limit: int = 20, analyze: int = 10, rank_by: str = "auto", path: str | None = None,
        dialect: str | None = None, min_calls_per_day: float = 10.0, refresh_per_day: float = 1.0) -> dict[str, Any]:
    """The most expensive repeated queries, with a priced, same-rows rewrite for the top ones."""
    if rank_by not in RANK_BY:
        return _error(f"rank_by must be one of {', '.join(RANK_BY)}")
    if days <= 0 or days > 365:
        return _error("days must be between 1 and 365.")
    if limit < 1:
        return _error("--limit must be at least 1.", "Use a positive limit, for example --limit 20.")
    if analyze < 0:
        return _error("--analyze must be at least 0.", "Use 0 to list the top queries without analysis.")
    try:
        conn = settings.connection(connection)
        kind, file_path = _resolve_source(source.strip(), path, conn.engine)
    except (ConfigError, ValueError) as exc:
        return _error(str(exc))
    needs = {"pg_stat_statements": "postgres", "bigquery_jobs": "bigquery", "snowflake_history": "snowflake"}
    if kind in needs and conn.engine != needs[kind]:
        return _error(f"source {kind} needs a {needs[kind]} connection; '{conn.name}' is {conn.engine}.",
                      "Pick the right connection with connection=..., or use source='receipts' or a file.")
    conn_dialect = dialect_for(conn.engine)
    dialect = dialect if (dialect and kind == "file") else conn_dialect  # history is in the engine's own dialect
    result: dict[str, Any] = {"action": "top", "source": kind, "connection": conn.name, "engine": conn.engine,
                              "dialect": dialect}
    if file_path:
        result["path"] = file_path
    notes: list[str] = []
    try:
        with ExitStack() as stack:
            engine: Engine | None = None
            if kind in needs or dialect == conn_dialect:
                try:
                    _, engine = stack.enter_context(_engine(settings, conn.name))
                except EngineError as exc:
                    if kind in needs:
                        raise
                    notes.append(f"No estimates: could not open connection {conn.name}: {exc}")
            elif kind == "file":
                notes.append(f"The file's dialect ({dialect}) differs from connection {conn.name} ({conn.engine}): "
                             "lint and rewrite only, no estimates.")
            if kind == "receipts":
                history = read_receipts(settings, conn.name, float(days))
            elif kind == "file":
                history = read_file(str(file_path), float(days))
            elif kind == "pg_stat_statements":
                history = read_pg_stat_statements(settings, conn.name, engine)
            elif kind == "bigquery_jobs":
                history = read_bigquery_jobs(settings, conn.name, engine, int(days))
            else:
                history = read_snowflake_history(settings, conn.name, engine, int(days))
            result.update(_report(history, dialect, engine, limit, analyze, rank_by, min_calls_per_day,
                                  refresh_per_day))
    except SourceError as exc:
        return {**result, **exc.result}
    except Blocked as blocked:
        return {**result, **blocked.result}
    except (EngineError, ConfigError) as exc:
        return {**result, **_error(str(exc))}
    if notes:
        result["notes"] = notes + result.get("notes", [])
    return result


def _report(history: History, dialect: str, engine: Engine | None, limit: int, analyze: int, rank_by: str,
            min_calls_per_day: float, refresh_per_day: float) -> dict[str, Any]:
    groups = group_entries(history.entries, dialect)
    if rank_by == "auto":
        rank_by = "cost" if any((g.cost_usd or 0) > 0 for g in groups) else "runtime"
    groups.sort(key=lambda g: (_metric(g, rank_by), g.calls), reverse=True)
    window = history.window_days
    factor = DAYS_PER_MONTH / window if window and window >= MIN_PROJECTION_DAYS else None
    totals: dict[str, Any] = {
        "queries": len(history.entries), "groups": len(groups), "calls": sum(g.calls for g in groups),
        "cost_usd": round(sum(g.cost_usd or 0 for g in groups), 6),
        "runtime_s": round(sum(g.runtime_s or 0 for g in groups), 3),
        "bytes": sum(g.bytes or 0 for g in groups),
    }
    if factor:
        totals["cost_per_month_usd"] = round(totals["cost_usd"] * factor, 2)
    out: dict[str, Any] = {"window_days": round(window, 3) if window else None, "rank_by": rank_by,
                           "cost_basis": history.cost_basis, "totals": totals,
                           "history_query_cost_usd": round(history.history_cost_usd, 6)}
    total_metric = sum(_metric(g, rank_by) for g in groups) or 0.0
    ranked = []
    for i, g in enumerate(groups[: max(limit, 0)], start=1):
        item: dict[str, Any] = {"rank": i, "fingerprint": g.fingerprint, "calls": g.calls, "cost_usd": g.cost_usd,
                                "runtime_s": round(g.runtime_s, 3) if g.runtime_s is not None else None,
                                "bytes": g.bytes, "variants": g.variants,
                                "share_pct": round(100 * _metric(g, rank_by) / total_metric, 1) if total_metric else None}
        if g.cost_usd is not None:
            item["cost_usd"] = round(g.cost_usd, 6)
        if window:
            item["calls_per_day"] = round(g.calls / window, 1)
        if factor and g.cost_usd:
            item["cost_per_month_usd"] = round(g.cost_usd * factor, 2)
        item["sql"] = _clip(g.sql)
        if i <= analyze:
            item["analysis"] = analyze_group(g, dialect, engine, factor)
        ranked.append(item)
    out["top"] = ranked
    out["materialize"] = materialize_candidates(groups, dialect, window, min_calls_per_day, refresh_per_day)[:limit]
    fixed = {t["fingerprint"] for t in ranked if (t.get("analysis") or {}).get("fix")}
    for m in out["materialize"]:
        if m["fingerprint"] in fixed:
            m["notes"].append("This query also has a same-rows fix in top; the two savings overlap, do not add "
                              "them up.")
    out["notes"] = history.notes + ["Fixes are computed on one example per group; the other literal variants "
                                    "have the same shape."]
    if window and not factor:
        out["notes"].append(f"Only {window * 24:.1f} hour(s) of history: too short to project per month or to "
                            "suggest summary tables. Run top again after at least a day.")
    if factor:
        out["notes"].append(f"Per-month numbers assume the next 30 days repeat the {round(window, 1):g} day(s) that were "
                            "read. 'exact' means the before/after estimates are the engine's own, not that the "
                            "future workload is known.")
    out.update(_verdict(out, history))
    return out


def _verdict(out: dict[str, Any], history: History) -> dict[str, Any]:
    if not out["top"]:
        return {"status": "ok", "next_step": "No queries found in this source and window. Widen days, or use "
                                             "another source (receipts, pg_stat_statements, bigquery_jobs, "
                                             "snowflake_history or a CSV/JSONL file)."}
    fixes = [t for t in out["top"] if (t.get("analysis") or {}).get("fix")]
    priced = [t for t in fixes if (t["analysis"].get("saving") or {}).get("per_month_usd")]
    hinted = [t for t in out["top"] if any(f["severity"] in ("warn", "error")
                                           for f in (t.get("analysis") or {}).get("lint") or [])]
    parts: list[str] = []
    if priced:
        best = max(priced, key=lambda t: t["analysis"]["saving"]["per_month_usd"])
        s = best["analysis"]["saving"]
        parts.append(f"Biggest priced fix: #{best['rank']} saves about {_fmt_usd(s['per_month_usd'])}/month "
                     f"({s['accuracy']}). Its fixed_sql returns exactly the same rows; preflight it and swap it in "
                     "where the query is defined.")
    elif fixes:
        flat = all((t["analysis"].get("saving") or {}).get("scan_saved_pct") == 0 for t in fixes)
        parts.append(f"{len(fixes)} top quer{'y has' if len(fixes) == 1 else 'ies have'} a same-rows rewrite "
                     "(analysis.fix.fixed_sql); "
                     + ("the engine's estimate shows no scan change here, so the saving is not proven (it shows "
                        "on partitioned tables, indexes and Parquet files)." if flat else
                        "the saving could not be priced, see analysis.saving."))
    if out["materialize"]:
        parts.append(f"{len(out['materialize'])} repeated aggregate(s) could read a summary table instead "
                     "(see materialize; heuristic).")
    if hinted and not fixes:
        parts.append("No safe automatic rewrite; the lint hints in analysis.lint need a human decision.")
    if not parts:
        parts.append("No scale problems found in the top queries.")
    status = "warn" if (fixes or hinted or out["materialize"]) else "ok"
    return {"status": status, "next_step": " ".join(parts)}


# -- text output for the CLI -------------------------------------------------------------------------
def render_text(result: dict[str, Any]) -> str:
    if result.get("status") == "error" or "top" not in result:
        return f"error: {result.get('error', '')}\nnext: {result.get('next_step', '')}"
    totals = result["totals"]
    window = result.get("window_days")
    lines = [f"{totals['groups']} query shapes ({totals['calls']:,} calls) from {result['source']}"
             + (f", last {round(window, 1):g} day(s)" if window else "") + f", ranked by {result['rank_by']}."]
    if totals.get("cost_per_month_usd"):
        lines[0] += f" About {_fmt_usd(totals['cost_per_month_usd'])}/month in total."
    lines.append("")
    lines.append(f"{'#':>3} {'calls':>8} {'per month':>12} {'runtime':>9} {'share':>6}  {'saving/month':<22} sql")
    for t in result["top"]:
        analysis = t.get("analysis") or {}
        saving = analysis.get("saving") or {}
        if saving.get("scan_saved_pct") == 0:
            save = "fix, no scan change"
        elif saving.get("per_month_usd") is not None:
            save = f"{_fmt_usd(saving['per_month_usd'])} {saving['accuracy']}"
        elif saving.get("runtime_s_per_month") is not None:
            save = f"{saving['runtime_s_per_month']:,.0f}s heuristic"
        elif analysis.get("fix"):
            save = "fix, not priced"
        else:
            save = "-"
        month = _fmt_usd(t["cost_per_month_usd"]) if t.get("cost_per_month_usd") else "-"
        runtime = f"{t['runtime_s']:,.1f}s" if t.get("runtime_s") is not None else "-"
        share = f"{t['share_pct']:.0f}%" if t.get("share_pct") is not None else "-"
        sql = " ".join(t["sql"].split())
        lines.append(f"{t['rank']:>3} {t['calls']:>8,} {month:>12} {runtime:>9} {share:>6}  {save:<22} "
                     f"{sql[:70] + ('...' if len(sql) > 70 else '')}")
    for t in result["top"]:
        analysis = t.get("analysis") or {}
        if analysis.get("fix"):
            lines += ["", f"-- #{t['rank']} {', '.join(analysis['fix']['rules'])}: same rows"
                      + (f", scans {analysis['saving']['scan_saved_pct']:g}% less"
                         if (analysis.get("saving") or {}).get("scan_saved_pct") is not None else ""),
                      analysis["fix"]["fixed_sql"]]
        elif any(f["severity"] in ("warn", "error") for f in analysis.get("lint") or []):
            hints = "; ".join(f"{f['rule']} {f['name']}" for f in analysis["lint"] if f["severity"] != "info")
            lines += ["", f"-- #{t['rank']} needs a human decision: {hints}"]
    if result.get("materialize"):
        lines += ["", "Repeated aggregates that could read a summary table (heuristic):"]
        for m in result["materialize"]:
            saved = (f"saves ~{_fmt_usd(m['saving_per_month_usd'])}/month" if m.get("saving_per_month_usd")
                     else f"saves ~{m.get('runtime_s_saved_per_month', 0):,.0f}s/month")
            lines.append(f"  {m['fingerprint']}  {m['calls_per_day']:g} calls/day  {saved}  "
                         f"{' '.join(m['sql'].split())[:60]}")
    if totals.get("bytes"):
        lines += ["", f"scanned in total: {_fmt_bytes(totals['bytes'])}"]
    lines += ["", f"next: {result.get('next_step', '')}"]
    return "\n".join(lines)
