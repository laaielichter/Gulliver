"""Output formats for `gulliver lint PATH...` and the rule list for `gulliver rules`.

  text    path:line:col: severity GVxxx name: message (for people)
  json    the full result (for agents and scripts)
  sarif   SARIF 2.1.0 for GitHub Code Scanning and other SARIF viewers; rules metadata, and `fixes` with the
          exact same-rows edits when Gulliver could prove them
  github  GitHub Actions workflow commands (::error file=...::), shown as annotations on the PR
  gitlab  GitLab Code Quality JSON (artifacts:reports:codequality)
  junit   JUnit XML, one testsuite per file
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import quoteattr as _quoteattr

from . import __version__
from .lint import RULES
from .lint_files import META_RULES, FileFinding, FileResult, fails, rule_name

FORMATS = ("text", "json", "sarif", "github", "gitlab", "junit")
HELP_URI = "https://github.com/laaielichter/Gulliver#scale-lint-rules"
INFO_URI = "https://github.com/laaielichter/Gulliver"

# id -> (default severity, what it means, how to fix, autofix)
RULE_DOCS: dict[str, tuple[str, str, str, str]] = {
    "GV001": ("warn", "SELECT * reads every column; columnar engines charge and work per column. An EXISTS "
                      "subquery is skipped: it is a semi-join, no engine reads its columns.",
              "List only the columns you need.", "none"),
    "GV002": ("warn", "The outer query has no LIMIT and no aggregation, so it can return every row.",
              "Add a LIMIT for exploration, or aggregate before returning rows.", "mode=all (changes the result)"),
    "GV003": ("error", "A join without a condition (cartesian product). CROSS JOIN is a warning; a comma join keyed "
                       "in WHERE is info.", "Join on a key (ON a.k = b.k), or write CROSS JOIN on purpose.", "none"),
    "GV004": ("error", "The table is partitioned but the query does not filter on the partition column. Needs "
                       "connection metadata (--metadata).", "Filter on the partition column (a date range).", "none"),
    "GV005": ("warn", "A function or calculation on a filtered column (DATE(ts) = ...) blocks partition pruning.",
              "Compare the raw column to a range.", "same rows when the column type is known (DATE / TIMESTAMP)"),
    "GV006": ("warn", "ORDER BY without LIMIT sorts the whole result (info inside a subquery).",
              "Add a LIMIT or drop the ORDER BY.", "none"),
    "GV007": ("info", "COUNT(DISTINCT ...) is exact and memory-heavy on big data.",
              "Use an approximate distinct count if an estimate is fine.", "mode=all (approximate)"),
    "GV008": ("warn", "A join on a non-equality or OR condition can explode and cannot use hash joins.",
              "Join on equality keys; split OR into UNION ALL.", "none"),
    "GV009": ("info", "UNION removes duplicates, which needs a sort or hash of both sides.",
              "Use UNION ALL if duplicates are impossible or acceptable.", "mode=all (keeps duplicates)"),
    "GV010": ("info", "A subquery in the SELECT list can run once per row on some engines.",
              "Rewrite it as a JOIN on a pre-aggregated subquery.", "none"),
    "GV011": ("warn", "A window with ORDER BY and no PARTITION BY puts every row in one partition.",
              "Add PARTITION BY a key.", "none"),
    "GV012": ("info", "NOT IN (subquery) returns nothing when the subquery has a NULL, and often plans badly.",
              "Use NOT EXISTS.", "mode=all (same rows unless the subquery returns NULL)"),
    "GV013": ("info", "LIKE / ILIKE with a leading wildcard ('%x') cannot use an index or min/max statistics, so it "
                      "scans every value.",
              "Filter on another (partition or indexed) column first, or use full-text / n-gram search.", "none"),
    "GV014": ("info", "An output column is personal data or a secret (by name, through aliases/CTEs/SELECT *, or "
                      "by earlier value tags). info for personal data or a masked secret, warn for a secret that "
                      "would reach the output unmasked.",
              "Leave the column out, aggregate it, or set a mask/hash/last4/deny action in "
              "[connections.X.policy].", "none"),
    "GV015": ("warn", "SUM/AVG/COUNT of a column from the one side of a join `one.id = many.x_id`: each one-side row "
                      "repeats per matching row, so the total is multiplied (fan trap). Only this naming pattern, "
                      "and only when the many side is a plain table.",
              "Aggregate the many side per key in a subquery before the join.", "none"),
    "GV016": ("warn", "ORDER BY RANDOM() / RAND() generates and orders a random key for every input row, even "
                      "with LIMIT.",
              "For random row sampling, consider TABLESAMPLE (Postgres, BigQuery, Snowflake) or USING SAMPLE "
              "(DuckDB); check the engine's sampling semantics.", "none"),
    "GV017": ("warn", "A high OFFSET produces and discards many rows, and every next page costs more.",
              "Use keyset pagination (WHERE id > <last id seen> ORDER BY id LIMIT N) instead of OFFSET.", "none"),
    "GV900": ("warn", "Gulliver could not parse a statement (dialect, or dbt Jinja it cannot render), so it was not "
                      "checked. Only in file lint.", "Set the dialect, or simplify the Jinja.", "none"),
    "GV901": ("warn", "A `gulliver: ignore` comment that ignores nothing: always for an unreadable rule list "
                      "(unknown rule, `ignore:`), unused ones only with --warn-unused-ignores.",
              "Remove the comment or the rule ids that no longer apply.", "none"),
}

SARIF_LEVEL = {"error": "error", "warn": "warning", "info": "note"}
GITHUB_LEVEL = {"error": "error", "warn": "warning", "info": "notice"}
GITLAB_SEVERITY = {"error": "major", "warn": "minor", "info": "info"}


def rules() -> list[dict[str, Any]]:
    out = []
    for rid in [*RULES, *META_RULES]:
        default, what, fix, autofix = RULE_DOCS.get(rid, ("warn", rule_name(rid), "", "none"))
        out.append({"rule": rid, "name": rule_name(rid), "default_severity": default, "description": what,
                    "fix": fix, "autofix": autofix, "help_uri": HELP_URI})
    return out


def rules_result() -> dict[str, Any]:
    return {"status": "ok", "rules": rules(),
            "next_step": "Turn rules off or change their severity in [lint] of gulliver.toml, or ignore one place "
                         "with `-- gulliver: ignore GVxxx`."}


def render_rules_text() -> str:
    lines = [f"{'rule':6} {'name':27} {'severity':8} description"]
    for r in rules():
        lines.append(f"{r['rule']:6} {r['name']:27} {r['default_severity']:8} {r['description']}")
        if r["autofix"] != "none":
            lines.append(f"{'':43} autofix: {r['autofix']}")
    return "\n".join(lines)


# -- per format ---------------------------------------------------------------------------------------------
def _results(result: dict[str, Any]) -> list[FileResult]:
    return list(result.get("_results") or [])


def render(result: dict[str, Any], fmt: str) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    if result.get("status") == "error" and fmt != "json":
        return f"gulliver: {result.get('error')}"
    return {"text": to_text, "json": to_json, "sarif": to_sarif, "github": to_github, "gitlab": to_gitlab,
            "junit": to_junit}[fmt](result)


def to_json(result: dict[str, Any]) -> str:
    from .lint_files import public

    return json.dumps(public(result), indent=2, default=str)


def to_text(result: dict[str, Any]) -> str:
    out: list[str] = []
    for f in (f for r in _results(result) for f in r.findings):
        out.append(f"{f.path}:{f.line}:{f.col}: {f.severity} {f.rule} {f.name}: {f.message}")
        if f.fix:
            out.append(f"    fix: {f.fix}")
    for x in result.get("fixes") or []:
        if x["placed"]:
            for e in x["edits"]:
                out.append(f"{x['path']}:{e['line']}:{e['col']}: autofix {', '.join(x['rules'])} [returns exactly the "
                           f"same rows]: {e['before']}  ->  {e['after']}")
    c = result.get("counts") or {}
    summary = (f"{result.get('files', 0)} file(s), {result.get('statements', 0)} statement(s): "
               f"{c.get('error', 0)} error, {c.get('warn', 0)} warn, {c.get('info', 0)} info")
    extra = []
    if result.get("ignored"):
        extra.append(f"{result['ignored']} ignored")
    if result.get("skipped_statements"):
        extra.append(f"{result['skipped_statements']} statement(s) not a read, skipped")
    if extra:
        summary += " (" + ", ".join(extra) + ")"
    if out:
        out.append("")
    out.append(summary)
    out.append(f"next: {result.get('next_step', '')}")
    return "\n".join(out)


def _rule_meta(rid: str) -> dict[str, Any]:
    default, what, fix, autofix = RULE_DOCS.get(rid, ("warn", rule_name(rid), "", "none"))
    rule: dict[str, Any] = {
        "id": rid,
        "name": rule_name(rid),
        "shortDescription": {"text": rule_name(rid)},
        "fullDescription": {"text": what},
        "help": {"text": f"{what} Fix: {fix}" + (f" Autofix: {autofix}." if autofix != "none" else ""),
                 "markdown": f"**{rid} {rule_name(rid)}**\n\n{what}\n\n**Fix:** {fix}"
                             + (f"\n\n**Autofix:** {autofix}" if autofix != "none" else "")},
        "helpUri": HELP_URI,
        "defaultConfiguration": {"level": SARIF_LEVEL.get(default, "warning")},
        "properties": {"tags": ["sql", "performance", "cost"] if rid not in META_RULES else ["sql", "gulliver"]},
    }
    return rule


def _region(line: int, col: int, end_line: int, end_col: int) -> dict[str, int]:
    region = {"startLine": max(1, line), "startColumn": max(1, col)}
    if (end_line, end_col) > (line, col):
        region.update(endLine=end_line, endColumn=end_col)
    return region


def _uri(path: str) -> str:
    from pathlib import Path

    p = Path(path)
    return p.as_uri() if p.is_absolute() else quote(path)


def _fingerprint(f: FileFinding, seen: dict[str, int]) -> str:
    """Stable across line moves: path + rule + the source line text (+ a counter for repeats)."""
    base = hashlib.sha1(f"{f.path}\0{f.rule}\0{' '.join(f.snippet.split())}".encode(), usedforsecurity=False).hexdigest()
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else hashlib.sha1(f"{base}:{seen[base]}".encode(),
                                                     usedforsecurity=False).hexdigest()


def to_sarif(result: dict[str, Any]) -> str:
    results = _results(result)
    used = list(dict.fromkeys([*RULES, *META_RULES, *(f.rule for r in results for f in r.findings)]))
    index = {rid: i for i, rid in enumerate(used)}
    seen: dict[str, int] = {}
    sarif_results = []
    for r in results:
        fixes_by_statement: dict[int, list[dict[str, Any]]] = {}
        for x in r.fixes:
            if x.placed:
                fixes_by_statement.setdefault(x.statement, []).append({
                    "description": {"text": f"{', '.join(x.rules)}: returns exactly the same rows. " + " ".join(x.notes)},
                    "artifactChanges": [{
                        "artifactLocation": {"uri": _uri(r.path)},
                        "replacements": [{"deletedRegion": _region(e.line, e.col, e.end_line, e.end_col),
                                          "insertedContent": {"text": e.after}} for e in x.edits],
                    }],
                })
        attached: set[int] = set()
        for f in r.findings:
            item: dict[str, Any] = {
                "ruleId": f.rule,
                "ruleIndex": index[f.rule],
                "level": SARIF_LEVEL.get(f.severity, "warning"),
                "message": {"text": f"{f.message} Fix: {f.fix}" if f.fix else f.message},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": _uri(f.path)},
                                                    "region": _region(f.line, f.col, f.end_line, f.end_col)}}],
                "partialFingerprints": {"gulliver/v1": _fingerprint(f, seen)},
            }
            if f.rule == "GV005" and f.statement in fixes_by_statement and f.statement not in attached:
                item["fixes"] = fixes_by_statement[f.statement]
                attached.add(f.statement)
            sarif_results.append(item)
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "gulliver", "version": __version__, "semanticVersion": __version__,
                                "informationUri": INFO_URI, "rules": [_rule_meta(rid) for rid in used]}},
            "columnKind": "unicodeCodePoints",
            "results": sarif_results,
        }],
    }
    return json.dumps(doc, indent=2)


def _gh_escape(text: str, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def to_github(result: dict[str, Any]) -> str:
    out = []
    for f in (f for r in _results(result) for f in r.findings):
        props = f"file={_gh_escape(f.path, True)},line={f.line},col={f.col}"
        if f.end_line >= f.line:
            props += f",endLine={f.end_line}"
            if f.end_line == f.line:
                props += f",endColumn={f.end_col}"
        props += f",title={_gh_escape(f'{f.rule} {f.name}', True)}"
        message = f"{f.message} Fix: {f.fix}" if f.fix else f.message
        out.append(f"::{GITHUB_LEVEL.get(f.severity, 'warning')} {props}::{_gh_escape(message)}")
    c = result.get("counts") or {}
    out.append(f"gulliver lint: {result.get('files', 0)} file(s), {c.get('error', 0)} error, {c.get('warn', 0)} warn, "
               f"{c.get('info', 0)} info, {result.get('failing', 0)} failing (fail_on={result.get('fail_on')})")
    return "\n".join(out)


def to_gitlab(result: dict[str, Any]) -> str:
    seen: dict[str, int] = {}
    issues = []
    for f in (f for r in _results(result) for f in r.findings):
        issues.append({
            "type": "issue",
            "check_name": f"{f.rule} {f.name}",
            "description": f"{f.rule} {f.name}: {f.message}",
            "content": {"body": f"Fix: {f.fix}"} if f.fix else {"body": ""},
            "categories": ["Performance"],
            "severity": GITLAB_SEVERITY.get(f.severity, "minor"),
            "fingerprint": _fingerprint(f, seen),
            "location": {"path": f.path, "lines": {"begin": f.line, "end": max(f.line, f.end_line)}},
        })
    return json.dumps(issues, indent=2)


def to_junit(result: dict[str, Any]) -> str:
    fail_on = result.get("fail_on", "error")
    suites = []
    total = failures = 0
    for r in _results(result):
        cases = []
        suite_failures = 0
        for f in r.findings:
            name = quoteattr(f"{f.rule} {f.name} (line {f.line}, col {f.col})")
            body = f"{f.path}:{f.line}:{f.col}: {f.severity} {f.rule} {f.name}: {f.message} Fix: {f.fix}"
            if fails(f.severity, fail_on):
                suite_failures += 1
                cases.append(f'    <testcase classname={quoteattr(r.path)} name={name}>\n'
                             f'      <failure type={quoteattr(f.severity)} message={quoteattr(f.message)}>'
                             f'{_xml_text(body)}</failure>\n    </testcase>')
            else:
                cases.append(f'    <testcase classname={quoteattr(r.path)} name={name}>\n'
                             f'      <system-out>{_xml_text(body)}</system-out>\n    </testcase>')
        if not cases:
            cases.append(f'    <testcase classname={quoteattr(r.path)} name="gulliver lint"/>')
        total += len(cases)
        failures += suite_failures
        suites.append(f'  <testsuite name={quoteattr(r.path)} tests="{len(cases)}" failures="{suite_failures}" '
                      f'errors="0" skipped="0">\n' + "\n".join(cases) + "\n  </testsuite>")
    head = (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuites name="gulliver lint" tests="{total}" '
            f'failures="{failures}" errors="0">')
    return "\n".join([head, *suites, "</testsuites>"])


# Code points XML 1.0 forbids (C0 controls except tab, LF and CR, plus U+FFFE/U+FFFF), as a translate table.
_XML_BAD = {cp: "\ufffd" for cp in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0xFFFE, 0xFFFF)}


def _xml_safe(text: str) -> str:
    """XML 1.0 forbids most control characters (a SQL literal can hold them): replace them with U+FFFD."""
    return text.translate(_XML_BAD)


def quoteattr(text: str) -> str:
    return _quoteattr(_xml_safe(text))


def _xml_text(text: str) -> str:
    return _xml_safe(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_fix_text(result: dict[str, Any]) -> str:
    if result.get("status") == "error":
        return f"gulliver: {result.get('error')}"
    out = []
    for x in result.get("fixes") or []:
        if x["placed"]:
            for e in x["edits"]:
                out.append(f"{x['path']}:{e['line']}:{e['col']}: {', '.join(x['rules'])} [returns exactly the same "
                           f"rows]\n    before: {e['before']}\n    after:  {e['after']}")
        else:
            out.append(f"{x['path']}: statement {x['statement']}: {', '.join(x['rules'])}: {' '.join(x['notes'])}")
    for n in result.get("notes") or []:
        out.append(f"note: {n}")
    if result.get("written"):
        out.append("rewrote: " + ", ".join(result["written"]))
    out.append(f"{result.get('files', 0)} file(s), {result.get('fixable', 0)} same-rows rewrite(s)")
    out.append(f"next: {result.get('next_step', '')}")
    return "\n".join(out)


# -- CLI glue (cli.py stays a thin wrapper) ------------------------------------------------------------------
def path_mode(values: list[str] | None, flags: bool = False) -> bool:
    """`gulliver lint X`: X is a file or folder (path mode) or one SQL string (unchanged single-SQL lint).

    Path mode when any value exists on disk, when several values are given, when a path-only flag is used, or
    when the one value looks like a path (no spaces, and ends in / or .sql or holds a /): a missing or mistyped
    folder is then reported as not found (exit 1), not linted as SQL."""
    import os

    values = values or []
    if any(os.path.exists(v) for v in values if v != "-"):
        return True
    if len(values) == 1 and _looks_like_path(values[0]):
        return True
    return len(values) > 1 or (flags and bool(values))


def _looks_like_path(value: str) -> bool:
    v = value.strip()
    if not v or v == "-" or any(c.isspace() for c in v) or any(c in v for c in "'\"();*=<>,"):
        return False
    # one bare word (`models`, `my_models.v2`) is not a SQL statement either
    return v.endswith(("/", "\\")) or v.lower().endswith(".sql") or "/" in v or bool(re.fullmatch(r"[\w.\-]+", v))


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        from pathlib import Path

        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _dialect_and_metadata(args: Any, config: Any, stack: Any, use_connection: bool,
                          required: bool = True) -> tuple[str, Any]:
    """Dialect: --dialect, then [lint] dialect, then the connection's engine (when one is configured)."""
    from .config import ConfigError, load_settings
    from .engines import dialect_for
    from .lint_files import Metadata

    dialect = getattr(args, "dialect", None) or config.dialect
    if not use_connection and dialect:
        return dialect, None
    try:
        settings = load_settings(config_path=args.config, dsn=args.dsn)
        conn = settings.connection(args.connection)
    except ConfigError:
        if use_connection:
            raise
        return dialect or "duckdb", None
    dialect = dialect or dialect_for(conn.engine)
    if not use_connection or dialect != dialect_for(conn.engine):
        return dialect, None
    from .core import _engine
    from .engines import EngineError

    try:
        _, engine = stack.enter_context(_engine(settings, conn.name))
    except EngineError as exc:
        if required:
            raise ValueError(f"Could not open connection '{conn.name}' for metadata: {exc}")
        return dialect, None
    return dialect, Metadata(engine)


def cli_lint(args: Any) -> int:
    from contextlib import ExitStack

    from .cli import EXIT
    from .lint_files import lint_paths, load_lint_config

    fmt = "json" if args.json and args.format in (None, "text") else (args.format or "text")
    config = load_lint_config(args.config)
    with ExitStack() as stack:
        dialect, metadata = _dialect_and_metadata(args, config, stack, bool(args.metadata))
        result = lint_paths(args.sql, dialect=dialect, config=config, fail_on=args.fail_on,
                            exclude=args.exclude or (), warn_unused_ignores=True if args.warn_unused_ignores else None,
                            metadata=metadata)
    text = render(result, fmt)
    _write_or_print(text, args.output)
    if args.output and fmt != "text":
        print(to_text(result) if result.get("status") != "error" else text)
    return EXIT.get(str(result.get("status")), 1)


def cli_fix(args: Any) -> int:
    from contextlib import ExitStack

    from .cli import EXIT
    from .lint_files import fix_paths, load_lint_config, public

    config = load_lint_config(args.config)
    with ExitStack() as stack:
        dialect, metadata = _dialect_and_metadata(args, config, stack, True, required=False)
        result = fix_paths(args.sql, dialect=dialect, config=config, metadata=metadata, write=bool(args.write))
    print(json.dumps(public(result), indent=2, default=str) if args.json else render_fix_text(result))
    return EXIT.get(str(result.get("status")), 1)


def cli_rules(args: Any) -> int:
    print(json.dumps(rules_result(), indent=2) if args.json else render_rules_text())
    return 0
