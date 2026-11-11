"""Auto-fix: rewrite SQL so it is cheaper on big data, and say exactly what each rewrite changes.

Agents get lint findings, but small models often fail to rewrite the SQL themselves. `gulliver_fix`
does the rewrite and labels every change with a guarantee:

  same     the rewritten query returns exactly the same rows. Applied by default, and only when
           Gulliver knows the column types from the connection's metadata.
  assumes  same rows IF the column is a DATE or a TIMESTAMP without time zone (the type was not known)
                                                                       -> only with mode="all"
  nulls    same rows, unless a NOT IN subquery returns NULL (then the original returns nothing,
           which is almost never what was meant) or the outer column is NULL
                                                                       -> only with mode="all"
  changes  the result changes on purpose (LIMIT, approximate count, UNION ALL)
                                                                       -> only with mode="all"

With a connection it also estimates the original and the fixed query (free: dry run / EXPLAIN),
so the agent and the human see the saving instead of trusting it. With verify=True every change is
also tested on generated edge-case data (verify.py); labels never change because of a test.

Why the date rewrite needs the type: on text columns a range compares strings, not dates; on
TIMESTAMP WITH TIME ZONE a day can start twice (DST at midnight) or per-row offsets apply; on
INTERVAL columns the range is not even valid SQL. So "same" is only claimed for DATE, TIMESTAMP
(without zone), DATETIME and BigQuery TIMESTAMP (always UTC).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from sqlglot import exp

from .lint import _has_aggregation, _outer_select, lint, summarize
from .sqlsafety import Statement, classify, table_ref

SAME = "same"
ASSUMES = "assumes"
NULLS = "nulls"
CHANGES = "changes"
GUARANTEE_TEXT = {
    SAME: "returns exactly the same rows",
    ASSUMES: "same rows if the column is a DATE or a TIMESTAMP without time zone",
    NULLS: "same rows unless a NULL is involved (in the subquery, or in the outer column)",
    CHANGES: "changes the result on purpose",
}

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ZONED = re.compile(r"WITH TIME ZONE|(?<!N)TZ")  # TIMESTAMPTZ, TIMESTAMP_TZ, TIMESTAMP_LTZ; not TIMESTAMP_NTZ


class _Log(list):
    """The changes a rewrite applies. `offer` numbers every candidate change in a fixed order and applies it
    unless its number is in `skip`, so a verifier can build the query with one change only, or without one."""

    def __init__(self, skip: frozenset[int] = frozenset()):
        super().__init__()
        self.skip = skip
        self.offered: list[Change] = []

    def offer(self, change: Change) -> bool:
        self.offered.append(change)
        if len(self.offered) - 1 in self.skip:
            return False
        self.append(change)
        return True


@dataclass
class Change:
    rule: str
    guarantee: str
    before: str
    after: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["meaning"] = GUARANTEE_TEXT[self.guarantee]
        return out


# -- column types ---------------------------------------------------------------------------
def safe_date_type(ctype: str) -> bool:
    """DATE, TIMESTAMP without zone, DATETIME, TIMESTAMP_NTZ, BigQuery TIMESTAMP (UTC)."""
    t = " ".join(ctype.upper().split())
    if _ZONED.search(t):
        return False
    return t == "DATE" or t.startswith("TIMESTAMP") or t.startswith("DATETIME")


def _from(select: exp.Select) -> exp.From | None:
    return select.args.get("from_") or select.args.get("from")  # the key is "from_" in newer sqlglot


def _sources(select: exp.Select, dialect: str, cte_names: set[str]) -> dict[str, str | None]:
    """Alias (lower case) -> base table ref (lower case), or None for a derived source (CTE, subquery ...)."""
    out: dict[str, str | None] = {}
    items: list[exp.Expression] = []
    if _from(select) is not None:
        items.append(_from(select).this)
    items.extend(join.this for join in select.args.get("joins") or [])
    for item in items:
        alias = (item.alias_or_name or "").lower()
        if isinstance(item, exp.Table) and isinstance(item.this, exp.Identifier):
            plain = item.name.lower() in cte_names and not item.args.get("db") and not item.args.get("catalog")
            out[alias] = None if plain else table_ref(item, dialect).lower()
        else:
            out[alias or f"_derived{len(out)}"] = None
    return out


def _type_resolver(select: exp.Select, dialect: str, cte_names: set[str],
                   tables: dict[str, dict[str, str]]) -> Callable[[exp.Column], str | None]:
    """Find a column's type only when it surely comes from one base table with metadata."""
    sources = _sources(select, dialect, cte_names)

    def resolve(col: exp.Column) -> str | None:
        name = col.name.lower()
        if col.table:
            key = col.table.lower()
            if key not in sources:
                return None  # an outer (correlated) reference: do not guess
            ref = sources[key]
            return tables.get(ref, {}).get(name) if ref else None
        if any(ref is None for ref in sources.values()):
            return None  # a CTE or subquery could define a column with this name
        if any(ref not in tables for ref in sources.values()):
            return None  # a table without metadata could have it
        found = [tables[ref][name] for ref in sources.values() if name in tables[ref]]
        return found[0] if len(found) == 1 else None

    return resolve


def table_columns(table_info: dict[str, dict] | None) -> dict[str, dict[str, str]]:
    """Table ref (lower case) -> {column (lower case): type} from engine metadata."""
    out: dict[str, dict[str, str]] = {}
    for ref, info in (table_info or {}).items():
        cols = info.get("columns")
        if cols:
            out[ref.lower()] = {str(c.get("name", "")).lower(): str(c.get("type", "")) for c in cols}
    return out


# -- date range filters (GV005) ----------------------------------------------------------
def _unit_of(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    text = node.name if isinstance(node, (exp.Var, exp.Literal)) else node.sql()
    text = text.strip("'\"").upper()
    return {"DAY": "day", "MONTH": "month", "QUARTER": "quarter", "YEAR": "year"}.get(text)


def _truncation(node: exp.Expression) -> tuple[exp.Column, str, str] | None:
    """`DATE(ts)`, `ts::date`, `TO_DATE(ts)`, `DATE_TRUNC('month', ts)` -> (ts, unit, "date");
    `YEAR(ts)`, `EXTRACT(YEAR FROM ts)` -> (ts, "year", "number")."""
    if isinstance(node, exp.Date) and isinstance(node.this, exp.Column) and not any(
            v for k, v in node.args.items() if k != "this"):
        return node.this, "day", "date"
    if (isinstance(node, exp.TsOrDsToDate) and isinstance(node.this, exp.Column)
            and not node.args.get("format") and not node.args.get("safe")):
        return node.this, "day", "date"
    if isinstance(node, exp.Cast) and not isinstance(node, exp.TryCast) and isinstance(node.this, exp.Column):
        to = node.args.get("to")
        if isinstance(to, exp.DataType) and to.this == exp.DataType.Type.DATE and not node.args.get("format"):
            return node.this, "day", "date"
    if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc, exp.DatetimeTrunc)) and isinstance(node.this, exp.Column):
        if node.args.get("zone"):
            return None
        unit = _unit_of(node.args.get("unit"))
        if unit:
            return node.this, unit, "date"
    if isinstance(node, exp.Year) and isinstance(node.this, exp.Column):
        return node.this, "year", "number"
    if (isinstance(node, exp.Extract) and isinstance(node.expression, exp.Column)
            and _unit_of(node.this) == "year"):
        return node.expression, "year", "number"
    return None


def _literal_start(node: exp.Expression | None, unit: str, kind: str) -> date | None:
    """The first day of the period the literal names; None if it is not a clean literal of the right kind."""
    if node is None:
        return None
    if kind == "number":  # YEAR(ts) = 2024: an integer, nothing else
        if not isinstance(node, exp.Literal) or node.is_string:
            return None
        try:
            year = int(node.name)
        except ValueError:
            return None
        return date(year, 1, 1) if 1 <= year <= 9998 else None
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal):
        to = node.args.get("to")
        if not (isinstance(to, exp.DataType) and to.this == exp.DataType.Type.DATE):
            return None
        node = node.this
    if isinstance(node, exp.Date) and isinstance(node.this, exp.Literal):
        node = node.this
    if not isinstance(node, exp.Literal) or not node.is_string or not _ISO_DAY.match(node.name):
        return None
    try:
        day = date.fromisoformat(node.name)
    except ValueError:
        return None
    if unit == "month" and day.day != 1:
        return None
    if unit == "quarter" and (day.day != 1 or day.month not in (1, 4, 7, 10)):
        return None
    if unit == "year" and (day.month, day.day) != (1, 1):
        return None
    return day


def _next(start: date, unit: str) -> date | None:
    """The first day of the next period, or None past year 9999 (then no rewrite)."""
    try:
        if unit == "day":
            return date.fromordinal(start.toordinal() + 1)
        if unit == "month":
            return date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        if unit == "quarter":
            return date(start.year + (start.month == 10), (start.month + 2) % 12 + 1, 1)
        return date(start.year + 1, 1, 1)
    except (ValueError, OverflowError):
        return None


FLIP = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ}


def _range(col: exp.Column, low: date | None, high: date | None) -> exp.Expression:
    parts: list[exp.Expression] = []
    if low is not None:
        parts.append(exp.GTE(this=col.copy(), expression=exp.Literal.string(low.isoformat())))
    if high is not None:
        parts.append(exp.LT(this=col.copy(), expression=exp.Literal.string(high.isoformat())))
    if len(parts) == 1:
        return parts[0]
    return exp.Paren(this=exp.And(this=parts[0], expression=parts[1]))


def _filter_only(cmp: exp.Expression, stop: exp.Expression) -> bool:
    """True when only AND, OR and brackets sit between `cmp` and its WHERE. There NULL and FALSE keep the same
    rows; NOT, IS NULL, COALESCE, = FALSE, CASE ... can tell them apart."""
    node = cmp.parent
    while node is not None and node is not stop:
        if not isinstance(node, (exp.And, exp.Or, exp.Paren)):
            return False
        node = node.parent
    return node is stop


def _rewrite_range(cmp: exp.Expression, where: exp.Where, dialect: str,
                   resolve: Callable[[exp.Column], str | None]) -> tuple[exp.Expression, exp.Column, str] | None:
    """Return (replacement, column, unit) for a comparison on a truncated date column, else None."""
    two_sided = True
    if isinstance(cmp, exp.Between):
        if cmp.args.get("symmetric"):
            return None  # BETWEEN SYMMETRIC swaps the bounds; keep it as it is
        trunc = _truncation(cmp.this)
        if not trunc:
            return None
        col, unit, kind = trunc
        low = _literal_start(cmp.args.get("low"), unit, kind)
        high = _literal_start(cmp.args.get("high"), unit, kind)
        if low is None or high is None:
            return None
        bounds = (low, _next(high, unit))
    else:
        op = type(cmp)
        if op not in FLIP:
            return None
        left, right = cmp.this, cmp.args.get("expression")
        trunc = _truncation(left)
        literal = right
        if not trunc:
            trunc = _truncation(right) if right is not None else None
            literal = left
            op = FLIP[op]
        if not trunc:
            return None
        col, unit, kind = trunc
        start = _literal_start(literal, unit, kind)
        if start is None:
            return None
        two_sided = op is exp.EQ
        bounds = {
            exp.EQ: (start, _next(start, unit)),
            exp.GTE: (start, None),
            exp.GT: (_next(start, unit), None),
            exp.LT: (None, start),
            exp.LTE: (None, _next(start, unit)),
        }[op]
    if bounds == (None, None) or (two_sided and None in bounds):
        return None  # the next period would start after year 9999
    if dialect == "duckdb" and kind == "number" and (not two_sided or not _filter_only(cmp, where)):
        return None  # DuckDB: YEAR('infinity') is NULL, a range on 'infinity' is not (TRUE one-sided, else FALSE)
    return _range(col, *bounds), col, unit


def _fix_ranges(tree: exp.Expression, dialect: str, tables: dict[str, dict[str, str]], mode: str,
                changes: _Log) -> None:
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for select in list(tree.find_all(exp.Select)):
        where = select.args.get("where")
        if where is None:
            continue
        resolve = _type_resolver(select, dialect, cte_names, tables)
        for cmp in list(where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
            if cmp.find_ancestor(exp.Select) is not select:
                continue  # belongs to a subquery; handled when we visit that select
            out = _rewrite_range(cmp, where, dialect, resolve)
            if out is None:
                continue
            repl, col, _unit = out
            ctype = resolve(col)
            if ctype is not None and not safe_date_type(ctype):
                continue  # text, TIMESTAMP WITH TIME ZONE, INTERVAL ...: not the same filter
            guarantee = SAME if ctype is not None else ASSUMES
            if guarantee == ASSUMES and mode != "all":
                continue
            if isinstance(repl, exp.Paren) and isinstance(cmp.parent, (exp.Where, exp.And)):
                repl = repl.this  # AND inside AND/WHERE needs no brackets
            before = cmp.sql(dialect=dialect)
            note = f"Filters the raw column {col.sql(dialect=dialect)} by range, so partitions, indexes and min/max " \
                   "statistics can skip data."
            if guarantee == SAME:
                note += f" {col.sql(dialect=dialect)} is {ctype}, so the rows are the same."
            else:
                note += (f" The type of {col.sql(dialect=dialect)} is unknown: only use this if it is a DATE or a "
                         "TIMESTAMP without time zone (not text, not with time zone).")
            if changes.offer(Change("GV005", guarantee, before, repl.sql(dialect=dialect), note)):
                cmp.replace(repl)


# -- NOT IN (subquery) -> NOT EXISTS (GV012) ---------------------------------------------
def _fix_not_in(tree: exp.Expression, dialect: str, changes: _Log) -> None:
    for not_node in list(tree.find_all(exp.Not)):
        inner = not_node.this
        if not isinstance(inner, exp.In) or inner.args.get("query") is None:
            continue
        outer_col = inner.this
        query = inner.args["query"]
        sub = query.this if isinstance(query, exp.Subquery) else query
        if not (isinstance(outer_col, exp.Column) and outer_col.table and isinstance(sub, exp.Select)):
            continue
        if (len(sub.expressions) != 1 or sub.args.get("joins") or sub.args.get("group") or sub.args.get("having")
                or sub.args.get("limit") or sub.args.get("distinct") or _from(sub) is None):
            continue
        source = _from(sub).this
        projected = sub.expressions[0]
        if isinstance(projected, exp.Alias):
            projected = projected.this
        if not (isinstance(source, exp.Table) and isinstance(projected, exp.Column)):
            continue
        if projected.table and projected.table.lower() not in {source.name.lower(), source.alias_or_name.lower()}:
            continue  # the subquery returns an outer column (o.x): rebuilding it on the subquery table changes it
        alias = source.alias_or_name if source.alias else "gv_sub"
        if alias.lower() == outer_col.table.lower():
            continue  # same name inside and outside: the rewrite could bind to the wrong table
        new_sub = sub.copy()
        new_source = _from(new_sub).this
        if not new_source.alias:
            new_source.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        inner_col = exp.column(projected.name, table=alias)
        condition = exp.EQ(this=inner_col, expression=outer_col.copy())
        where = new_sub.args.get("where")
        new_sub.set("where", exp.Where(this=exp.and_(where.this, condition) if where is not None else condition))
        new_sub.set("expressions", [exp.Literal.number(1)])
        repl = exp.Not(this=exp.Exists(this=new_sub))
        before = not_node.sql(dialect=dialect)
        if changes.offer(Change("GV012", NULLS, before, repl.sql(dialect=dialect),
                                "NOT EXISTS plans as an anti-join. It differs only with NULLs: when the subquery "
                                "returns a NULL, NOT IN returns no rows at all (NOT EXISTS ignores the NULL); and a "
                                "row whose outer column is NULL is dropped by NOT IN when the subquery has rows, but "
                                "kept by NOT EXISTS.")):
            not_node.replace(repl)


# -- result-changing rewrites (opt-in) -----------------------------------------------------
def _fix_limit(tree: exp.Expression, dialect: str, limit: int, changes: _Log) -> None:
    outer = _outer_select(tree)
    if outer is None or outer.args.get("limit") or outer.args.get("fetch") or _has_aggregation(outer):
        return
    if changes.offer(Change("GV002", CHANGES, "(no LIMIT)", f"LIMIT {limit}",
                            f"Returns at most {limit} rows, for exploring. Note: on BigQuery a LIMIT does not lower "
                            "the bytes billed; filters and fewer columns do.")):
        outer.set("limit", exp.Limit(expression=exp.Literal.number(limit)))


def _fix_approx(tree: exp.Expression, dialect: str, changes: _Log) -> None:
    if dialect == "postgres":
        return  # no built-in approximate distinct count
    for count in list(tree.find_all(exp.Count)):
        distinct = count.this
        if not isinstance(distinct, exp.Distinct) or len(distinct.expressions) != 1:
            continue
        repl = exp.ApproxDistinct(this=distinct.expressions[0].copy())
        before = count.sql(dialect=dialect)
        if changes.offer(Change("GV007", CHANGES, before, repl.sql(dialect=dialect),
                                "Approximate distinct count (HyperLogLog): usually within about 1-2%, far less "
                                "memory.")):
            count.replace(repl)


def _fix_union(tree: exp.Expression, dialect: str, changes: _Log) -> None:
    for union in list(tree.find_all(exp.Union)):
        if union.args.get("distinct") and changes.offer(
                Change("GV009", CHANGES, "UNION", "UNION ALL",
                       "Keeps duplicate rows instead of sorting/hashing both sides to remove them. "
                       "Same result only if the two sides cannot overlap.")):
            union.set("distinct", False)


# -- driver -------------------------------------------------------------------------------
def rewrite(sql: str, dialect: str = "duckdb", mode: str = "safe", limit: int = 1000,
            table_info: dict[str, dict] | None = None) -> dict[str, Any]:
    """Rewrite one statement. Pure SQL work: no database needed.

    `table_info` maps lower-case table refs to engine metadata with `columns` ({name, type}); without it,
    date rewrites cannot be proven and are only offered in mode "all" (guarantee "assumes")."""
    return _rewrite(sql, dialect, mode, limit, table_info)[0]


def _rewrite(sql: str, dialect: str, mode: str, limit: int, table_info: dict[str, dict] | None,
             skip: frozenset[int] = frozenset()) -> tuple[dict[str, Any], list[Change]]:
    """rewrite(), leaving out the offered changes numbered in `skip`; also returns every offered change."""
    if mode not in ("safe", "all"):
        raise ValueError("mode must be 'safe' or 'all'")
    statement: Statement = classify(sql, dialect)
    if statement.kind == "block" or statement.tree is None or statement.select is None:
        return {"status": "block", "statement": statement.public(), "changes": [],
                "next_step": "Send a single SELECT (or CREATE/INSERT ... SELECT). " + statement.reason}, []
    before_findings = lint(statement.select, dialect, table_info)
    changes = _Log(skip)
    tree = statement.tree
    _fix_ranges(statement.select, dialect, table_columns(table_info), mode, changes)
    if mode == "all":
        _fix_not_in(statement.select, dialect, changes)
        _fix_approx(statement.select, dialect, changes)
        _fix_union(statement.select, dialect, changes)
        _fix_limit(statement.select, dialect, limit, changes)
    fixed_sql = tree.sql(dialect=dialect, pretty=True)
    after = classify(fixed_sql, dialect)
    after_findings = lint(after.select, dialect, table_info) if after.select is not None else []
    return {
        "statement": statement.public(),
        "fixed_sql": fixed_sql,
        "changes": [c.as_dict() for c in changes],
        "same_result": all(c.guarantee == SAME for c in changes),
        "lint_before": summarize(before_findings),
        "lint_after": summarize(after_findings),
        "remaining": [f.as_dict() for f in after_findings],
        "sql_hash_before": statement.sql_hash,
        "sql_hash_after": after.sql_hash,
    }, changes.offered


# -- with a connection: prove the saving --------------------------------------------------
def fix(settings: Any, sql: str, connection: str | None = None, dialect: str | None = None,
        mode: str = "safe", limit: int | None = None, verify: bool = False, cases: int | None = None,
        seed: int = 0, timezone: str | None = None) -> dict[str, Any]:
    """Rewrite the SQL and, when a connection is available, estimate both versions (no data is read).

    verify=True also tests every change on generated edge-case data (see verify.py)."""
    from .core import _engine, _error, _fmt_bytes, _fmt_usd, _table_infos
    from .engines import EngineError, dialect_for

    if mode == "all" and limit is not None and limit < 1:
        return _error(f"limit must be at least 1, got {limit}.", "Use --limit 1 or higher, or omit --limit.")
    conn = None
    if settings is not None:
        conn = settings.connection(connection)
        dialect = dialect or dialect_for(conn.engine)
        if dialect != dialect_for(conn.engine):
            conn = None  # different dialect than the connection: rewrite offline only
    dialect = dialect or "duckdb"
    max_rows = limit or (settings.budget.max_output_rows if settings is not None else 1000)

    infos: dict[str, dict] = {}
    notes: list[str] = []
    with ExitStack() as stack:
        engine = None
        if conn is not None:
            try:
                _, engine = stack.enter_context(_engine(settings, conn.name))
                statement = classify(sql, dialect)
                if statement.kind != "block":
                    found, table_notes = _table_infos(engine, statement)
                    infos = {k: v.as_dict(with_columns=True) for k, v in found.items()}
                    notes.extend(table_notes)
            except EngineError as exc:
                notes.append(f"metadata unavailable: {exc}")
                engine = None

        if verify:
            from . import verify as verifier

            result = verifier.verify_rewrite(sql, dialect, mode, max_rows, infos or None, seed=seed,
                                             cases=cases or verifier.DEFAULT_CASES, timezone=timezone)
        else:
            result = rewrite(sql, dialect, mode=mode, limit=max_rows, table_info=infos or None)
        result = {"action": "fix", "dialect": dialect, **result}
        if result.get("status") == "block":
            return result
        if engine is not None and result["changes"]:
            estimates: dict[str, Any] = {}
            for label, text in (("before", sql), ("after", result["fixed_sql"])):
                st = classify(text, dialect)
                try:
                    estimates[label] = engine.estimate(st.select.sql(dialect=dialect)).as_dict()
                except EngineError as exc:
                    estimates[label] = {"error": str(exc)}
            result["estimate"] = estimates
            saving = _saving(estimates.get("before") or {}, estimates.get("after") or {})
            if saving:
                result["saving"] = saving
                if saving.get("bytes_pct") is not None:
                    saving["summary"] = (f"scan {_fmt_bytes(saving['bytes_before'])} -> "
                                         f"{_fmt_bytes(saving['bytes_after'])} ({saving['bytes_pct']:.0f}% less)")
                    if engine.billed and saving.get("usd_before") is not None and saving.get("usd_after") is not None:
                        saving["summary"] += (f", cost {_fmt_usd(saving['usd_before'])} -> "
                                              f"{_fmt_usd(saving['usd_after'])}")
                    if saving["bytes_pct"] <= 0:
                        saving["summary"] += (". The estimate does not change here (no partitions, or the engine "
                                              "already handles this); the rewrite matters for partitioned tables, "
                                              "indexes and Parquet files.")
        if conn is not None:
            result["connection"] = conn.name

    if notes:
        result["notes"] = notes
    changes = result["changes"]
    remaining_errors = result["lint_after"]["error"]
    if not changes:
        result["status"] = "warn" if remaining_errors or result["lint_after"]["warn"] else "ok"
        result["next_step"] = ("Nothing could be rewritten with a same-rows guarantee. "
                               + ("Fix the remaining lint findings by hand (they need a decision, e.g. which date "
                                  "range or which join key)." if result["remaining"] else
                                  "The SQL has no scale problems Gulliver can fix. Call gulliver_preflight."))
        if mode == "safe" and _opt_in_rules(result["remaining"]):
            result["next_step"] += (" mode='all' offers rewrites that change the result slightly or need a column "
                                    "type Gulliver could not check.")
        return _verified(result)
    result["status"] = "warn" if remaining_errors else "ok"
    if result["same_result"]:
        result["next_step"] = "Use fixed_sql: it returns exactly the same rows. Call gulliver_preflight with it."
    else:
        result["next_step"] = ("Some rewrites are not proven to keep the rows (see changes[].meaning). Tell the human "
                               "what changed, check with gulliver_diff before relying on it, then gulliver_preflight "
                               "fixed_sql.")
    if remaining_errors:
        result["next_step"] += " Lint errors remain (see remaining); fix those by hand first."
    return _verified(result)


def _verified(result: dict[str, Any]) -> dict[str, Any]:
    if "verify" in result:
        from .verify import finish

        finish(result)
    return result


def _opt_in_rules(findings: list[dict[str, Any]]) -> bool:
    return any(f.get("rule") in ("GV002", "GV005", "GV007", "GV009", "GV012") for f in findings)


def _saving(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    b_bytes, a_bytes = before.get("bytes"), after.get("bytes")
    b_usd, a_usd = before.get("cost_usd"), after.get("cost_usd")
    if b_bytes is None and b_usd is None:
        return None
    out: dict[str, Any] = {"bytes_before": b_bytes, "bytes_after": a_bytes, "usd_before": b_usd, "usd_after": a_usd,
                           "accuracy": after.get("accuracy") or before.get("accuracy")}
    if b_bytes and a_bytes is not None:
        out["bytes_pct"] = round(100.0 * (b_bytes - a_bytes) / b_bytes, 1)
    if b_usd and a_usd is not None:
        out["usd_pct"] = round(100.0 * (b_usd - a_usd) / b_usd, 1)
    return out
