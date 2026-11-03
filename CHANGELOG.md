# Changelog

## Unreleased

### Fixed
- `gulliver_drift` is no longer annotated read-only. It saves a local snapshot file (on the first call, and with `save=true`), so it now reports `readOnlyHint: false`, `destructiveHint: false`. It still only reads the database. Reported by Glama's tool scoring.
- New test `tests/test_tool_annotations.py`: read-only tools may not describe writes.
- `gulliver_check` is no longer annotated read-only either: it stores the row count locally for the next `max_change_pct` comparison.

### Changed
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
