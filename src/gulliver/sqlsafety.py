"""Decide what a SQL statement is allowed to do before anything runs.

Gulliver is read-first. It understands three kinds of statements:

  read   SELECT / WITH ... SELECT / set operations   -> may be estimated, rehearsed, run
  write  CREATE TABLE ... AS SELECT, INSERT ... SELECT -> the SELECT part is estimated and
         rehearsed (rehearsals never write); a real run needs allow_write=True
  block  everything else (DELETE, UPDATE, DROP, TRUNCATE, ALTER, MERGE, GRANT, several
         statements at once, unparsable SQL)               -> refused

A read or write that calls a side-effect function, runs SQL hidden in a string, or reads a file or URL outside
the allowed folders is also refused, with a code GS001-GS004 (see security.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError

from .security import SecurityPolicy
from .security import check as security_check

READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)
# Statements that change data or schema. Inside another statement (a CTE such as WITH d AS (DELETE ... RETURNING *))
# they would run as a side effect of a read or of an INSERT/CREATE ... SELECT, so they are refused there.
DATA_CHANGING = (exp.Insert, exp.Delete, exp.Update, exp.Create, exp.Drop, exp.Merge)
SYNTAX_ERROR_NEXT_STEP = "Fix the syntax error and try again. If the SQL is for another engine, set --dialect."


@dataclass
class Statement:
    sql: str
    dialect: str
    kind: str                      # "read" | "write" | "block"
    reason: str = ""
    tree: exp.Expression | None = None
    select: exp.Expression | None = None   # the read part (for write statements too)
    target: str | None = None              # write target table
    tables: list[str] = field(default_factory=list)
    code: str = ""                         # GS001-GS004 when a security rule refused it
    parse_error: bool = False              # internal routing for syntax-specific advice

    @property
    def sql_hash(self) -> str:
        # comments are dropped: the same query with "-- retry 2" is the same query (loop guard, approvals)
        return sql_fingerprint(self.select.sql(dialect=self.dialect, comments=False)
                               if self.select is not None else self.sql)

    def public(self) -> dict:
        out = {"kind": self.kind, "tables": self.tables}
        if self.reason:
            out["reason"] = self.reason
        if self.code:
            out["code"] = self.code
        if self.target:
            out["write_target"] = self.target
        return out


def sql_fingerprint(sql: str) -> str:
    normalized = " ".join(sql.split()).rstrip(";").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _strip_trailing(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def parse_error_reason(exc: SqlglotError, sql: str, dialect: str) -> str:
    """Format parser details without internal token dumps or ANSI-highlighted exception text.

    TokenError has no structured position. Report the end of input explicitly as a fallback,
    rather than guessing a quote's opening position from dialect-specific tokenization rules.
    """
    details = exc.errors[0] if isinstance(exc, ParseError) and exc.errors else {}
    line, column = details.get("line"), details.get("col")
    if isinstance(line, int) and line > 0 and isinstance(column, int) and column > 0:
        location = f"line {line}, column {column}"
        if highlight := details.get("highlight"):
            location += f", near {json.dumps(highlight, ensure_ascii=False)}"
    else:
        line = sql.count("\n") + 1
        column = len(sql.rsplit("\n", 1)[-1]) + 1
        location = f"line {line}, column {column} (end of input)"
    if isinstance(exc, TokenError):
        description = "Could not tokenize SQL; check for an unterminated quote or comment"
    else:
        description = str(details.get("description") or "Syntax error").split("<Token", 1)[0].strip()
        description = re.sub(r"\s+but got$", "", description)
        if not description or "TokenType" in description:
            description = "Unexpected token"
    return f"Could not parse SQL for dialect '{dialect}' at {location}: {description.rstrip('.')}."


def base_tables(tree: exp.Expression, dialect: str) -> list[exp.Table]:
    """Physical tables (and table functions such as read_parquet) referenced by a query.

    CTE names are excluded, as are write targets.
    """
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    out: list[exp.Table] = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower() if isinstance(table.this, exp.Identifier) else ""
        if name and name in cte_names and not table.args.get("db") and not table.args.get("catalog"):
            continue
        out.append(table)
    return out


def table_ref(table: exp.Table, dialect: str) -> str:
    """The table reference without its alias, as SQL text for the dialect."""
    bare = table.copy()
    bare.set("alias", None)
    for key in ("sample", "joins", "laterals", "pivots", "hints"):
        if key in bare.args:
            bare.set(key, None)
    return bare.sql(dialect=dialect)


def classify(sql: str, dialect: str = "duckdb", policy: SecurityPolicy | None = None) -> Statement:
    """What the statement is and whether it may run. `policy` comes from the connection
    (security.policy_for); without it, path allowlists cannot be checked (offline lint)."""
    statement = _classify(sql, dialect)
    if statement.kind != "block" and statement.tree is not None:
        hit = security_check(statement.tree, dialect, policy, statement.kind)
        if hit is not None:
            return Statement(sql, dialect, "block", reason=f"{hit.label}: {hit.reason}", tree=statement.tree,
                             tables=statement.tables, code=hit.code)
    return statement


def _classify(sql: str, dialect: str) -> Statement:
    text = _strip_trailing(sql)
    if not text:
        return Statement(sql, dialect, "block", reason="Empty SQL.")
    try:
        # Keep leading whitespace so parser positions refer to the SQL supplied by the caller.
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except SqlglotError as exc:   # ParseError and TokenError (e.g. Postgres `IN ($1,$2)`, an open quote)
        return Statement(sql, dialect, "block", reason=parse_error_reason(exc, sql, dialect), parse_error=True)
    if len(trees) != 1:
        return Statement(sql, dialect, "block", reason="Send exactly one statement at a time.")
    tree = trees[0]

    if isinstance(tree, (*READ_ROOTS, exp.Create, exp.Insert)) and any(
            isinstance(node, DATA_CHANGING) for node in tree.walk() if node is not tree):
        return Statement(sql, dialect, "block", reason="Data-changing statement nested inside a query.", tree=tree)

    if isinstance(tree, READ_ROOTS):
        if any(isinstance(node, exp.Select) and node.args.get("into") for node in tree.walk()):
            return Statement(sql, dialect, "block", reason="SELECT ... INTO writes a table; use CREATE TABLE ... AS SELECT.",
                             tree=tree)
        return Statement(sql, dialect, "read", tree=tree, select=tree,
                         tables=[table_ref(t, dialect) for t in base_tables(tree, dialect)])

    if isinstance(tree, exp.Create):
        kind = str(tree.args.get("kind") or "").upper()
        source = tree.expression
        if kind in ("TABLE", "VIEW") and isinstance(source, READ_ROOTS):
            target = tree.this
            target_table = target.this if isinstance(target, exp.Schema) else target
            target_name = target_table.sql(dialect=dialect) if target_table is not None else None
            return Statement(sql, dialect, "write", tree=tree, select=source, target=target_name,
                             tables=[table_ref(t, dialect) for t in base_tables(source, dialect)])
        return Statement(sql, dialect, "block", tree=tree,
                         reason="Only CREATE TABLE/VIEW ... AS SELECT is supported; other DDL is refused.")

    if isinstance(tree, exp.Insert):
        source = tree.expression
        if isinstance(source, READ_ROOTS):
            target = tree.this
            target_table = target.this if isinstance(target, exp.Schema) else target
            return Statement(sql, dialect, "write", tree=tree, select=source,
                             target=target_table.sql(dialect=dialect) if target_table is not None else None,
                             tables=[table_ref(t, dialect) for t in base_tables(source, dialect)])
        return Statement(sql, dialect, "block", tree=tree, reason="INSERT without a SELECT is refused.")

    label = type(tree).__name__.upper()
    return Statement(sql, dialect, "block", tree=tree,
                     reason=f"{label} statements are refused. Gulliver only rehearses and runs reads "
                            "and CREATE/INSERT ... SELECT.")
