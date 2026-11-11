# Lint in CI: files, folders, PR annotations and pre-commit

`gulliver lint "SQL"` checks one query. For a repository you want the same checks on every pull request:
on all `.sql` files and dbt models, with the problem shown on the right line, a way to accept a pattern on
purpose, and output your CI understands. Other SQL linters check style or DDL; none of them know that a
`DATE(ts) = ...` filter stops partition pruning, or offer a rewrite that is proven to return the same rows.

## Commands

```bash
gulliver lint models/ queries/                          # folders: every *.sql file; files work too
gulliver lint models/ --format sarif -o gulliver.sarif  # GitHub Code Scanning (any SARIF viewer)
gulliver lint models/ --format github                   # annotations on the PR (GitHub Actions)
gulliver lint models/ --format gitlab -o gl-code-quality.json   # GitLab Code Quality report
gulliver lint models/ --format junit -o gulliver.xml    # JUnit XML, one testsuite per file
gulliver lint models/ --json                            # everything, for scripts and agents
gulliver lint models/ --fail-on warn                    # fail on warnings too (default: error)
gulliver lint . --exclude 'models/legacy/*' --warn-unused-ignores
gulliver lint models/ --metadata -c warehouse           # read partitions and column types (free metadata)
gulliver lint models/ --write-baseline gulliver-baseline.json   # record today's findings, exit 0
gulliver lint models/ --baseline gulliver-baseline.json         # report and fail only on new findings

gulliver rules [--json]                                 # every rule: id, name, default severity, autofix

gulliver fix --check models/                            # exit 2 when a same-rows rewrite exists
gulliver fix --write models/ -c warehouse               # apply same-rows rewrites in place, nothing else
```

`gulliver lint` with one SQL string (or `-f file.sql`, or stdin) works exactly as before. A value that
exists on disk, more than one value, a files-only flag (`--format sarif|github|gitlab|junit`, `--fail-on`,
`--exclude`, `--warn-unused-ignores`, `--metadata`, `--baseline`, `--write-baseline`, `-o`), or one value that looks like a path (no spaces, and
a bare word, or it ends in `/` or `.sql`, or holds a `/`) switches to files mode. So a mistyped or missing
`models/` gives `Not found` and exit 1, not a SQL parse error.

Exit codes: `0` nothing at or above `fail_on`, `2` at least one finding fails the check (or, for
`fix --check`, a rewrite is available), `1` a path or the config is wrong.

## Example

`models/a.sql` (three statements) and the dbt model `models/sub/m.sql`:

```sql
-- a header
select *
from events e, users u;

-- gulliver: ignore GV002 exploratory
select e.user_id,
       e.ts
from events e
where DATE(e.ts) = '2025-01-03'
order by e.ts;
delete from events where 1=1;
```

```sql
{{ config(materialized='table') }}
{# a comment
   on two lines #}
select o.id, count(distinct o.user_id) as n  -- gulliver: ignore GV007
from {{ ref('orders') }} o
join {{ source('raw', 'users') }} u on o.user_id > u.id
group by 1
```

```text
$ gulliver lint models
models/a.sql:2:1: warn GV002 unbounded-output: The result has no LIMIT and no aggregation, so it can return every row.
    fix: Add a LIMIT for exploration, or aggregate (GROUP BY / COUNT) before returning rows.
models/a.sql:2:8: warn GV001 select-star: SELECT * reads every column. Columnar engines (BigQuery, Snowflake, DuckDB on Parquet) charge and work per column.
    fix: List only the columns you need.
models/a.sql:3:14: error GV003 cartesian-join: Join with u has no join condition: a cartesian product.
    fix: Add ON <key> = <key> (or USING). If you really want every combination, write CROSS JOIN.
models/a.sql:9:7: warn GV005 function-on-filter-column: The filter wraps e.ts in a function or calculation, which blocks partition pruning and min/max skipping.
    fix: Compare the raw column to a constant range, e.g. ts >= '2024-01-01' AND ts < '2024-01-02' instead of DATE(ts) = '2024-01-01'.
models/a.sql:10:1: warn GV006 order-without-limit: ORDER BY without LIMIT sorts the whole result.
    fix: Add a LIMIT, or drop the ORDER BY if the order does not matter.
models/sub/m.sql:6:1: warn GV008 non-equi-join: Join with u uses a non-equality or OR condition; these joins can explode and cannot use hash joins.
    fix: Join on equality keys where possible; split OR conditions into UNION ALL.

2 file(s), 4 statement(s): 1 error, 5 warn, 0 info (2 ignored, 1 statement(s) not a read, skipped)
next: 1 finding(s) at or above 'error'. Fix the SQL, or add `-- gulliver: ignore GVxxx` with a reason where the pattern is intended.

$ gulliver fix --check models -c warehouse
models/a.sql:9:7: GV005 [returns exactly the same rows]
    before: DATE(e.ts) = '2025-01-03'
    after:  e.ts >= '2025-01-03' AND e.ts < '2025-01-04'
2 file(s), 1 same-rows rewrite(s)
next: 1 same-rows rewrite(s) available. Run `gulliver fix --write` on these files.
```

## How files are read

- Folders are walked for `*.sql`. Hidden folders, `target/`, `dbt_packages/`, `node_modules/` are skipped
  while walking. `--exclude` and `[lint] exclude` globs apply to named files too, so pre-commit respects them.
- A file can hold many statements. It is split with the SQL tokenizer, so a `;` inside a string or a
  comment does not split it.
- Reads are linted: `SELECT`, and the `SELECT` part of `CREATE TABLE/VIEW ... AS SELECT` and
  `INSERT ... SELECT`. Other statements (DDL, `DELETE`, migrations) are counted as skipped, not reported.
- dbt models: `{{ ref() }}`, `{{ source() }}`, `{{ this }}`, `{{ config() }}`, `{% ... %}` and `{# ... #}` are
  rendered the same way as `gulliver impact` does, and every position is mapped back to the original file,
  so line and column point at the text you edit. Both branches of an `{% if %}` are kept; a statement that
  does not parse after rendering is reported as `GV900 parse-error` (warn) and not checked. Exclude
  `macros/` if it has no plain models.
- Positions come from the sqlglot tokens of the code a rule is about (the `*`, the join, the filter, the
  `ORDER BY`). The line is reliable; the column is the first token Gulliver can locate for that rule.

## Ignores

```sql
select *  -- gulliver: ignore GV001 we export every column on purpose
from events limit 10;

-- gulliver: ignore GV006, GV002     (a comment line: the next line, and above a statement: the whole statement)
select user_id, ts from events order by ts;

-- gulliver: ignore-file GV004       (anywhere in the file: the whole file)
```

Rule ids or names work (`GV001` or `select-star`). Without a rule list (or with `all`), every rule is
ignored there. Words after the rule list are a free-text reason. `{# gulliver: ignore GV001 #}` works in dbt
files. A directive inside a string literal is not a comment and is not read.

A rule list Gulliver cannot read ignores nothing and is always reported as `GV901`: an unknown rule id
(`GV-001`, `GV999`), a near-miss name (`select_star`), or no space after `ignore` (`ignore: GV001`,
`ignore(GV001)`). A typo therefore never turns into "ignore every rule".
`--warn-unused-ignores` also reports ignore comments that no longer ignore anything (`GV901 unused-ignore`).

## Baseline

Adopting the linter on a repo with hundreds of models gives a long list on day one. A baseline records the
findings that exist today, so only new ones are reported and fail the build. The old ones can be fixed over time.

```bash
gulliver lint models/ --write-baseline gulliver-baseline.json   # write the file, exit 0
gulliver lint models/ --baseline gulliver-baseline.json         # from now on: new findings only
```

Put the paths before `--write-baseline`; it takes the next word as its file name. Without a file name it uses
`[lint] baseline`. Commit the baseline file.

- **What is stored.** One entry per finding: path, rule, a fingerprint and a count. No SQL text. The file is
  sorted JSON, so it diffs cleanly. The fingerprint is path + rule + the whitespace-normalised source line (the
  same one the SARIF and GitLab outputs use), so a finding still matches after lines above it move.
- **What counts as new.** A second copy of a baselined line is new (the count is exceeded), and so is an edited
  line.
- **Paths.** Stored relative to the folder of the baseline file, with forward slashes, so one file works on
  Windows and Linux runners, from the repo root (pre-commit) and from a subfolder. `[lint] baseline` is
  relative to `gulliver.toml`; `--baseline` is relative to where you run the command.
- **Result.** Matching findings move to `ignored_findings` with `ignored_by: "baseline"`. They do not count
  toward `failing` or the exit code, and they are left out of the sarif, github, gitlab and junit output. The
  JSON result has `baselined` and `baseline_fixed` (baseline entries that no longer match). `baseline_fixed`
  counts only the files that were linted, so a pre-commit run on one file does not report the rest as fixed.
  The text summary says `(12 in baseline, 3 fixed: run --write-baseline to shrink it)`.
- **Shrinking it.** Run `--write-baseline` again after fixing findings. It records what is left today, so it
  also accepts any new findings: use it on purpose, not in CI.
- **Errors.** A baseline file that is missing or is not valid JSON is an error (exit 1), not an empty baseline.

## Configuration

```toml
[lint]
disable = ["GV010"]                         # rule ids or names
severity = { GV006 = "error", GV001 = "info" }
fail_on = "error"                           # error | warn | info | never
exclude = ["models/legacy/*", "seeds"]
dialect = "bigquery"                        # default: --dialect, then the connection's engine, then duckdb
warn_unused_ignores = true
baseline = "gulliver-baseline.json"         # relative to gulliver.toml; see Baseline
```

`[lint]` applies to files mode only. `gulliver lint "SQL"`, preflight and the run gate are not changed by it.

## Output formats

| format | for | notes |
|---|---|---|
| `text` | people | `path:line:col: severity GVxxx name: message` |
| `json` | scripts, agents | findings with `position`, `ignored_findings`, `fixes`, counts |
| `sarif` | GitHub Code Scanning, SARIF viewers | SARIF 2.1.0, rules metadata from `gulliver rules`, stable `partialFingerprints`, `fixes` with the exact same-rows edits |
| `github` | GitHub Actions annotations | GitHub shows a limited number of annotations per step (about 10 of each type); use SARIF for more |
| `gitlab` | GitLab Code Quality | sha1 fingerprints of path + rule + source line, stable when lines move |
| `junit` | any CI test report | one testsuite per file; findings at or above `fail_on` are failures |

GitHub Actions:

```yaml
- run: pip install gulliver-mcp
- run: gulliver lint models/ --format sarif -o gulliver.sarif --fail-on never
- uses: github/codeql-action/upload-sarif@v3
  with: { sarif_file: gulliver.sarif }
- run: gulliver lint models/ --format github      # annotations, and the job fails on errors
```

GitLab CI:

```yaml
gulliver:
  script: [pip install gulliver-mcp, gulliver lint models/ --format gitlab -o gl-code-quality.json]
  artifacts: { reports: { codequality: gl-code-quality.json }, when: always }
```

pre-commit (`.pre-commit-config.yaml`):

```yaml
repos:
  - repo: https://github.com/laaielichter/Gulliver
    rev: main            # or a release tag that includes these hooks
    hooks:
      - id: gulliver-lint
      - id: gulliver-fix
        args: [--dsn, warehouse.duckdb]   # column types are needed to prove same-rows rewrites
```

## Fixes: same rows only

`gulliver fix --check` and `--write` (and the SARIF `fixes`) use only rewrites with the `same` guarantee
from `gulliver fix`: today that is GV005, a function on a date column turned into a range on the raw column,
and only when the connection shows the column is a DATE or a TIMESTAMP without time zone. Without a
connection (or with `gulliver lint` without `--metadata`) the type is unknown, so no fix is offered. When a
table is not found in the connection (or there is no connection), `fix` says so in `notes` and the status is
`warn`, so "nothing to rewrite" is not mistaken for a clean check.

A fix is offered only where its finding is reported: a `gulliver: ignore` comment or `[lint] disable` for
GV005 also stops `fix --check` and `fix --write` there.

Each fix is applied as a small text edit, so comments, formatting, line endings (`\r\n` stays `\r\n`)
and a UTF-8 BOM stay. Before an edit is accepted, Gulliver applies it and re-parses the statement: it must
parse to exactly the tree `gulliver fix` produced. If it does not, the edit would touch dbt Jinja (also a
`{% if %}` or `{# #}` right next to it), or it would remove a comment inside the changed code, the fix is
listed as not placed and the file is left alone.

Viewers differ in what they do with SARIF `fixes`; GitHub Code Scanning shows the alert either way.

## GV015 fan-trap (new rule)

`SUM`, `AVG` or `COUNT` of a column from the one side of a join like `orders.id = order_items.order_id`
repeats each order once per item, so the total is multiplied. GV015 (warn) flags it only in this narrow
shape, to keep false alarms rare: one equality in the `ON`, one side's column is `id` and the other's ends
in `_id`, the many side is a plain table (not a subquery or CTE that may already be aggregated), and the
query does not group by the many side's `id`. An anti-join (`LEFT JOIN many ... WHERE many.x IS NULL`)
is not flagged: each one-side row survives at most once. It does not know real keys or intent, so a 1:1
join named this way, or a fact-to-dimension lookup where repeating the value is intended
(`sum(p.price) from order_items i join products p on p.id = i.product_id`), is flagged too; ignore it there
with `-- gulliver: ignore GV015`.

## SQL syntax errors

Syntax errors include a line and column, the highlighted token when available, and a short
parser description. For example, `gulliver lint "SELECT a b c FROM t" --json` reports:

```text
Could not parse SQL for dialect 'duckdb' at line 1, column 12, near "c": Invalid expression / Unexpected token.
```

The query stays blocked (exit code `2`). The next step is to fix the syntax or select the correct
engine with `--dialect`; restrictions on multiple statements and non-SELECT statements are unchanged.
If tokenization fails, such as an unterminated quote or comment, the message explicitly uses the
end of input as a fallback location, not the opening quote's position. File lint reports these errors
as GV900 and keeps its configured severity and exit-code policy.

## Limits

- Files mode needs no database. GV004 (missing partition filter) and the same-rows fixes need metadata, so
  pass `--metadata` (lint) or a connection (fix). Metadata is free: no data is read.
- dbt rendering is not dbt: macros, `var()` and other `{{ }}` become `NULL`, `{% %}` blocks are dropped. Use
  the compiled SQL (`target/compiled/...`) if you need exact results for heavy Jinja.
- The column is approximate for findings about a whole clause (it points at the first token Gulliver can
  locate). Columns count Unicode code points (SARIF `columnKind: unicodeCodePoints`).
- The MCP tool `gulliver_lint` still takes one SQL string; files mode is CLI only.
