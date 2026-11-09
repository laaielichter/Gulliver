# Runaway guard: stop loops, budget each agent and session, tag every query

**The problem.** Agents retry. They re-run the same expensive query across turns, call each other in loops, and
one busy agent can use up the whole team's daily budget before anyone notices. A daily total reacts too late,
BigQuery cannot set a quota per agent, and FinOps teams cannot see which agent spent what.

Gulliver now knows who is asking and checks every execution (full runs, rehearsal rungs and the internal
queries of `diff`, `check` and `incremental`) just before it runs:

| Guard | Default | When it trips | Status |
|---|---|---|---|
| `max_repeats` / `repeat_window_min` | 5 runs / 30 min | the same query on the same connection runs again, the tables did not change | `block`, reason `loop`, with the last result in `cached` (free) |
| | | ... and Gulliver cannot tell whether the data changed | `needs_approval`, reason `repeat` |
| `velocity_usd` / `velocity_window_min` | $2 / 10 min | one agent (across all its sessions) or one session spends faster than this | `needs_approval`, reason `velocity` |
| `session_usd` | off | one session spent its budget | `block`, reason `session_budget` |
| `agent_day_usd` | off | one agent spent its budget for today | `block`, reason `agent_budget` |

Repeats that are cheap (under $0.01 **and** under 1 GB) are never treated as a loop, so looking at a small
table again and again just works. When the estimate has no byte count (`count(*)`, `select 42`), the size of
the tables it reads from metadata is used instead (a DuckDB view counts as the tables it reads); a query on
a table or view of unknown size (a BigQuery or Snowflake view, a remote file) is not cheap.
A repeat is also allowed when the tables changed since the last run. "The same query" ignores case,
whitespace, a trailing `;` and comments, so `-- retry 2` does not make it a new query.
A human approval (`gulliver approve <receipt_id>`) for a query buys it another `max_repeats` runs and lifts
the velocity check for that query for the length of the window. It only counts as approval of the run's cost
when it was given for at least that cost: approving a rehearsal's velocity request does not approve the full
run. The per-query and per-day limits still apply to everyone.

## Who is asking

| | Where it comes from (first one set wins) |
|---|---|
| agent | `GULLIVER_AGENT` (set by the human) > MCP `clientInfo.name` > `gulliver --agent NAME` > `cli` |
| session | MCP `Mcp-Session-Id` header > `GULLIVER_SESSION` > the MCP server process + client name. CLI calls have no session unless `GULLIVER_SESSION` is set. |
| task | `GULLIVER_TASK` (set by the human), e.g. the ticket the agent works on |

Every ledger line now records `agent`, `session`, `task`, the `tables` read, the `receipt` and the
`data_version` it saw. `gulliver status` shows the caller's own spend and limits under `caller`.

## Configure

```toml
[budget]
session_usd = 3                                   # per agent session (0 = off)
agent_day_usd = { default = 5, "claude-code" = 10 } # or one number for every agent (0 / absent = off)
max_repeats = 5                                   # 0 = no loop guard
repeat_window_min = 30
velocity_usd = 2.0                                # 0 = no velocity check
velocity_window_min = 10
```

Environment variables: `GULLIVER_SESSION_USD`, `GULLIVER_AGENT_DAY_USD` (one number), `GULLIVER_MAX_REPEATS`,
`GULLIVER_REPEAT_WINDOW_MIN`, `GULLIVER_VELOCITY_USD`.

## A loop, stopped

```console
$ gulliver run --agent bot --json "select country, count(*) n from users group by 1"   # 6th time in 30 min
```

```json
{
  "status": "block",
  "reason": "loop",
  "runaway": {"agent": "bot", "repeats": 5, "window_min": 30.0, "reason": "loop"},
  "cached": {
    "cached": true,
    "receipt_id": "gv-20261102-180640-run-caff",
    "ran_at": "2026-11-02T18:06:40+00:00",
    "columns": ["country", "n"],
    "preview": [{"country": "NL", "n": 12500}, "..."],
    "rows": 8
  },
  "next_step": "Loop: this exact query already ran 5 times in the last 30 minutes and the tables have not changed, so it would return the same result. Use `cached.preview` (the last run's result, free) instead. Change the query if you need something else, or ask the human why it repeats."
}
```

## How "the tables did not change" is decided

Gulliver only reuses a result when the engine gives a reliable change signal, and treats everything else as
unknown (so it asks a human instead of blocking):

| Engine | Change signal | No signal (treated as unknown) |
|---|---|---|
| DuckDB | mtime + size of the database file and its WAL; of each file read with `read_parquet(...)` etc. or `FROM 'x.parquet'` | in-memory databases, views, attached databases, remote files, names that are neither a table nor a local file |
| BigQuery | `lastModifiedTime`, rows and bytes of each table | views, external and wildcard tables, tables with a streaming buffer |
| Snowflake | `LAST_ALTERED` (moves on every DML and DDL), rows and bytes | views, external and dynamic tables, tables in another database |
| Postgres | none: `pg_stat_user_tables` counters are updated asynchronously, so they can lag right after a write | always unknown |

Queries with `NOW()`, `CURRENT_DATE`, `RANDOM()`, `UUID()`, `TABLESAMPLE`, `get_current_timestamp()`,
`uuidv4()`/`uuidv7()`, `nextval()`, `txid_current()`, or any function whose name contains `current`, `random`,
`uuid`, `nextval`, `txid`, `sysdate`, `getdate` or `clock`, never count as unchanged.

## Query tags

Each allowed execution is labelled where the warehouse can store it, so its own billing views show agent spend:

| Engine | Tag |
|---|---|
| Postgres | `application_name = gulliver:<receipt_id>:<agent>` (visible in `pg_stat_activity` and the logs) |
| Snowflake | `QUERY_TAG` = JSON `{"tool": "gulliver", "kind", "agent", "session", "task", "receipt"}` (in `QUERY_HISTORY`) |
| BigQuery | job labels `gulliver`, `gulliver_agent`, `gulliver_session`, `gulliver_task`, `gulliver_receipt` (in `INFORMATION_SCHEMA.JOBS`) |
| DuckDB | none (DuckDB keeps no query history to read a tag from) |

## Spend report

```console
$ gulliver spend --by agent            # also: day, table, session, task, connection, kind
# Gulliver spend by agent since 2026-10-27

| agent | Money | Executions | Full runs | Bytes |
|---|---|---|---|---|
| claude-code | $4.20 | 31 | 12 | 610.2 GB |
| nightly-job | $0.9000 | 4 | 4 | 140.0 GB |
| **total** | $5.10 | 35 | | 750.2 GB |

$ gulliver spend --by day --days 30 --format csv -o spend.csv
$ gulliver spend --by table --json
```

`--by table` splits a query that reads several tables evenly between them, so the rows add up to the total.
Lines written before this version have no agent and show as `(unknown)`. Current lines without a session or
task (CLI calls without `GULLIVER_SESSION` / `GULLIVER_TASK`), and queries that read no table, show as `(none)`.
`--days` below 1 is refused with the standard error result instead of reporting a window you did not ask for.

## Audit chain

Every ledger line carries the hash of the line before it, and every saved receipt and approval gets a chained
line in `.gulliver/audit.jsonl` with the sha256 of the file as written.

```console
$ gulliver audit verify
{"status": "ok", "ledger": {"entries": 35, "chained": 35, "head": "9343ac02..."},
 "receipts": {"entries": 80, "files_checked": 52, "head": "fde31855..."}, ...}

$ gulliver audit verify --head 9343ac02...     # later: prove nothing up to that point changed
```

It reports an edited, removed, reordered or inserted ledger line, and a receipt or approval file that changed
or disappeared after Gulliver wrote it (`status: fail`, exit code 4). A receipt or approval file with no audit
entry that is dated after the chain started (written by hand, or `audit.jsonl` was deleted), and a line without
a hash that is newer than the chain, are also a `fail`. An approval file without a matching audit entry is
ignored by `gulliver_run`, so a hand-written approval approves nothing. Lines and files from before the chain
existed are counted and reported as a warning; verify only says "Intact" when there are no warnings.

### Pruning old receipts (`gulliver prune`)

Every preflight, rehearsal, run, check and diff writes a JSON receipt to `.gulliver/receipts/`. Over time,
this folder grows. Deleting receipts by hand causes `gulliver audit verify` to fail because the missing files
are detected by the audit chain.

`gulliver prune` safely cleans up old receipts and expired approvals while preserving the integrity of the
audit trail:

```console
$ gulliver prune --older-than 30d --dry-run
[dry-run] Pruned 42 receipt(s) and 3 expired approval(s).
Freed: 1.2 MB
Oldest kept receipt: 2026-10-09T14:22:00+00:00

next: Dry run: re-run without --dry-run to remove these files and append audit prune entries.

$ gulliver prune --older-than 30d --keep-last 50
Pruned 42 receipt(s) and 3 expired approval(s).
Freed: 1.2 MB
Oldest kept receipt: 2026-10-09T14:22:00+00:00

next: Pruned. Check the audit trail with gulliver audit verify.
```

- **Receipt protection:** Receipts required by an active approval (`expires_at > now`) or an active rehearsal
  (within `REHEARSAL_TTL`, 24 hours) are never pruned.
- **`--keep-last N`:** Guarantees that at least the N most recent receipts are retained regardless of age.
- **Audit preservation:** For each removed file, `Store._audit` appends a chained entry
  `{"kind": "prune", "id": ..., "sha256": <content_hash>, "reason": ...}` to `audit.jsonl`.
  `gulliver audit verify` recognizes legitimate prune entries, reports the count in its notes, and stays `ok`.
- **Ledger:** `ledger.jsonl` is preserved (spend history and daily budget accounting read it).

## Guarantees and limits

- The agent name and session are self-reported by the client. Per-agent and per-session budgets are
  accounting, not a security boundary: a client can claim another name. `GULLIVER_AGENT` set by the human in
  the MCP server's environment overrides what the client says. The per-query and per-day limits always apply.
- On a stateless MCP transport (HTTP without `Mcp-Session-Id`), clients with the same name in one server
  process share a session. On stdio there is one client per process, so the session is exact.
- Identity from MCP `clientInfo` needs mcp >= 2; with mcp 1.x the agent is `GULLIVER_AGENT` or `mcp`.
- A cached result is the last run's preview (at most `max_output_rows` rows); `cached.rows` is the full count.
- Without `GULLIVER_AUDIT_KEY` the chain is plain sha256: anyone who can write `.gulliver` can rebuild it.
  Set the key somewhere the agent cannot read and the hashes become HMACs that cannot be recomputed without it.
  When the first keyed line of a file is written, `<file>.keyed` (itself an HMAC) records how many lines came
  before it. With the key set, every later line must be keyed, so a chain rebuilt as plain sha256 or stripped
  of its hashes fails; without the marker every line must be keyed. After the key is set, approvals recorded
  before it no longer count.
  Removing the newest lines leaves a shorter valid chain; keep a `head` hash elsewhere and pass `--head` to
  catch that.
- Repeat counting and spend sums read the local ledger, so they cover this machine's `.gulliver` folder only.
- Rehearsals are checked against the session, agent and velocity limits once before the ladder starts; the
  loop guard does not apply to them (a rehearsal is already reused by `gulliver_run` for 24 hours).
