# Changelog

## Unreleased

### Fixed
- `check` with no quality-check options records row-count history but explicitly says no checks were requested (`checks_requested: 0`), rather than claiming all checks passed (#130).

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
