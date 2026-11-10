<!-- Generated against Gulliver 0.3.0 on the demo database (200,000 events). Row counts and timings are approximate and will vary with sampling. -->

# Tutorial: the Gulliver loop on the demo database

New here? This page walks through one complete task on the bundled demo database: inspect, preflight, lint, fix, rehearse, run, and receipt. No warehouse is required.

The demo has two tables:

- `events` — about 200,000 rows in this run (default is 5 million). Columns: `event_id`, `user_id`, `event_type`, `amount`, `ts`.
- `users` — about 100,000 rows. Smaller companion table.

## 1. Install and create the demo

```bash
pip install gulliver-mcp
gulliver demo --events 200000
export GULLIVER_DSN=gulliver_demo.duckdb
```

You should see a JSON result with `"status": "ok"` and the two table counts. The next step suggested is `gulliver inspect`.

## 2. Inspect the data

```bash
gulliver inspect
```

Typical output (row counts and sizes vary):

```
table                                              rows        bytes  partitioned_by
events                                           200000      10.7 MB  
users                                            100000       3.4 MB  

next: Inspect one table for its columns, or call gulliver_preflight with your SQL.
```

Look at one table:

```bash
gulliver inspect events
```

```
events: 200000 rows, 10.7 MB
  event_id                       BIGINT
  user_id                        BIGINT
  event_type                     VARCHAR
  amount                         DOUBLE
  ts                             TIMESTAMP

next: Write your SQL using only the columns you need, then call gulliver_preflight.
```

## 3. Preflight a first query

```bash
gulliver preflight "SELECT count(*) FROM events" --json
```

You get a structured result. Key fields:

- `"status": "ok"` (or `warn`, `block`, `needs_rehearsal` — see the status table in `AGENTS.md`)
- estimate of rows, cost, and which tables are touched
- a `next_step` that tells you what to do next

A simple aggregation like this is usually `ok`.

## 4. Lint a problematic query and fix a date filter

A cartesian product is blocked:

```bash
gulliver lint "select * from events e, users u"
```

```
error GV003 cartesian-join: Join with u has no join condition: a cartesian product.
      fix: Add ON <key> = <key> (or USING). If you really want every combination, write CROSS JOIN.
warn  GV001 select-star: SELECT * reads every column. ...
warn  GV002 unbounded-output: The result has no LIMIT and no aggregation, so it can return every row.
```

Now rewrite a date filter that prevents pruning:

```bash
gulliver fix "SELECT * FROM events WHERE date(ts) = '2025-01-20'"
```

```
GV005 [returns exactly the same rows]
  before: CAST(ts AS DATE) = '2025-01-20'
  after:  ts >= '2025-01-20' AND ts < '2025-01-21'
  why:    Filters the raw column ts by range, so partitions, indexes and min/max statistics can skip data. ts is TIMESTAMP, so the rows are the same.

-- fixed SQL
SELECT
  *
FROM events
WHERE
  ts >= '2025-01-20' AND ts < '2025-01-21'
```

The message “returns exactly the same rows” is the guarantee. Remaining warnings are still listed.

## 5. Rehearse a self-join

```bash
gulliver rehearse "select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id"
```

You will see:

- an estimate before any sampling
- scale lint warnings
- a table of 1%, 3%, and 10% sample runs (runtime, output rows)
- a prediction for 100% with a confidence level and range
- a gate section that flags “multiplies rows” when the join is not unique

Treat low-confidence predictions as ranges, not exact numbers.

## 6. Run and look at the receipt

```bash
gulliver run "SELECT count(*) as cnt FROM events"
```

A receipt is written. Then:

```bash
gulliver receipt --list
gulliver receipt
```

`--list` shows recent receipts (newest first). A bare `gulliver receipt` shows the most recent one in full: the SQL, the estimate, the actual runtime and rows, and the next step.

## 7. Where to go next

- Feature pages in `docs/features/` (lint, check, diff, review, …)
- Budget and connection settings in `gulliver.toml` (see `gulliver.example.toml`)
- `AGENTS.md` for the status values and the loop description
- The MCP server for use inside Claude Code, Cursor, or any MCP client

That is the whole loop on the demo database. Every command above was run on Gulliver 0.3.0.
