# `gulliver top`: the most expensive repeated queries, with priced fixes

**The problem.** Teams rarely know which repeated queries burn the money. FinOps dashboards show what ran and what
it cost, but not the rewrite. Gulliver's other tools look at one new query at a time. `gulliver top` looks back at
the query history, groups the queries by shape, ranks them, and for the top ones gives a rewrite that returns
exactly the same rows, with the saving per month.

```bash
gulliver top                                   # the connection's own history (see "Sources")
gulliver top --days 30 --limit 10
gulliver top --source receipts                 # only what ran through gulliver run
gulliver top --source csv:history.csv          # or jsonl:FILE, or --file FILE
gulliver top --file bq_export.jsonl --dialect bigquery
gulliver top --json                            # for agents and scripts
```

MCP: `gulliver_top` (arguments `source`, `connection`, `path`, `days`, `limit`, `analyze`, `rank_by`, `dialect`).

## Sources

| source | reads | money per query |
|---|---|---|
| `receipts` | Gulliver's own `gulliver run` receipts on this connection (no query needed) | what Gulliver measured at run time |
| `pg_stat_statements` | Postgres statement statistics for the current database | none (self-hosted): ranked by runtime |
| `bigquery_jobs` | `` `region-<location>`.INFORMATION_SCHEMA.JOBS_BY_PROJECT ``, successful `QUERY` jobs, `SCRIPT` parents left out so nothing is counted twice | bytes billed x the on-demand price |
| `snowflake_history` | `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`, joined to `QUERY_ATTRIBUTION_HISTORY` when the role can read it | attributed credits x `credit_usd`; otherwise execution time x warehouse size (heuristic) |
| `file` | CSV with a header, JSON lines or a JSON list. Columns: `query` (required), `calls`, `bytes`, `runtime_s`, `cost_usd`, optional `hash` | from the file |

`auto` (the default) picks `pg_stat_statements`, `bigquery_jobs` or `snowflake_history` for those engines, and
`receipts` for DuckDB. Each history query goes through the same budget gate as every query Gulliver writes itself:
it is estimated first, can be blocked or need a human approval, and its cost goes into the spend ledger.

## What it does

1. **Fingerprints.** Literals and parameters (`42`, `'NL'`, `$1`, `@p`, `?`) become `?`, IN lists collapse to one
   `?`, comments and case are ignored. So `WHERE day = '2026-10-01'` and `WHERE day = '2026-10-02'` are one group.
   SQL that sqlglot cannot parse falls back to the engine's hash (BigQuery `normalized_literals`, Snowflake
   `query_parameterized_hash`, Postgres `queryid`).
2. **Ranks** the groups by cost (or runtime when nothing has a price; `--rank-by` changes it) with calls, calls
   per day, share of the total and cost per month.
3. **For the top groups** (`--analyze`, default 10): scale lint, the safe rewrite from `gulliver fix` (same rows
   only), and an estimate of the original and the rewrite (dry run / EXPLAIN, no data read). The saving per month
   is `(1 - estimate after / estimate before) x observed cost per month`. `--analyze 0` lists the top groups
   without analysing them; `--limit` below 1 and `--analyze` below 0 are refused instead of silently changing
   the list.
4. **Summary-table candidates** (`materialize`): aggregates that run at least 10 times a day and scan about the
   same bytes each time. Priced as runs per month x cost per run, minus one refresh a day.

## Example (BigQuery history, illustrative numbers)

```text
2 query shapes (772 calls) from bigquery_jobs, last 7 day(s), ranked by cost. About $2,063.04/month in total.

  #    calls    per month   runtime  share  saving/month           sql
  1      770    $2,062.50  1,540.0s   100%  $2,041.88 exact        SELECT country, COUNT(*) AS n FROM ds.events WHERE DATE(ts) = '2026-10...
  2        2      $0.5400      4.0s     0%  -                      SELECT * FROM ds.users

-- #1 GV005: same rows, scans 99% less
SELECT
  country,
  COUNT(*) AS n
FROM ds.events
WHERE
  ts >= '2026-10-01' AND ts < '2026-10-02'
GROUP BY
  country

-- #2 needs a human decision: GV001 select-star; GV002 unbounded-output

Repeated aggregates that could read a summary table (heuristic):
  a8b74dbc8c62c0ce  110 calls/day  saves ~$2,043.75/month  SELECT country, COUNT(*) AS n FROM ds.events WHERE DATE(ts)

next: Biggest priced fix: #1 saves about $2,041.88/month (exact). Its fixed_sql returns exactly the same rows;
preflight it and swap it in where the query is defined. 1 repeated aggregate(s) could read a summary table instead
(see materialize; heuristic).
```

The JSON has the same data per group: `fingerprint`, `calls`, `calls_per_day`, `variants`, `cost_usd`,
`cost_per_month_usd`, `share_pct`, `sql` (the most expensive example), and `analysis` with `lint`, `fix`
(`fixed_sql`, `rules`, `same_result`), `estimate.before/after` and `saving` (`scan_saved_pct`, `per_month_usd`,
`accuracy`, `basis`).

## Guarantees

- Only rewrites that return exactly the same rows are offered (the `safe` mode of `gulliver fix`). The date
  rewrite needs the column type from the connection's metadata.
- A saving is labeled `exact` only when both estimates are the engine's own exact numbers (BigQuery dry runs).
  Everything else is `heuristic`. Runtime savings are always `heuristic`: they assume runtime falls with bytes.
- When the estimate does not move (DuckDB native tables, no partitions), Gulliver says so and claims no saving.
- Every history query is budget-gated, capped by the engine where it can be (BigQuery `maximum_bytes_billed`) and
  recorded in the ledger. Nothing is written.

## Limits

- Per-month numbers assume the next 30 days repeat the window that was read. `exact` describes the estimates, not
  the future workload. A window shorter than one day (right after a `pg_stat_statements` reset) gets no per-month
  numbers and no summary-table candidates; a note says how many hours were read.
- The fix is computed on one example per group. The other literal variants have the same shape, but a rewrite that
  depends on a literal (a date range) is shown for that example.
- `pg_stat_statements` counts since its last reset, not per day. With Postgres 14+ the reset time comes from
  `pg_stat_statements_info`; without it there is no per-month projection. Its queries carry `$1` parameters instead of
  literals, so the date rewrite does not apply to them; lint still runs. Text such as `IN ($1,$2)` cannot be parsed
  (sqlglot reads `$1,$` as a dollar quote): that group is still ranked, with `analysis.kind` `block` and the reason.
  Session and utility statements (SET, EXPLAIN, ...), system catalog lookups and reads of `pg_stat_statements`,
  Gulliver's own included, are left out; a note counts them. Bytes are shared buffer blocks x 8 KiB. The
  extension must be in `shared_preload_libraries`; without it `top` returns an error that says so.
- BigQuery: the money is the on-demand price. With capacity (slot) pricing it follows slot time instead. Jobs are
  read for the connection's `location` only. The role needs `bigquery.jobs.listAll`.
- Snowflake: `ACCOUNT_USAGE` needs a privileged role (`SNOWFLAKE.GOVERNANCE_VIEWER`, plus `SNOWFLAKE.USAGE_VIEWER`
  for attribution) and lags up to 45 minutes (attribution up to 8 hours). Attributed credits leave out warehouse
  idle time. Runs without an attribution row (recent or very short ones) count with the estimate, and the cost basis
  says "credits attributed per query" only when every run of every group was attributed.
- `receipts` only knows queries that ran through `gulliver run`.
- A file is taken to cover `--days` days. CSV may be saved with a UTF-8 byte order mark (Excel) and may hold
  multi-line SQL in quoted fields. Rows whose numbers do not parse (or are `inf` / `nan`) are skipped and counted.
- Summary-table pricing assumes one refresh costs about one run and that reading the summary is close to free.
  Groups with many literal variants need the filtered columns as GROUP BY keys, and such a refresh can cost more.
  This saving and the rewrite saving for the same query overlap; do not add them up.
- Query text from history is returned to the agent, and it can contain literal values.
