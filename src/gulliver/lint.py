"""Scale lint: SQL patterns that work on small data and hurt on big data.

Every rule has a stable id (GV001...), a severity and a concrete fix.
  error  blocks gulliver run (fix the SQL first)
  warn   allowed, but shown in the preflight and the receipt
  info   a cheaper alternative exists
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from .sqlsafety import base_tables, table_ref

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

RULES = {
    "GV001": "select-star",
    "GV002": "unbounded-output",
    "GV003": "cartesian-join",
    "GV004": "missing-partition-filter",
    "GV005": "function-on-filter-column",
    "GV006": "order-without-limit",
    "GV007": "exact-count-distinct",
    "GV008": "non-equi-join",
    "GV009": "union-distinct",
    "GV010": "scalar-subquery-in-select",
    "GV011": "window-without-partition",
    "GV012": "not-in-subquery",
    "GV013": "leading-wildcard-like",
    "GV014": "sensitive-column-in-output",
    "GV015": "fan-trap",
    "GV016": "order-by-random",
    "GV017": "deep-offset",
    "GV018": "count-for-existence",
}

DEEP_OFFSET_THRESHOLD = 10_000


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    fix: str
    node: Any = field(default=None, repr=False, compare=False)  # where it was found (for line/column); not output

    @property
    def name(self) -> str:
        return RULES.get(self.rule, self.rule)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "name": self.name, "severity": self.severity, "message": self.message,
                "fix": self.fix}


AGG_TYPES = (exp.AggFunc,)
COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike)


def _outer_select(tree: exp.Expression) -> exp.Select | None:
    if isinstance(tree, exp.Select):
        return tree
    if isinstance(tree, exp.Subquery):
        return _outer_select(tree.this)
    return None


def _has_aggregation(select: exp.Select) -> bool:
    """True when this SELECT collapses its own rows: a GROUP BY, or an aggregate it evaluates.

    An aggregate that belongs to a nested SELECT (a scalar subquery in the projection list, say) collapses
    that subquery, not this query: `select u.id, (select count(*) from orders) as n from users u` still
    returns one row per user."""
    if select.args.get("group"):
        return True
    for projection in select.expressions:
        for node in projection.walk():
            if isinstance(node, AGG_TYPES) and not node.find_ancestor(exp.Window) and (
                    node.find_ancestor(exp.Select) is select):
                return True
    return False


def _is_star_projection(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _join_condition(join: exp.Join) -> exp.Expression | None:
    return join.args.get("on") or (exp.true() if join.args.get("using") else None)


def _aliases_in(expr: exp.Expression) -> set[str]:
    return {col.table.lower() for col in expr.find_all(exp.Column) if col.table}


# reading `0 < (SELECT COUNT(*) ...)` as `(SELECT COUNT(*) ...) > 0`
_MIRRORED = {exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}


def _count_only_subquery(node: exp.Expression) -> exp.Subquery | None:
    """`(SELECT COUNT(*) ...)` or `(SELECT COUNT(1) ...)`, nothing else.

    Only these can become EXISTS: a grouped count returns one row per group, and
    COUNT(column) skips NULLs, so neither answers "does at least one row match?".
    """
    if not isinstance(node, exp.Subquery):
        return None
    select = node.this
    if not isinstance(select, exp.Select):
        return None
    if select.args.get("group") or select.args.get("having") or select.args.get("distinct"):
        return None
    projections = list(select.expressions)
    if len(projections) != 1 or not isinstance(projections[0], exp.Count):
        return None
    count = projections[0]
    if count.args.get("distinct"):
        return None
    arg = count.this
    if isinstance(arg, exp.Star):
        return node
    if isinstance(arg, exp.Literal) and arg.is_int and str(arg.this) == "1":
        return node
    return None


def _count_for_existence(node: exp.Expression) -> str | None:
    """`"exists"` / `"not exists"` when this comparison only asks whether a count is zero."""
    left, right = node.this, node.expression
    if _count_only_subquery(left) is not None:
        other, mirrored = right, False
    elif _count_only_subquery(right) is not None:
        other, mirrored = left, True
    else:
        return None
    if not isinstance(other, exp.Literal) or not other.is_int:
        return None
    try:
        value = int(other.this)
    except (TypeError, ValueError):
        return None
    op = type(node)
    if mirrored:
        op = _MIRRORED.get(op, op)
    if op is exp.GT and value == 0:      # count > 0
        return "exists"
    if op is exp.GTE and value == 1:     # count >= 1
        return "exists"
    if op is exp.NEQ and value == 0:     # count <> 0
        return "exists"
    if op is exp.EQ and value == 0:      # count = 0
        return "not exists"
    if op is exp.LT and value == 1:      # count < 1
        return "not exists"
    return None


def lint(statement_tree: exp.Expression, dialect: str = "duckdb",
         table_info: dict[str, dict] | None = None) -> list[Finding]:
    """Lint a read tree (the SELECT part). `table_info` maps lower-case table refs to engine
    metadata; when it says a table is partitioned, GV004 can check the filter."""
    tree = statement_tree
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(rule: str, severity: str, message: str, fix: str, node: exp.Expression | None = None) -> None:
        key = (rule, message)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(rule, severity, message, fix, node))

    outer = _outer_select(tree)

    # GV001 select-star (only where it reaches the output or a scan; COUNT(*) is fine;
    # an EXISTS subquery is a semi-join, no engine reads its columns)
    for select in tree.find_all(exp.Select):
        if select.find_ancestor(exp.Exists) is not None:
            continue
        stars = [p for p in select.expressions if _is_star_projection(p)]
        if stars:
            add("GV001", "warn",
                "SELECT * reads every column. Columnar engines (BigQuery, Snowflake, DuckDB on Parquet) "
                "charge and work per column.",
                "List only the columns you need.", stars[0])
            break

    # GV002 unbounded-output and GV006 order-without-limit on the outermost query
    if outer is not None:
        has_limit = bool(outer.args.get("limit") or outer.args.get("fetch"))
        has_from = (outer.args.get("from_") or outer.args.get("from")) is not None
        if has_from and not has_limit and not _has_aggregation(outer):
            add("GV002", "warn",
                "The result has no LIMIT and no aggregation, so it can return every row.",
                "Add a LIMIT for exploration, or aggregate (GROUP BY / COUNT) before returning rows.", outer)
        if outer.args.get("order") and not has_limit:
            add("GV006", "warn",
                "ORDER BY without LIMIT sorts the whole result.",
                "Add a LIMIT, or drop the ORDER BY if the order does not matter.", outer.args["order"])
    for select in tree.find_all(exp.Select):
        if select is outer:
            continue
        if select.args.get("order") and not select.args.get("limit") and not select.find_ancestor(exp.Window):
            add("GV006", "info",
                "ORDER BY inside a subquery without LIMIT is usually ignored but may still cost a sort.",
                "Remove the inner ORDER BY.", select.args["order"])

    # GV016 order-by-random, including inner queries and expressions such as ABS(RANDOM()).
    for order in tree.find_all(exp.Order):
        if order.find(exp.Rand) is not None:
            add("GV016", "warn",
                "ORDER BY RANDOM() / RAND() generates a random key for every input row and orders those keys, "
                "even with LIMIT.",
                "For random row sampling, consider TABLESAMPLE (Postgres, BigQuery, Snowflake) or USING SAMPLE "
                "(DuckDB). Check the engine's sampling semantics before replacing the query.", order)

    # GV017 deep-offset: an OFFSET with an integer literal of 10,000 or more
    for offset in tree.find_all(exp.Offset):
        expr = offset.expression
        if isinstance(expr, exp.Literal) and expr.is_int:
            try:
                val = int(expr.this)
            except (ValueError, TypeError):
                continue
            if val >= DEEP_OFFSET_THRESHOLD:
                add("GV017", "warn",
                    f"OFFSET {val:,} produces and discards {val:,} rows before returning results.",
                    "Use keyset pagination (WHERE id > <last id seen> ORDER BY id LIMIT ...) instead of a deep OFFSET.",
                    offset)

    # GV018 count-for-existence: COUNT(*) compared with zero only to ask "is there any row?"
    for comparison in tree.find_all(exp.GT, exp.GTE, exp.NEQ, exp.EQ, exp.LT):
        kind = _count_for_existence(comparison)
        if kind == "exists":
            add("GV018", "info",
                "A subquery counts every matching row only to test that the count is not zero.",
                "Use EXISTS (SELECT 1 FROM ...): it can stop at the first match.", comparison)
        elif kind == "not exists":
            add("GV018", "info",
                "A subquery counts every matching row only to test that the count is zero.",
                "Use NOT EXISTS (SELECT 1 FROM ...): it can stop at the first match.", comparison)

    # GV003 cartesian-join / GV008 non-equi-join
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        where_aliases = _aliases_in(where) if where is not None else set()
        for join in select.args.get("joins") or []:
            target = join.this
            if isinstance(target, (exp.Unnest, exp.Lateral)) or (
                isinstance(target, exp.Table) and isinstance(target.this, exp.Unnest)
            ):
                continue
            alias = (target.alias_or_name or "").lower()
            kind = (join.args.get("kind") or "").upper()
            condition = _join_condition(join)
            if kind == "CROSS":
                add("GV003", "warn",
                    f"CROSS JOIN with {alias or 'a table'} multiplies row counts (rows A x rows B).",
                    "Make sure one side is tiny, or join on a key instead.", join)
                continue
            if condition is None:
                joined_in_where = where is not None and alias in where_aliases and any(
                    isinstance(eq, exp.EQ) for eq in where.find_all(exp.EQ)
                )
                if joined_in_where:
                    add("GV003", "info",
                        f"Comma join with {alias} relies on the WHERE clause for its join key.",
                        "Write it as an explicit JOIN ... ON so a missing key cannot slip in.", join)
                else:
                    add("GV003", "error",
                        f"Join with {alias or 'a table'} has no join condition: a cartesian product.",
                        "Add ON <key> = <key> (or USING). If you really want every combination, write CROSS JOIN.",
                        join)
                continue
            if join.args.get("on") is not None:
                on = join.args["on"]
                has_eq = any(True for _ in on.find_all(exp.EQ))
                top_or = isinstance(on, exp.Or)
                if not has_eq or top_or:
                    add("GV008", "warn",
                        f"Join with {alias} uses a non-equality or OR condition; these joins can explode "
                        "and cannot use hash joins.",
                        "Join on equality keys where possible; split OR conditions into UNION ALL.", join)

    # GV004 missing-partition-filter (needs engine metadata)
    if table_info:
        where_columns = {
            col.name.lower()
            for select in tree.find_all(exp.Select)
            if select.args.get("where") is not None
            for col in select.args["where"].find_all(exp.Column)
        }
        for table in base_tables(tree, dialect):
            info = table_info.get(table_ref(table, dialect).lower())
            if not info:
                continue
            part = (info.get("partitioned_by") or "")
            part_cols = [p.strip().lower() for p in str(part).split(",") if p.strip()]
            if part_cols and not any(p in where_columns for p in part_cols):
                add("GV004", "error",
                    f"{table_ref(table, dialect)} is partitioned by {', '.join(part_cols)} but the query does not "
                    "filter on it, so every partition is scanned.",
                    f"Add a WHERE filter on {part_cols[0]} (for example a date range).", table)

    # GV005 function-on-filter-column
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if where is None:
            continue
        for cmp in where.find_all(*COMPARISONS):
            operands = [cmp.this, cmp.args.get("expression")]
            for operand in operands:
                if isinstance(operand, (exp.Func, exp.Cast, exp.Binary)) and not isinstance(operand, exp.Column):
                    if isinstance(operand, (exp.Binary,)) and not isinstance(operand, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
                        continue
                    cols = list(operand.find_all(exp.Column))
                    if cols and not isinstance(operand, exp.AggFunc):
                        add("GV005", "warn",
                            f"The filter wraps {cols[0].sql(dialect=dialect)} in a function or calculation, which "
                            "blocks partition pruning and min/max skipping.",
                            "Compare the raw column to a constant range, e.g. ts >= '2024-01-01' AND "
                            "ts < '2024-01-02' instead of DATE(ts) = '2024-01-01'.", cmp)
                        break

    # GV007 exact-count-distinct
    for count in tree.find_all(exp.Count):
        if isinstance(count.this, exp.Distinct):
            add("GV007", "info",
                "COUNT(DISTINCT ...) is exact and memory-heavy on big data.",
                "If an estimate is fine, use APPROX_COUNT_DISTINCT (or the engine's HLL function).", count)
            break

    # GV009 union-distinct
    for union in tree.find_all(exp.Union):
        if union.args.get("distinct"):
            add("GV009", "info",
                "UNION removes duplicates, which needs a full sort or hash of both sides.",
                "Use UNION ALL if duplicates are impossible or acceptable.", union)
            break

    # GV010 scalar subquery in the SELECT list
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            if any(isinstance(n, exp.Subquery) for n in projection.walk()):
                add("GV010", "info",
                    "A subquery in the SELECT list can run once per row on some engines.",
                    "Rewrite it as a JOIN on a pre-aggregated subquery.", projection)
                break

    # GV011 window-without-partition
    for window in tree.find_all(exp.Window):
        if not window.args.get("partition_by") and window.args.get("order"):
            add("GV011", "warn",
                "A window function with ORDER BY but no PARTITION BY puts every row in one partition "
                "(one worker, one big sort).",
                "Add PARTITION BY a key, or compute it on a smaller result.", window)
            break

    # GV012 NOT IN (subquery)
    for not_node in tree.find_all(exp.Not):
        inner = not_node.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            add("GV012", "info",
                "NOT IN (subquery) returns nothing if the subquery has a NULL, and often plans badly.",
                "Use NOT EXISTS or a LEFT JOIN ... WHERE key IS NULL.", not_node)
            break

    # GV013 LIKE / ILIKE with a leading wildcard
    for like in tree.find_all(exp.Like, exp.ILike):
        pattern = like.expression
        if isinstance(pattern, exp.Literal) and pattern.is_string and pattern.name.startswith("%"):
            add("GV013", "info",
                f"{like.sql(dialect=dialect)} starts with a wildcard, so no index or min/max statistics can help: "
                "it scans every value.",
                "Filter on another (partition or indexed) column first, or use the engine's full-text / "
                "n-gram search.", like)
            break

    # GV015 fan-trap (kept narrow on purpose: only the `one.id = many.x_id` naming pattern counts)
    for select in tree.find_all(exp.Select):
        trap = _fan_trap(select, tree)
        if trap is not None:
            agg, one, many, cond = trap
            add("GV015", "warn",
                f"{agg.sql(dialect=dialect)} adds up a column of {one} after the join {cond.sql(dialect=dialect)}: "
                f"each {one} row repeats once per matching {many} row, so the total can be multiplied (fan trap).",
                f"Aggregate {many} per key in a subquery before the join, or compute the {one} total separately.",
                agg)

    # GV014 sensitive-column-in-output (column names, lineage, default policy; preflight adds the connection policy)
    from .guard import lint_findings

    for severity, message, fix in lint_findings(tree, dialect, table_info):
        add("GV014", severity, message, fix)

    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule))
    return findings


def _fan_trap(select: exp.Select, tree: exp.Expression) -> tuple[exp.Expression, str, str, exp.EQ] | None:
    """SUM/AVG/COUNT of a column from the 'one' side of `one.id = many.<x>_id`, in an aggregating SELECT.

    The many side must be a base table (a subquery or CTE may already be aggregated per key), and the query
    must not group by the many side's own id (then each group has one many row)."""
    if not _has_aggregation(select):
        return None
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    sources: dict[str, exp.Expression] = {}
    from_ = select.args.get("from_") or select.args.get("from")
    for item in ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]:
        sources[(item.alias_or_name or "").lower()] = item
    group_cols = {(c.table.lower(), c.name.lower()) for c in (select.args.get("group") or exp.Group()).find_all(exp.Column)}
    for join in select.args.get("joins") or []:
        cond = join.args.get("on")
        if not (isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column)
                and isinstance(cond.expression, exp.Column)):
            continue
        left, right = cond.this, cond.expression
        if not (left.table and right.table) or left.table.lower() == right.table.lower():
            continue
        sides = [(left.table.lower(), left.name.lower()), (right.table.lower(), right.name.lower())]
        ones = [s for s in sides if s[1] == "id"]
        manys = [s for s in sides if s[1] != "id" and s[1].endswith("_id")]
        if len(ones) != 1 or len(manys) != 1:
            continue
        one, many = ones[0][0], manys[0][0]
        many_src = sources.get(many)
        if not (isinstance(many_src, exp.Table) and isinstance(many_src.this, exp.Identifier)) or (
                many_src.name.lower() in cte_names and not many_src.args.get("db")):
            continue
        if one not in sources or (many, "id") in group_cols:
            continue
        if _anti_join(select, join, many):
            continue
        for projection in select.expressions:
            for agg in projection.find_all(exp.Sum, exp.Avg, exp.Count):
                if agg.find_ancestor(exp.Window) or agg.find_ancestor(exp.Select) is not select:
                    continue
                if isinstance(agg.this, exp.Distinct):
                    continue
                cols = list(agg.find_all(exp.Column))
                if cols and all(c.table and c.table.lower() == one for c in cols):
                    return agg, one, many, cond
    return None


def _anti_join(select: exp.Select, join: exp.Join, many: str) -> bool:
    """LEFT JOIN many ... WHERE many.<col> IS NULL keeps only the rows with no match: each one row once."""
    side = (join.args.get("side") or "").upper()
    joined = (join.this.alias_or_name or "").lower()
    if not ((side == "LEFT" and joined == many) or (side == "RIGHT" and joined != many)):
        return False
    where = select.args.get("where")
    if where is None:
        return False
    conds = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
    return any(isinstance(c, exp.Is) and isinstance(c.expression, exp.Null) and isinstance(c.this, exp.Column)
               and (c.this.table or "").lower() == many for c in conds)


def summarize(findings: list[Finding]) -> dict[str, int]:
    out = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        out[f.severity] += 1
    return out
