# Changelog

## Unreleased

### Fixed
- `lint`, `fix`, `lineage`, `impact` and `top` accept only `duckdb`, `postgres`, `bigquery` and `snowflake` for `--dialect`, so a typo such as `--dialect postgress` or an unsupported dialect such as `oracle` exits 2 with argparse's list of valid values, even with `--json`, instead of ending with "Check the arguments." or passing silently. Leaving `--dialect` out works as before. Thanks to @KingEmma7! (#214, #220)
- `review` and `drift` accept only `duckdb`, `postgres`, `bigquery` and `snowflake` for `--dialect` too, using the same shared list, so `--dialect postgress` or `--dialect oracle` exits 2 with argparse's list of valid values instead of passing silently. Thanks to @dianatofficial! (#222, #223)
- Lint GV003 now reports a join whose `ON` condition does not relate the two tables (`ON 1=1`, `ON TRUE`, `ON t.id = t.id`) as a cartesian product (error) instead of passing it as a key join. A real key, with or without extra conditions, and unqualified columns are unchanged. Thanks to @Ipsita-Mondal-30! (#156, #164)
- `impact`, `lineage` and `drift` refuse `--models` pointing to a folder with no `.sql` files with `status: error` and a `next_step` instead of reporting zero models or empty impact. Thanks to @shaw778! (#122, #147)
- `gulliver fix --verify --cases N` refuses a value below 1 with `status: error` and a `next_step`, before any rewrite is verified. Before, `--cases 0` became the default of 200 and `--cases -5` ran one case and reported `status: "ok"`, which is a weaker guarantee than it reads. The MCP tool already refused these with `ge=1`. `--cases` without `--verify` is still ignored Thanks to @tyza66! (#111, #117)
- `parse_period` refuses an out-of-range period with `ValueError` and a clear message, so `prune --older-than 99999999999d`, `check --fresh ts=99999999999d` and `incremental --period 99999999999d` return `status: "error"` and exit 1 instead of an `OverflowError` traceback. Huge values in hours, minutes and weeks are refused the same way as days, and valid periods are unchanged Thanks to @draarivpatel-ui! (#108, #110)
- `run --max-rows N` refuses a value below 0 with `status: error` and a `next_step`, exits 1, and skips estimating, running, or writing a receipt, while `--max-rows 0` stays valid. Thanks to @dianatofficial! (#119, #121)
- Lint GV003 no longer reports a `NATURAL JOIN` (including `NATURAL LEFT`, `RIGHT` and `FULL`) as a cartesian product: it joins on the columns both tables share, so `preflight` and `run` no longer block it. Thanks to @AzalHoseinian! (#150, #163)
- `gulliver diff` refuses unknown names in `--columns` before queries run, matching the behavior of the key check. Thanks to @dianatofficial! (#152, #168)
- SQL syntax errors are reported in plain words with the line, column and the parser's own description, instead of sqlglot's internal token repr and a "send a single SELECT" hint, for lint, preflight, rehearse and run. Thanks to @amritkang165! (#128, #139)
- A `gulliver.toml` section with the wrong type (for example `budget = 3`, `lint = 5` or `[connections] x = 1`) is refused with `status: error` and a message that names the section, instead of a Python traceback. Thanks to @amritkang165! (#126, #144)
- `rehearse --fractions` with a value that is not a comma-separated list of numbers (`abc`, `0.01,,0.1`) is refused with a message that names the option and shows the format, and a negative `--seed` is refused before any rung runs or a receipt is written, instead of a raw Python error or a parser error about "[removed by gulliver]". Thanks to @dianatofficial! (#151, #170)
- An unknown `default_connection` in `gulliver.toml` is refused with `ConfigError` and the list of known connections, and `gulliver -c NAME status` refuses an unknown NAME like `preflight` does, instead of quietly using another connection. Thanks to @IG-ABHINAV! (#127, #162)
- `receipt --list` with no match says why: `No receipts yet.` for an empty store, with a `next_step` that names the commands that write one, or `No receipts with status '...'.` with the statuses that do exist, instead of an empty line and advice to pass a receipt id that does not exist. Thanks to @dianatofficial! (#155, #172)
- `demo --events N` refuses a value below 1 or above 50,000,000 with `status: error`, a `next_step` and exit 1, before any file is created, instead of reporting `events: -5` for an empty table. Thanks to @dianatofficial! (#112, #115)
- `fix --verify --timezone` refuses an unknown time zone with `status: error`, a `next_step` and exit 1 before anything is rewritten or tested, instead of reporting `status: ok` with a DuckDB error inside `verify`. Zone names still match case-insensitively (`utc`, `europe/amsterdam`). Thanks to @Mr45401! (#153, #167)
- The result guard masks connection URLs with an embedded password (`postgresql://user:password@host/db`), as a whole cell and inside free text (`[masked:credential_url]`); they count as secrets, so the default policy masks them. Thanks to @dianatofficial! (#154, #166)
- `incremental` refuses a `--time-column` or `--unique-key` that is not a column of the source or the model, with `status: error`, a `next_step` that lists the columns, and exit 1, instead of generating a model that uses it. Thanks to @Ipsita-Mondal-30! (#157, #165)
- `check --fresh`, `--range`, `--accepted` and `incremental --period` with a malformed value name the option in the message and show a working example in `next_step`, instead of the generic message. Thanks to @deshmukh-viraj! (#123, #146)
- `fix --mode all --limit N` refuses a value below 1 with `status: error`, a `next_step` and exit 1, before any rewrite is built. Before, `--limit -5` wrote `LIMIT -5` into the SQL and `--limit 0` quietly became the budget default. `--mode safe` still ignores `--limit`. Thanks to @arpanjot-coding! (#118, #141)
- `check TABLE` with no check options no longer says `All checks passed.`: it returns `checks_requested: 0` and a `next_step` saying that only the row count was recorded and which options validate the data. The row count is still kept for `--max-change`. Thanks to @arpanjot-coding! (#130, #142)
- Lint GV005 gives `fix` advice that fits what wraps the filtered column and names the real column: a date range for date and time functions, moving the calculation to the constant side for arithmetic (`amount > 100 / 1.2`), a case-insensitive comparison or normalized column for `UPPER`/`LOWER`/`TRIM`, the NULL case written out for `COALESCE`, and a neutral sentence for anything else, instead of the same date-range example every time. Rule id, severity, message and the date autofix are unchanged. Thanks to @dianatofficial! (#158, #174)
- Lint GV005 no longer suggests a rewrite that changes results in three cases: a `COALESCE` whose first argument is not a plain column or whose default or compared value is not a literal, `/` on Postgres (integer division), and `TRIM`/`LTRIM`/`RTRIM`, which now get trimmed-column advice instead of case-folding advice. Thanks to @dianatofficial! (#191, #193)
- Lint GV005 no longer suggests a truncating division on Postgres: for `amount * 2 >= 11` the advice is the generic sentence instead of `amount >= 11 / 2`, which returns different rows, while exact and decimal divisions keep their concrete rewrite. Thanks to @tyza66! (#195, #196)
- `check` refuses a negative `--min-rows` and a negative, NaN or infinite `--max-change` with `status: error` and exit 1, instead of a check that always passes or always fails; `0` stays valid for both. Thanks to @rohangoud17! (#106, #138)
- `gulliver-mcp --port` and `gulliver mcp --port` refuse a value outside 0-65535 with a one-line `gulliver-mcp: port must be 0-65535` message and exit 2, instead of an `OverflowError` traceback. `--port 0` stays valid. Thanks to @HarshRajSinghania! (#113, #114)
- `incremental --lookback` and `prune --keep-last` refuse a negative number with `status: error` and a `next_step`, before any SQL advice is built or any receipt is pruned (a rejected `prune` leaves the receipt and audit files untouched). `0` stays valid. Thanks to @yinbowen-dc! (#98, #100)
- The DuckDB path check also covers `parquet_page_index`, a table function newer DuckDB versions add that reads a Parquet file: a path outside `allowed_paths` or a remote URL that is not allowed is now refused with GS004, like `parquet_metadata`. Thanks to @shaw778! (#206, #207)
- `lint --format markdown` keeps `<` and `>` in the Problem and Fix cells (escaped as `&lt;` / `&gt;`), so GV003's `Add ON <key> = <key>` no longer loses its placeholders on GitHub, and a `|` in a file path is escaped in the Where cell, so the table row stays intact. Thanks to @dianatofficial! (#209, #210)
- `spend --days N` refuses a value above 36500 (about 100 years) with `status: error`, a `next_step` and exit 1, before any date math, instead of an `OverflowError` traceback for a huge `--days`. Values from 1 to 36500 work as before. Thanks to @rohangoud17! (#107, #161)
- `receipt --list --action` and `--connection` with no match list the actions or connections that do exist in `next_step` (for example `Actions in your receipts: preflight, run.`), one line per filter used, instead of always listing the statuses. Thanks to @dianatofficial! (#213, #218)
- `top --days` outside 1-365 says `--days must be between 1 and 365.` and `diff --key ""` gets a `next_step` with the CLI form (`--key order_id`), instead of the generic "Fix the problem above and try again." Thanks to @dianatofficial! (#215, #217)

### Added
- Lint rule GV020 `null-comparison`: warns on `= NULL`, `<> NULL` and `!= NULL` in either operand order, including joins and CASE expressions. Suggests NULL predicates without an autofix because results change Thanks to @amritkang165! (#129, #137)
- Lint rule GV019 `having-on-group-column` (info, no autofix): flags a `HAVING` condition that uses only GROUP BY columns, since it filters after the aggregation and could move to `WHERE`. Aggregates, `GROUP BY`-less queries, `ROLLUP`/`CUBE`/`GROUPING SETS`, subqueries and `random()` are not flagged. Thanks to @Ipsita-Mondal-30! (#125, #186)
- `check --max-null COL=PCT` (MCP `max_null`, and `max_null` in a `[checks.<name>]` table of `gulliver.toml`) adds a `null_rate` check to the same scan as the other checks: it passes while at most PCT percent of the rows are NULL and reports `null_pct`, `max_null_pct` and `failing_rows`. `--max-null col=0` behaves like `--not-null col`, an empty table has no rate (`null_pct: null`, not passed), and a PCT that is not a finite number from 0 to 100 is refused before a query runs. Thanks to @tyza66! (#132, #183)
- A step-by-step tutorial on the demo database, [docs/tutorial.md](docs/tutorial.md), linked from the README quick start: inspect, preflight, lint, fix, rehearse, run and receipt, with the output to expect and no warehouse needed. Thanks to @HarshRajSinghania! (#134, #136)
- `gulliver demo --help` describes `--overwrite` and `--events`, `gulliver mcp --help` and `gulliver-mcp --help` describe `--http`, `--host` and `--port`, and the README CLI reference lists `demo --events/--overwrite`, `incremental --no-savings` and `mcp --host/--port`. Defaults and behaviour are unchanged. Thanks to @HarshRajSinghania! (#124, #149)
- `gulliver receipt --list` takes `--action` (for example `run` or `rehearse`) and uses the global `--connection` to list only the receipts of that action or connection. They combine with `--status`, the limit is applied after all filters, and the output stays summary-only. Thanks to @yinbowen-dc! (#93, #101)
- `gulliver lint --format markdown` prints a Markdown report for GitHub Actions job summaries (`>> $GITHUB_STEP_SUMMARY`) and PR comments: a summary line and a `Severity | Rule | Where | Problem | Fix` table, with `|` and line breaks escaped in cells, a one-line result for a clean run, and a note when the table is cut to stay under the 65,536-character comment limit. Works with `-o` and `--fail-on`. Thanks to @shaw778! (#133, #179)

## 0.4.0 - 2026-11-11

Data checks you define once in `gulliver.toml` and run in CI, a lint baseline for existing repos, rehearsal
calibration, receipt listing and pruning, three new lint rules, and stricter refusal of bad budgets, config keys
and flags. Thanks to 14 outside contributors.

### Added
- Refuse inspect limits outside 1-200 and sensitive value scans outside 0-1000 instead of silently clamping them (#97). A named table still ignores the listing limit.
- Data quality checks in `gulliver.toml` under `[checks.<name>]`: run them by name with `gulliver check NAME` or run all configured targets in CI with `gulliver check --all` (exits 4 if any check fails) (#80).
- `gulliver rules --format markdown|json` prints a generated rule table (markdown) or the same JSON as `--json`; the README Scale lint rules table is generated from it (#18).
- `gulliver spend -o/--output PATH` and `gulliver top -o/--output PATH` write the report as UTF-8 and print a one-line confirmation (#19).
- Opt-in `check --samples N` / MCP `samples` (0-20) reports guarded offending values and row counts for failed unique, accepted-values and range checks. Each extra query is budget-gated and logged; blocked or failed samples leave the check result intact (#48, #49).
- Lint rule GV016 `order-by-random`: warns on `ORDER BY RANDOM()` / `RAND()`, also with LIMIT, and points to `TABLESAMPLE` / `USING SAMPLE` for random sampling (#23).
- Prompt-injection scan: new `override_i18n` signal for "ignore previous instructions" in Dutch, German, French and Spanish (#22).
- Lint rule GV017 `deep-offset`: warns when an `OFFSET` is an integer literal of 10,000 or more and suggests keyset pagination (#30).
- Lint rule GV018 `count-for-existence`: flags `(SELECT COUNT(*) ...)` compared with zero (`> 0`, `>= 1`, `<> 0`, `= 0`, `< 1` and the mirrored `0 < ...`, also with `COUNT(1)`) and suggests `EXISTS`, or `NOT EXISTS` for `= 0` and `< 1` (#31).
- Result guard: npm (`npm_...`), PyPI (`pypi-...`) and Hugging Face (`hf_...`) tokens are tagged `api_key` and masked inside free text (#38).
- `gulliver calibration [--days N] [-c NAME]` and the `gulliver://calibration` MCP resource show how often rehearsal ranges held up against real runs, per metric and confidence level. New run receipts store `low`, `high` and `confidence` in `predicted_vs_actual` (#47).
- `gulliver receipt --list [--limit N] [--status STATUS] [--json]` lists recent receipts newest first, with summary fields only (no preview rows), so you can find a receipt without knowing its id (#39, #60).
- `gulliver diff` says which columns differ for changed keys ("Rows differ in: country (1 key)"), computed inside the existing keys query, so no extra queries run and no values leave the engine (#46, #69).
- `gulliver prune [--older-than 30d] [--keep-last N] [--dry-run]` cleans up old receipts and expired approvals while `gulliver audit verify` keeps passing (#79).
- `gulliver lint --baseline FILE` / `--write-baseline [FILE]` and `[lint] baseline`: record today's findings and report (and fail on) only new ones on an existing repo. Findings match by path, rule and source line, so moved lines still match and copies or edits are new. The JSON result gets `baselined` and `baseline_fixed` (#45).

### Thanks
- @bfs2021 for refusing NaN, infinite and negative budget limits (#40) and the GV001 fix for EXISTS subqueries (#50).
- @mayankdhingra02 for the guarded samples in `gulliver check` (#49) and the rehearsal calibration report (#61).
- @shaw778 for the derived `gulliver_lint` rule range (#41), the npm, PyPI and Hugging Face token shapes (#58), lint rule GV017 (#59), column-level `gulliver diff` (#69) and its follow-up test and note fix (#72).
- @ariktadas144 for the non-English prompt-injection signal (#62).
- @Doomguy-BFG9000 for escaping `|` and `~` in composite keys and diffs (#53).
- @17302513901yh-lang for refusing unknown lint rule ids (#54), the GV002 fix for aggregates inside subqueries (#64) and lint rule GV018 (#66), and @ariktadas144 for the careful review of it.
- @Muhtasim-Munif-Fahim for `gulliver rules --format markdown|json` and the generated README rule table (#68), and `spend -o` / `top -o` (#67).
- @ariktadas144 for `gulliver lint --baseline`, so existing repos only see new findings (#90).
- @shaw778 for `gulliver prune`, which cleans up receipts without breaking the audit chain (#88), and for checks defined in `gulliver.toml` with `gulliver check NAME` and `check --all` (#105).
- @yinbowen-dc for refusing out-of-range `inspect` limits and sensitive scan sizes (#99).
- @hamid-digitalmarketer for refusing `spend`, `calibration` and `top` windows and limits below one (#95).
- @Abhishekpandey123-git for `gulliver receipt --list` (#60).
- @HarshRajSinghania for the read-only Postgres role docs (#8), which shaped the section that shipped in 0.3.0.
- @YinL-k for lint rule GV016 (#23).
- @ShahadatTest for `gulliver fix -o/--output` (#10) and reading and writing SQL files as UTF-8 (#15).
- @abu-tahir-0 for five fixes on their first day: strict `check --range` bounds (#85), one row-count history per connection (#86), whole-number and non-boolean budget limits (#84), refusing unknown `gulliver.toml` sections (#83) and GV013 for leading `_` (#82).

### Fixed
- `spend --days N`, `calibration --days N` and `top --limit N` refuse a value below 1 with `status: error` and a `next_step`, and exit 1 instead of quietly reporting an empty or off-by-one window. `top --analyze` refuses a negative value too, while `--analyze 0` still lists the groups without analysing them (#92).
- `check --range` refuses `nan`, infinite and non-numeric bounds and an inverted `min > max` before any query runs, naming the column. Before, `nan` / `inf` reached the engine as column names and an inverted range silently failed every row (#74, #85).
- `check --max-change` keys the row-count history on the resolved connection name, so a check with and without `-c default` shares one history. Histories saved under the old key are still read and migrated (#75, #86).
- Whole-number budget limits (`max_output_rows`, `max_bytes`, `max_repeats`, ...) refuse fractions instead of truncating them (`max_output_rows = 0.5` used to become `0` and switch the limit off), and every budget key refuses `true` / `false` (#76, #84).
- An unknown top-level key or section in `gulliver.toml`, such as a `[budjet]` typo, is refused with the known sections listed. Before, it was ignored and the default budget applied (#77, #83).
- In a sampled `gulliver diff` with duplicate keys, "Column counts cover the sampled buckets only." appears once in `notes` instead of twice (#72).
- The `gulliver_lint` tool description builds its rule range from `RULES`; it said GV001-GV012 while the linter had grown past that (#37).
- A lint rule id that no rule uses (`disable = ["GV099"]`, a typo for GV009) is refused with "unknown rule", the way unknown names already were. Before, it loaded and turned nothing off (#54).
- Composite uniqueness checks and data diffs escape `~` and `|` in each text part before joining, so embedded separators no longer make distinct keys or rows compare equal (#36).
- GV002 no longer treats an aggregate inside a scalar subquery as if the outer query aggregates, so `SELECT id, (SELECT COUNT(*) ...) FROM users` without a LIMIT is reported again. A SELECT without FROM returns one row and is skipped (#64).
- GV001 no longer warns on `SELECT *` inside an `EXISTS` or `NOT EXISTS` subquery: it is a semi-join, so no engine reads those columns. A `SELECT *` elsewhere in the same query is still reported (#50).
- Budget limits that are `nan`, infinite or negative are refused at load time with an error that names the key, from `gulliver.toml` and from `GULLIVER_*` variables. Before, `max_query_usd = nan` silently switched the hard stop off (#40).
- `gulliver_drift` is no longer annotated read-only. It saves a local snapshot file (on the first call, and with `save=true`), so it now reports `readOnlyHint: false`, `destructiveHint: false`. It still only reads the database. Reported by Glama's tool scoring.
- New test `tests/test_tool_annotations.py`: read-only tools may not describe writes.
- `gulliver_check` is no longer annotated read-only either: it stores the row count locally for the next `max_change_pct` comparison.
- Receipt ids now carry 64 random bits instead of 32. With 32 bits, two ids made in the same second collided about 1 in 300 times over 5000 ids (seen as a flaky CI test on macOS). The prompt-injection guard recognizes the longer ids.

### Changed
- Lint rule GV013 `leading-wildcard-like` also flags `LIKE` / `ILIKE` patterns that start with the `_` wildcard; an escaped `\_` stays unflagged (#78, #82).
- All 16 MCP tool descriptions now say when to use the tool instead of its siblings, what it costs, what it stores locally and what each status means. Parameter descriptions explain defaults and effects (e.g. `rehearse.method`, `fix.seed`/`cases`, `diff.columns`, `impact.target` matching). Based on Glama's tool scoring.

## 0.3.0 - 2026-11-02

### Breaking

- **DuckDB is sandboxed.** DuckDB connections can no longer read files outside the database folder and the
  current folder, read remote URLs, or auto-load extensions. When one of those folders is `/`, the home folder or
  above it, it is left out of the default. Set `allowed_paths` (folders) or `allow_remote` (URLs, or `true`) on the
  connection in `gulliver.toml` to open them, or `sandbox = false` to turn the engine sandbox off (the parser still
  checks). SELECTs that call side-effect, dynamic-SQL or file functions are now refused (GS001-GS004);
  `allow_functions` lifts that per connection.
- **Self-join rehearsals changed, and are now right.** When a query reads the sampled table more than once (a
  self-join, `IN (SELECT ...)` on the same table, a CTE used twice), only the first occurrence is sampled and the
  others are read in full, and `--method auto` uses bernoulli on DuckDB. Before, both sides held the same sample, and
  the demo self-join was predicted 2-5x too low and outside its range. Now five seeds predict 252M-257M rows against
  254,966,150 real ones. A join that multiplies rows now shows the warning "the query multiplies rows" instead of an
  exponent above 1.3 (`--sample all` still shows the exponent). Rungs read more data, so rehearsals take longer.

### Security

- Stronger read-only gate: SELECTs that stop sessions, change settings or locks, run SQL hidden in strings, or
  read files and URLs are refused with codes GS001-GS004, in every dialect and every part of the statement (CTEs,
  subqueries, UNION branches, the SELECT of a write). Globs that match hidden files, `../` and symlinks out are
  refused too.
- DuckDB engine sandbox: `allowed_directories`, `enable_external_access = false`, no extension auto-install or
  auto-load, and `lock_configuration = true`.
- `gulliver selftest security`: runs the bypass test set that ships with Gulliver (150+ cases) and checks the
  sandbox live. `--corpus FILE` runs your own cases.
- Threat model in SECURITY.md.

### Agent guards

- Result guard: every `gulliver_run` preview is checked for personal data and secrets (by column name, by
  output-column lineage and by validated value patterns: email, phone, IBAN mod-97, Luhn cards, BSN 11-check, IP,
  JWT and API keys). It is marked `untrusted: true`, scanned for prompt injection (`data_warnings`, `injection`)
  and kept within `max_output_chars` / `max_cell_chars` (with a `column_summary` when rows are cut). New
  per-connection `policy` (`allow | warn | mask | hash | last4 | deny`; salted hashes via `GULLIVER_HASH_SALT`).
  The default masks only secrets. Receipts store only the guarded preview, and engine error messages are scrubbed.
  New `gulliver inspect --sensitive [--scan N]` and `gulliver_inspect(sensitive=true)`.
- Runaway guard: identical runs over `max_repeats` in `repeat_window_min` are blocked (reason `loop`) when the
  tables did not change, with the last result returned as `cached` (free), or need approval (reason `repeat`) when
  Gulliver cannot tell. Spending faster than `velocity_usd` per `velocity_window_min` needs approval. New
  `session_usd` and per-agent `agent_day_usd` budgets.
- Every ledger line records agent (MCP `clientInfo`, `--agent`, `GULLIVER_AGENT`), session, task (`GULLIVER_TASK`),
  tables and data version. Queries are tagged in the warehouse (Postgres `application_name`, Snowflake
  `QUERY_TAG`, BigQuery job labels).
- New `gulliver spend --by agent|day|table|session|task|connection|kind`, and a hash-chained ledger and receipt
  trail with `gulliver audit verify` (HMAC with `GULLIVER_AUDIT_KEY`).

### CI and pull requests

- `gulliver lint PATH...`: lint files and folders (multi-statement files, dbt Jinja, line and column on every
  finding), inline `-- gulliver: ignore GVxxx` / `ignore-file`, `--warn-unused-ignores`, `--fail-on`, `--exclude`,
  a `[lint]` section in `gulliver.toml`, and output as text, JSON, SARIF 2.1.0 (with same-rows fixes), GitHub
  annotations, GitLab Code Quality or JUnit XML. `gulliver lint "SQL"` is unchanged.
- New `gulliver rules`, `gulliver fix --check` / `--write` (same-rows rewrites only, checked by re-parsing) and
  `.pre-commit-hooks.yaml` (`gulliver-lint`, `gulliver-fix`).
- `gulliver review` (CLI), `gulliver_review` (MCP) and a GitHub Action (`action.yml`): a PR gate for SQL models.
  It reports new scale lint, the change type (cosmetic / non_breaking / breaking), the downstream models that
  break, the scan and money per run and per month before vs after, and with `--rehearse` new superlinear row
  growth. A `[review]` policy maps the flags to ok / warn / block (exit code 2). A missing base ref or base
  manifest blocks instead of reviewing everything.
- `gulliver top` / `gulliver_top`: the most expensive repeated queries from Gulliver's receipts,
  `pg_stat_statements`, BigQuery `JOBS_BY_PROJECT`, Snowflake `QUERY_HISTORY` or a CSV/JSONL file, grouped by
  shape. The top ones get lint, a same-rows rewrite and a saving per month labeled `exact` or `heuristic`, plus
  summary-table candidates. History queries go through the budget gate.

### Rewrites and lint

- `gulliver fix --verify` (MCP `gulliver_fix(verify=true)`): tests every rewrite on seeded, generated edge-case
  tables in a private in-memory DuckDB (NULLs, duplicates, date boundaries, infinity, daylight-saving jumps) and
  returns a shrunk, runnable counterexample when rows differ. Options `--cases`, `--seed` and `--timezone`. A
  counterexample against a `same` rewrite drops that change and warns; labels are never promoted. Preflight's
  `autofix` carries a quick `verified` block. The `nulls` label now also says a NULL in the outer column changes
  the result.
- `gulliver_fix` rewrites `DATE_TRUNC('quarter', ts) = '2024-04-01'` to a range on `ts` (same rows on DATE and
  TIMESTAMP columns; a literal that does not start a quarter is left alone)
  ([#4](https://github.com/laaielichter/Gulliver/issues/4)).
- New lint rules: GV013 `leading-wildcard-like` ([#5](https://github.com/laaielichter/Gulliver/issues/5)),
  GV014 `sensitive-column-in-output`, GV015 `fan-trap`; GV900 `parse-error` and GV901 `unused-ignore` in file lint.

### MCP

- Opt-in approval in the MCP client through elicitation (`[approval] channel = "elicitation"`), recorded in the
  receipt with its channel; falls back to `needs_approval`. The agent cannot fill the form.
- Resources (`gulliver://status`, `gulliver://rules`, tables, receipts), prompts (`safe-query-loop`,
  `refactor-and-prove`, `make-incremental`) and progress notifications per rehearsal sample.
- `gulliver-mcp --toolset core|pipeline|all` (and `gulliver tools --toolset`): `core` is 8 tools and less than
  half the schema.
- A typed `outputSchema` on every tool (status enum + `next_step`), and `destructiveHint` on `gulliver_run` when a
  connection allows writes.
- 16 tools (new: `gulliver_top`, `gulliver_review`).

### Smaller changes

- `gulliver fix -o/--output PATH`: write rewritten SQL to a file when there are changes
  ([#6](https://github.com/laaielichter/Gulliver/issues/6)).
- `gulliver receipt -o PATH` and `gulliver translate -o PATH` write the receipt / translated SQL to a file
  ([#12](https://github.com/laaielichter/Gulliver/issues/12), [#11](https://github.com/laaielichter/Gulliver/issues/11)).
- `gulliver receipt latest` picks the newest receipt within the same second correctly.
- Docs: a read-only Postgres role for Gulliver, with `statement_timeout`
  ([#7](https://github.com/laaielichter/Gulliver/issues/7)); feature guides in `docs/features/`.

## 0.2.0 - 2026-11-02

- New `gulliver_fix` (MCP) / `gulliver fix` (CLI): rewrites SQL to be cheaper and labels every change with a
  guarantee (`same`, `assumes`, `nulls`, `changes`). By default only `same`: date filters such as
  `DATE(ts) = '...'`, `ts::date`, `DATE_TRUNC`, `YEAR`/`EXTRACT(YEAR ...)` become ranges on the raw column when the
  connection shows a DATE or TIMESTAMP-without-zone column. `mode=all` adds the same rewrite for unknown types
  (`assumes`), NOT IN → NOT EXISTS, approximate distinct counts, UNION ALL and a LIMIT. With a connection it
  estimates before and after.
- `gulliver_preflight` includes `autofix.fixed_sql` when a same-rows rewrite exists, so the agent gets the fix
  without an extra call.
- The MCP server reports its version to clients (mcp 2.x).
- npm launcher `gulliver-mcp` (`npx -y gulliver-mcp`), which runs the matching PyPI version through uv.
- `server.json` for the official MCP Registry; CONTRIBUTING.md and CODE_OF_CONDUCT.md.
- Claude Code plugin installs the MCP server from PyPI (`uvx gulliver-mcp`).

## 0.1.0 - 2026-11-01

First release.

Query rehearsal:
- Workflow: `status`, `inspect`, `lint`, `preflight`, `rehearse`, `run`, `receipt`, `approve`.
- Rehearsal ladder (1% → 3% → 10%), power-law extrapolation with confidence ranges, detection of super-linear growth,
  saturation cap for grouped results.
- 12 scale lint rules (GV001-GV012).
- Budget gate: per query, per day, bytes, runtime, rehearsal requirement and human approval.

Pipeline tools:
- `impact` / `lineage`: column-level lineage over SQL model folders (dbt Jinja supported) or a dbt manifest.
- `snapshot` / `drift`: schema drift per connection, mapped to impacted models.
- `diff`: key-based data diff inside the engines with MD5 buckets and a zoom-in pass; works across engines.
- `translate`: SQL between DuckDB, Postgres, BigQuery and Snowflake, with warnings and an optional target preflight.
- `incremental`: append / partition_overwrite / merge_recompute / not_safe advice, generated SQL and dbt config,
  measured savings.
- `check`: unique, not null, accepted values, range, freshness and row-count change in one scan (exit code 4 on fail).

Engines and integration:
- DuckDB and PostgreSQL tested (Postgres live in CI), BigQuery and Snowflake beta (tested with fakes).
- MCP server (stdio and Streamable HTTP), CLI with `--json`, OpenAI and Gemini tool export.
- Claude Code plugin and agent skill.
