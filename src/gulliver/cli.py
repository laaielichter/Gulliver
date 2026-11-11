"""Command line: the same workflow as the MCP tools, with --json for agents and scripts.

Exit codes: 0 ok/warn/skipped, 1 error, 2 block, 3 needs_rehearsal / needs_approval, 4 a data check failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, core
from .config import ConfigError, load_settings

EXIT = {"ok": 0, "warn": 0, "skipped": 0, "error": 1, "block": 2, "needs_rehearsal": 3, "needs_approval": 3,
        "fail": 4}


def _read_sql(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    if args.sql == "-" or (args.sql is None and not sys.stdin.isatty()):
        return sys.stdin.read()
    if not args.sql:
        raise SystemExit("gulliver: give SQL as an argument, with -f file.sql, or on stdin")
    return args.sql


def _write_output(args: argparse.Namespace, text: str | None) -> None:
    """`-o/--output PATH`: write text (SQL, a receipt) to a UTF-8 file, when there is something to write."""
    path = getattr(args, "output", None)
    if path and text is not None:
        Path(path).write_text(text, encoding="utf-8")


def _print_human(result: dict[str, Any]) -> None:
    from .receipts import render_markdown

    if result.get("all") and "targets" in result:
        print(f"{'check':20} {'target':25} {'status':8} {'failed':>6}  {'receipt':12}  next_step")
        for t in result["targets"]:
            rec = t.get("receipt_id") or ""
            step = t.get("next_step", "")
            print(f"{t['name'][:20]:20} {t['target'][:25]:25} {t['status']:8} {t['failed']:>6}  {rec:12}  {step}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "markdown" in result:
        print(result["markdown"])
        return
    if result.get("receipt_id") and result.get("action"):
        print(render_markdown(result))
        preview = result.get("preview")
        if preview:
            print("## Preview\n")
            cols = result.get("columns") or list(preview[0])
            print(" | ".join(cols))
            for row in preview[:20]:
                print(" | ".join(str(row.get(c)) for c in cols))
            if result.get("truncated"):
                print(f"... ({result.get('actual', {}).get('rows', '?')} rows in total)")
        return
    if "sensitive_tables" in result:
        for t in result["sensitive_tables"]:
            print(f"{t['name']}: " + ("nothing sensitive found" if not t["sensitive_columns"] else ""))
            for c in t["sensitive_columns"]:
                print(f"  {c['column']:30} {c['kind']:7} {c['category']:12} {c['action']:6} ({c['detected_by']})")
        for note in result.get("notes") or []:
            print(f"note: {note}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "tables" in result and isinstance(result["tables"], list):
        print(f"{'table':40} {'rows':>14} {'bytes':>12}  partitioned_by")
        for t in result["tables"]:
            print(f"{t['name'][:40]:40} {t.get('rows', ''):>14} {core._fmt_bytes(t.get('bytes')):>12}  "
                  f"{t.get('partitioned_by', '')}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "table" in result:
        t = result["table"]
        print(f"{t['name']}: {t.get('rows', '?')} rows, {core._fmt_bytes(t.get('bytes'))}")
        if t.get("partitioned_by"):
            print(f"partitioned by: {t['partitioned_by']}")
        if t.get("clustered_by"):
            print(f"clustered by: {t['clustered_by']}")
        for c in t.get("columns") or []:
            tag = c.get("sensitive")
            print(f"  {c['name']:30} {c['type']}" + (f"  [{tag['kind']}: {tag['category']}, {tag['action']}]"
                                                     if tag else ""))
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "fixed_sql" in result:
        changes = result.get("changes") or []
        if not changes:
            print("Nothing to rewrite automatically.")
        from .verify import human_lines

        for c in changes:
            print(f"{c['rule']} [{c['meaning']}]\n  before: {c['before']}\n  after:  {c['after']}\n  why:    {c['note']}")
            for line in human_lines(c):
                print(line)
        for c in result.get("dropped") or []:
            print(f"DROPPED {c['rule']} (claimed the same rows, but generated data disagrees)")
            for line in human_lines(c):
                print(line)
        if changes:
            print("\n-- fixed SQL\n" + result["fixed_sql"])
        if (result.get("saving") or {}).get("summary"):
            print("\nsaving: " + result["saving"]["summary"])
        for item in result.get("remaining") or []:
            print(f"still: {item['severity']} {item['rule']} {item['name']}: {item['fix']}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "lint" in result and "counts" in result:
        if not result["lint"]:
            print("No scale problems found.")
        for item in result["lint"]:
            print(f"{item['severity']:5} {item['rule']} {item['name']}: {item['message']}\n      fix: {item['fix']}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "affected" in result:
        target = result.get("target", {})
        name = target.get("table", "") + (f".{target['column']}" if target.get("column") else "")
        print(f"Changing {name} affects {result['count']} model(s):")
        for a in result["affected"]:
            cols = ", ".join(a["columns"]) or "(whole model / filters)"
            star = " [SELECT *]" if a.get("select_star") else ""
            print(f"  {'  ' * (a['depth'] - 1)}{a['model']}: {cols}{star}  <- {', '.join(a['via'])}")
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "strategy" in result:
        print(f"Strategy: {result['strategy']}")
        for r in result.get("reasons") or []:
            print(f"  reason: {r}")
        for n in result.get("notes") or []:
            print(f"  note: {n}")
        if result.get("incremental_sql"):
            print("\n-- incremental SQL\n" + result["incremental_sql"])
        if result.get("dbt_model"):
            print("\n-- dbt model\n" + result["dbt_model"])
        if result.get("savings"):
            print("\nsavings: " + (result["savings"].get("summary") or json.dumps(result["savings"])))
        print(f"\nnext: {result.get('next_step', '')}")
        return
    if "to" in result and "sql" in result and "from" in result:
        print(result["sql"])
        for w in result.get("warnings") or []:
            print(f"-- warning: {w}")
        if result.get("check"):
            print(f"-- check on target: {result['check'].get('status')} {result['check'].get('error') or ''}")
        return
    print(json.dumps(result, indent=2, default=str))


def _emit(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(result)
    return EXIT.get(str(result.get("status")), 1)


def _add_common(p: argparse.ArgumentParser, defaults: bool) -> None:
    extra: dict[str, Any] = {} if defaults else {"default": argparse.SUPPRESS}
    p.add_argument("--config", help="Path to gulliver.toml (default: ./gulliver.toml or $GULLIVER_CONFIG)", **extra)
    p.add_argument("--dsn", help="Connection string: file.duckdb, postgresql://..., bigquery://project, ...", **extra)
    p.add_argument("-c", "--connection", help="Connection name from gulliver.toml", **extra)
    p.add_argument("--agent", help="Who is asking (ledger, per-agent budget, query tags); GULLIVER_AGENT wins", **extra)
    if defaults:
        p.add_argument("--json", action="store_true", help="Print JSON (for agents and scripts)")
    else:
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print JSON")


def _add_sql_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("sql", nargs="?", help="SQL text, or '-' to read stdin")
    p.add_argument("-f", "--file", help="Read the SQL from a file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gulliver",
        description="Test on tiny data before you run on giant data.",
        epilog="Workflow: inspect -> preflight -> rehearse -> run -> receipt. Docs: "
               "https://github.com/laaielichter/gulliver",
    )
    parser.add_argument("--version", action="version", version=f"gulliver {__version__}")
    _add_common(parser, defaults=True)
    # The same flags also work after the command (gulliver status --json).
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, defaults=False)
    sub = parser.add_subparsers(dest="command", required=True)
    _orig_add = sub.add_parser

    def add_parser(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return _orig_add(name, parents=[common], **kwargs)

    sub.add_parser = add_parser  # type: ignore[method-assign]

    sub.add_parser("status", help="Connections, budget and spend today")

    p = sub.add_parser("inspect", help="Biggest tables, or one table's columns")
    p.add_argument("table", nargs="?")
    p.add_argument("--limit", type=int, default=25, help="Tables to list (1-200); ignored with a table name")
    p.add_argument("--sensitive", action="store_true",
                   help="List columns that look like personal data or secrets (by name and earlier values)")
    p.add_argument("--scan", type=int, default=0, metavar="ROWS",
                   help="With --sensitive: check 0-1000 rows of text values (0 disables the budget-gated scan)")

    p = sub.add_parser("lint", help="Scale lint only (no database needed): one SQL string, or files and folders")
    p.add_argument("sql", nargs="*", help="SQL text, '-' to read stdin, or .sql files and folders (CI mode)")
    p.add_argument("-f", "--file", help="Read one SQL statement from a file")
    p.add_argument("--dialect", default=None, help="duckdb, postgres, bigquery or snowflake")
    p.add_argument("--format", choices=["text", "json", "sarif", "github", "gitlab", "junit"], default=None,
                   help="Files mode output (default text; --json = json)")
    p.add_argument("--fail-on", choices=["error", "warn", "info", "never"], default=None,
                   help="Files mode: exit 2 when a finding is at least this severe (default error, or [lint] fail_on)")
    p.add_argument("--exclude", action="append", metavar="GLOB", help="Files mode: skip matching paths (repeatable)")
    p.add_argument("--warn-unused-ignores", action="store_true", help="Files mode: report ignore comments that "
                                                                      "ignore nothing (GV901)")
    p.add_argument("--baseline", metavar="PATH", help="Files mode: report only findings that are not in this "
                                                      "baseline file (default: [lint] baseline)")
    p.add_argument("--write-baseline", nargs="?", const=True, default=None, metavar="PATH",
                   help="Files mode: record the current findings in PATH (default: [lint] baseline) and exit 0")
    p.add_argument("--metadata", action="store_true", help="Files mode: read table metadata from the connection "
                                                           "(partitions for GV004, column types for same-rows fixes)")
    p.add_argument("-o", "--output", metavar="PATH", help="Files mode: write the report to PATH (e.g. gulliver.sarif)")

    p = sub.add_parser("rules", help="List the lint rules (id, name, default severity, autofix)")
    p.add_argument("--format", choices=["text", "markdown", "json"], default="text",
                   help="Output format (default text; markdown = README table; --json = json)")

    p = sub.add_parser("fix", help="Rewrite SQL to be cheaper, and say if the rows stay exactly the same")
    p.add_argument("sql", nargs="*", help="SQL text, '-' to read stdin, or .sql files and folders with --check/--write")
    p.add_argument("-f", "--file", help="Read the SQL from a file")
    p.add_argument("--check", action="store_true", help="Files: exit 2 when a same-rows rewrite exists (CI)")
    p.add_argument("--write", action="store_true", help="Files: apply same-rows rewrites in place")
    p.add_argument("--dialect", default=None, help="duckdb, postgres, bigquery or snowflake (default: connection's)")
    p.add_argument("--mode", choices=["safe", "all"], default="safe",
                   help="safe: same rows only (default). all: also LIMIT, approx count, UNION ALL, NOT EXISTS")
    p.add_argument("--limit", type=int, default=None, help="Row limit to add in --mode all")
    p.add_argument("-o", "--output", metavar="PATH",
                   help="Write the rewritten SQL to PATH when there are changes (still prints the change list)")
    p.add_argument("--verify", action="store_true",
                   help="Test every change on generated edge-case data in a private in-memory DuckDB")
    p.add_argument("--cases", type=int, default=None, help="Generated cases for --verify (default 200)")
    p.add_argument("--seed", type=int, default=0, help="Seed for --verify; the same seed gives the same cases")
    p.add_argument("--timezone", default=None,
                   help="Session time zone for --verify (default: try several, incl. daylight-saving ones)")

    p = sub.add_parser("preflight", help="Estimate cost and size without running")
    _add_sql_args(p)

    p = sub.add_parser("rehearse", help="Run on 1%%, 3%%, 10%% samples and predict the full run")
    _add_sql_args(p)
    p.add_argument("--fractions", help="Comma-separated, e.g. 0.01,0.03,0.1")
    p.add_argument("--sample", choices=["largest", "all"], default="largest",
                   help="largest: sample the first occurrence of the largest table, read everything else in full "
                        "(default, unbiased for joins); all: sample every table and occurrence, each with its own "
                        "seed (cheaper, rougher for joins)")
    p.add_argument("--method", choices=["auto", "system", "bernoulli"], default="auto")
    p.add_argument("--seed", type=int, default=42)

    p = sub.add_parser("run", help="Run the full query inside the budget")
    _add_sql_args(p)
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--allow-write", action="store_true", help="Allow CREATE/INSERT ... SELECT "
                                                             "(the connection must allow it too)")

    p = sub.add_parser("receipt", help="Show a receipt or list recent receipts")
    p.add_argument("receipt_id", nargs="?", default="latest")
    p.add_argument("--list", action="store_true", help="List recent receipts instead of showing one")
    p.add_argument("--limit", type=int, default=20, help="Maximum receipts to list (default: 20)")
    p.add_argument("--status", help="Only list receipts with this status, e.g. needs_approval")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("-o", "--output", metavar="PATH", help="Also write the receipt (in --format) to PATH")

    p = sub.add_parser("approve", help="(Human) approve a run that is over the auto-approve limit")
    p.add_argument("receipt_id")
    p.add_argument("--note", default="")

    p = sub.add_parser("demo", help="Create a demo DuckDB database (5M events) to try Gulliver")
    p.add_argument("path", nargs="?", default="gulliver_demo.duckdb")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--events", type=int, default=5_000_000)

    def models_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--models", help="Folder with SQL models (*.sql, dbt-style refs allowed)")
        p.add_argument("--manifest", help="dbt target/manifest.json")
        p.add_argument("--dialect", default=None, help="duckdb, postgres, bigquery or snowflake")

    p = sub.add_parser("lineage", help="Model graph and column lineage of a folder of SQL models")
    models_args(p)

    p = sub.add_parser("impact", help="What breaks downstream if a table or column changes")
    p.add_argument("target", help="table or table.column, e.g. raw.events.user_id")
    models_args(p)

    sub.add_parser("snapshot", help="Save the connection's schema (tables, columns, rows) for drift checks")

    p = sub.add_parser("drift", help="Schema changes since the last snapshot (and the models they break)")
    models_args(p)
    p.add_argument("--save", action="store_true", help="Accept the current schema as the new snapshot")

    p = sub.add_parser("translate", help="Translate SQL between dialects")
    _add_sql_args(p)
    p.add_argument("--to", required=True, dest="to_dialect", choices=["duckdb", "postgres", "bigquery", "snowflake"])
    p.add_argument("--from", dest="from_dialect", default="duckdb",
                   choices=["duckdb", "postgres", "bigquery", "snowflake"])
    p.add_argument("--check", action="store_true", help="Preflight the result on the connection (-c)")
    p.add_argument("-o", "--output", metavar="PATH",
                   help="Write the translated SQL to PATH (still prints the warnings)")

    p = sub.add_parser("diff", help="Do two queries or tables return the same rows?")
    p.add_argument("--left", required=True, help="Query or table name")
    p.add_argument("--right", required=True, help="Query or table name")
    p.add_argument("--key", required=True, help="Key column(s), comma-separated")
    p.add_argument("--right-connection", help="Connection for the right side, if different")
    p.add_argument("--columns", help="Only compare these columns (comma-separated)")

    p = sub.add_parser("incremental", help="Can this full-rebuild model be incremental? Generates the SQL")
    _add_sql_args(p)
    p.add_argument("--time-column")
    p.add_argument("--unique-key", help="Comma-separated")
    p.add_argument("--period", default="1d", help="Run interval / recompute window, e.g. 1d, 1h")
    p.add_argument("--lookback", type=int, default=1)
    p.add_argument("--no-savings", action="store_true", help="Do not query the source to measure savings")

    p = sub.add_parser("check", help="Data quality checks in one scan (exit code 4 when a check fails)")
    p.add_argument("target", nargs="?", default=None,
                   help="Table name, SELECT, or configured check name (omit when using --all)")
    p.add_argument("--all", action="store_true", help="Run all data checks configured in gulliver.toml")
    p.add_argument("--where")
    p.add_argument("--unique", action="append", help="Key that must be unique; 'a,b' for composite (repeatable)")
    p.add_argument("--not-null", action="append", help="Comma-separated columns (repeatable)")
    p.add_argument("--accepted", action="append", help="col=a|b|c (repeatable)")
    p.add_argument("--range", action="append", dest="ranges", help="col=min:max (repeatable)")
    p.add_argument("--fresh", action="append", help="col=24h (repeatable)")
    p.add_argument("--min-rows", type=int)
    p.add_argument("--max-change", type=float, help="Max %% row-count change since the last check")
    p.add_argument("--samples", type=int, choices=range(21), default=0,
                   help="Up to N offending values per failed unique/accepted/range check (0-20; extra gated queries)")

    p = sub.add_parser("selftest", help="Check Gulliver itself: 'security' runs the bypass test set and the sandbox")
    p.add_argument("suite", choices=["security"])
    p.add_argument("--corpus", help="Another test set file (TOML, same format as the built-in one)")

    p = sub.add_parser("spend", help="Money and bytes spent, by agent, day, table, session, task, connection or kind")
    p.add_argument("--by", choices=["agent", "day", "table", "session", "task", "connection", "kind"], default="agent")
    p.add_argument("--days", type=int, default=7, help="How many days back, today included (default 7)")
    p.add_argument("--format", choices=["markdown", "csv"], default="markdown")
    p.add_argument("-o", "--output", metavar="PATH", help="Write the report (in --format) to PATH")

    p = sub.add_parser("calibration", help="Compare rehearsal prediction ranges with real runs")
    p.add_argument("--days", type=int, default=30, help="How many days back, today included (default 30)")

    p = sub.add_parser("audit", help="Check the hash chain over the ledger, receipts and approvals")
    p.add_argument("action", choices=["verify"])
    p.add_argument("--head", help="A head hash printed by an earlier verify: prove nothing up to it changed")

    p = sub.add_parser("prune", help="Clean up old receipts and expired approvals without breaking audit verify")
    p.add_argument("--older-than", default="30d", help="Remove receipts older than this (e.g. 30d, 7d, 24h; default 30d)")
    p.add_argument("--keep-last", type=int, default=None, help="Always keep at least this many newest receipts")
    p.add_argument("--dry-run", action="store_true", help="List what would be removed without deleting anything")

    p = sub.add_parser("top", help="Most expensive repeated queries, with priced same-rows fixes")
    p.add_argument("--source", default="auto",
                   help="auto, receipts, pg_stat_statements, bigquery_jobs, snowflake_history, csv:FILE or jsonl:FILE")
    p.add_argument("--file", dest="path", help="CSV / JSONL / JSON export (columns: query, calls, bytes, ...)")
    p.add_argument("--days", type=int, default=7, help="History window in days (default 7)")
    p.add_argument("--limit", type=int, default=20, help="Query groups to list (default 20)")
    p.add_argument("--analyze", type=int, default=10, help="Top groups to lint, fix and price (default 10)")
    p.add_argument("--rank-by", choices=["auto", "cost", "runtime", "bytes", "calls"], default="auto")
    p.add_argument("--dialect", default=None, help="Dialect of the SQL in a file (default: connection's)")
    p.add_argument("-o", "--output", metavar="PATH", help="Write the report to PATH")

    from .review import add_arguments as review_arguments

    review_arguments(sub.add_parser("review", help="PR gate: lint, change type, downstream impact and cost of the "
                                                   "SQL models a branch changes (exit code 2 = do not merge)"))

    p = sub.add_parser("tools", help="Print tool definitions for MCP, OpenAI or Gemini function calling")
    p.add_argument("--format", choices=["mcp", "openai", "gemini"], default="mcp")
    p.add_argument("--toolset", default="all", help="all, core, pipeline, or a comma list of those and tool names")

    p = sub.add_parser("mcp", help="Start the MCP server (same as gulliver-mcp)")
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--toolset", default="all", help="all, core, pipeline, or a comma list of those and tool names")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    from .identity import from_env, use

    with use(from_env(args.agent, default_agent="cli", source="cli")):
        return _main(parser, args)


def _main(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        if args.command == "demo":
            from .demo import build_demo

            return _emit(build_demo(args.path, events=args.events, overwrite=args.overwrite), args.json)
        if args.command == "tools":
            from .mcp_server import export_tools

            print(json.dumps(export_tools(args.format, args.toolset), indent=2))
            return 0
        if args.command == "mcp":
            from .mcp_server import main as mcp_main

            forwarded = []
            if args.config:
                forwarded += ["--config", args.config]
            if args.dsn:
                forwarded += ["--dsn", args.dsn]
            if args.http:
                forwarded += ["--http", "--host", args.host, "--port", str(args.port)]
            forwarded += ["--toolset", args.toolset]
            mcp_main(forwarded)
            return 0
        if args.command == "rules":
            from .lint_report import cli_rules

            return cli_rules(args)
        if args.command in ("lint", "fix"):
            from . import lint_report

            files_flags = args.command == "lint" and bool(
                args.format not in (None, "text", "json") or args.fail_on or args.exclude or args.warn_unused_ignores
                or args.metadata or args.output or args.baseline or args.write_baseline)
            if args.command == "fix" and (args.check or args.write):
                if not args.sql:
                    raise ValueError("gulliver fix --check/--write needs .sql files or folders")
                return lint_report.cli_fix(args)
            if not args.file and lint_report.path_mode(args.sql, files_flags):
                return lint_report.cli_lint(args) if args.command == "lint" else lint_report.cli_fix(args)
            if len(args.sql or []) > 1:
                raise ValueError("Give one SQL statement in quotes, or existing .sql files and folders")
            args.sql = args.sql[0] if args.sql else None
        if args.command == "lint":
            dialect = args.dialect
            if dialect is None:
                try:
                    settings = load_settings(config_path=args.config, dsn=args.dsn)
                    from .engines import dialect_for

                    dialect = dialect_for(settings.connection(args.connection).engine)
                except ConfigError:
                    dialect = "duckdb"
            return _emit(core.lint_sql(_read_sql(args), dialect), args.json)

        if args.command == "fix":
            from .fixer import fix

            try:
                settings = load_settings(config_path=args.config, dsn=args.dsn)
            except ConfigError:
                if not args.dialect:
                    raise
                settings = None
            result = fix(settings, _read_sql(args), connection=args.connection, dialect=args.dialect,
                        mode=args.mode, limit=args.limit, verify=args.verify, cases=args.cases, seed=args.seed,
                        timezone=args.timezone)
            if result.get("changes"):
                _write_output(args, result.get("fixed_sql"))
            return _emit(result, args.json)

        if args.command in ("lineage", "impact"):
            from . import features

            dialect = args.dialect or "duckdb"
            if args.command == "lineage":
                return _emit(features.lineage_overview(args.models, args.manifest, dialect), args.json)
            return _emit(features.impact(args.target, args.models, args.manifest, dialect), args.json)
        if args.command == "review":
            from .review import cli as review_cli

            return review_cli(args)
        if args.command == "translate" and not args.check:
            from . import features

            result = features.translate(None, _read_sql(args), args.to_dialect, args.from_dialect)
            _write_output(args, result.get("sql"))
            return _emit(result, args.json)

        settings = load_settings(config_path=args.config, dsn=args.dsn)
        conn = args.connection
        if args.command == "selftest":
            from . import security

            result = security.selftest(settings, conn, corpus=args.corpus)
            if not args.json:
                print(security.render_selftest(result))
                return EXIT.get(str(result.get("status")), 1)
            return _emit(result, args.json)
        if args.command == "top":
            from . import workload

            result = workload.top(settings, source=args.source, connection=conn, days=args.days, limit=args.limit,
                                  analyze=args.analyze, rank_by=args.rank_by, path=args.path, dialect=args.dialect)
            if not args.json:
                text = workload.render_text(result)
                if getattr(args, "output", None):
                    # print(text) always appends a newline; write that exact UTF-8
                    to_write = text if text.endswith("\n") else text + "\n"
                    _write_output(args, to_write)
                    print(f"Wrote {args.output}")
                else:
                    print(text)
                return EXIT.get(str(result.get("status")), 1)
            return _emit(result, args.json)
        if args.command in ("snapshot", "drift", "translate", "diff", "incremental", "check"):
            from . import features

            def csv(text: str | None) -> list[str] | None:
                return [c.strip() for c in text.split(",") if c.strip()] if text else None

            if args.command == "snapshot":
                result = features.snapshot(settings, conn)
            elif args.command == "drift":
                result = features.drift(settings, conn, args.models, args.manifest, save=args.save)
            elif args.command == "translate":
                result = features.translate(settings, _read_sql(args), args.to_dialect, args.from_dialect,
                                            connection=conn or settings.default_connection)
                _write_output(args, result.get("sql"))
            elif args.command == "diff":
                result = features.diff(settings, args.left, args.right, csv(args.key) or [], connection=conn,
                                       right_connection=args.right_connection, columns=csv(args.columns))
            elif args.command == "incremental":
                result = features.incremental(settings, _read_sql(args), connection=conn,
                                              time_column=args.time_column, unique_key=csv(args.unique_key),
                                              period=args.period, lookback=args.lookback,
                                              estimate_savings=not args.no_savings)
            elif args.command == "check":
                if args.all:
                    if args.target:
                        parser.error("do not pass a target when using --all.")
                    check_flags_passed = any([
                        args.where,
                        args.unique,
                        args.not_null,
                        args.accepted,
                        args.ranges,
                        args.fresh,
                        args.min_rows is not None,
                        args.max_change is not None,
                    ])
                    if check_flags_passed:
                        parser.error(
                            "do not pass check flags (--where, --unique, --not-null, --accepted, "
                            "--range, --fresh, --min-rows, --max-change) when using --all."
                        )
                    result = features.check_all(settings, connection=conn, samples=args.samples)
                else:
                    if not args.target:
                        parser.error("provide a target table/query, a configured check name, or --all.")
                    result = features.check(settings, args.target, connection=conn, where=args.where,
                                            unique=args.unique, not_null=args.not_null, accepted=args.accepted,
                                            ranges=args.ranges, fresh=args.fresh, min_rows=args.min_rows,
                                            max_change_pct=args.max_change, samples=args.samples)
            return _emit(result, args.json)
        if args.command == "calibration":
            from .calibration import calibration, render_calibration

            result = calibration(settings, days=args.days, connection=conn)
            if args.json or result["status"] != "ok":
                return _emit(result, args.json)
            print(render_calibration(result))
            return 0
        if args.command in ("spend", "audit", "prune"):
            from . import runaway

            if args.command == "audit":
                return _emit(runaway.audit_verify(settings, head=args.head), args.json)
            if args.command == "prune":
                try:
                    from .incremental import parse_period
                    older_than_td = parse_period(args.older_than)
                except ValueError as exc:
                    parser.error(f"--older-than: {exc}")
                result = runaway.prune(settings, older_than=older_than_td,
                                       keep_last=args.keep_last, dry_run=args.dry_run)
                if args.json or result.get("status") != "ok":
                    return _emit(result, args.json)
                print(runaway.render_prune(result))
                return 0
            result = runaway.spend(settings, by=args.by, days=args.days)
            if args.json or result["status"] != "ok":
                return _emit(result, args.json)
            text = runaway.render_spend(result, args.format)
            if getattr(args, "output", None):
                _write_output(args, text)
                print(f"Wrote {args.output}")
            else:
                print(text, end="")
            return 0
        if args.command == "status":
            result = core.status(settings)
        elif args.command == "inspect" and (args.sensitive or args.scan):
            from .guard import inspect_sensitive

            result = inspect_sensitive(settings, connection=conn, table=args.table, limit=args.limit,
                                       scan_rows=args.scan)
        elif args.command == "inspect":
            result = core.inspect(settings, connection=conn, table=args.table, limit=args.limit)
        elif args.command == "preflight":
            result = core.preflight(settings, _read_sql(args), connection=conn)
        elif args.command == "rehearse":
            fractions = [float(x) for x in args.fractions.split(",")] if args.fractions else None
            result = core.rehearse(settings, _read_sql(args), connection=conn, fractions=fractions,
                                   sample=args.sample, method=args.method, seed=args.seed)
        elif args.command == "run":
            result = core.run(settings, _read_sql(args), connection=conn, max_rows=args.max_rows,
                              allow_write=args.allow_write)
        elif args.command == "receipt":
            if args.list:
                if args.receipt_id != "latest":
                    parser.error("receipt --list does not take a receipt ID")
                result = core.list_receipts(settings, limit=args.limit, status=args.status)
                if result.get("status") == "ok" and not args.json:
                    for rec in result["receipts"]:
                        print(f"{rec['receipt_id']}  {rec['created_at']}  {rec['action']}  "
                              f"{rec['status']}  {rec['connection']}  {rec['sql_preview']}")
                    print(f"\nnext: {result['next_step']}")
                    return EXIT.get(str(result.get("status")), 1)
            else:
                result = core.get_receipt(settings, args.receipt_id, fmt=args.format)
                if result.get("status") == "ok":
                    text = (result["markdown"] if args.format == "markdown"
                            else json.dumps(result["receipt"], indent=2, default=str) + "\n")
                    _write_output(args, text)
        elif args.command == "approve":
            result = core.approve(settings, args.receipt_id, note=args.note)
        else:  # pragma: no cover - argparse guards this
            parser.error(f"unknown command {args.command}")
            return 2
        return _emit(result, args.json)
    except ConfigError as exc:
        return _emit({"status": "error", "error": str(exc), "next_step": "Fix gulliver.toml or the flags."}, args.json)
    except ValueError as exc:
        return _emit({"status": "error", "error": str(exc), "next_step": "Check the arguments."}, args.json)
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:  # pragma: no cover
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
