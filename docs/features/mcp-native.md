# MCP-native: approve in the client, resources, prompts, progress, smaller tool sets

Gulliver started as a set of MCP tools. This release uses more of the protocol, so a client can show the human
an approval form, attach context without a tool call, start the loop from a prompt, show rehearsal progress and
load fewer tool schemas.

## The problem

- `needs_approval` makes the human switch to a terminal and type `gulliver approve <id>`. Teams then raise the
  auto-approve limit to avoid it, which is the opposite of what the limit is for.
- Clients that never read AGENTS.md do not know the loop (inspect, preflight, rehearse, run, receipt).
- Agents call `gulliver_inspect` again and again for the same context.
- All 16 tool schemas take about 25 KB of the agent's context, even when the task only needs the query loop.
- A rehearsal on a slow warehouse looks like a hung call.

## Approval in the MCP client (elicitation), opt-in

A human turns it on in `gulliver.toml`. There is no environment variable and no tool argument for it.

```toml
[approval]
channel = "elicitation"   # default "cli": a human runs `gulliver approve <receipt_id>`
```

When `gulliver_run` would answer `needs_approval` (over the auto-approve limit, or the runaway guard's
velocity or repeat check), the server first sends the client a form (MCP elicitation).
The client shows it to its user:

```text
Gulliver: approve this query?

READ-ONLY query (reads events, users).

An agent wants to run a query that costs more than your auto-approve limit.
This agent is spending fast: over the velocity limit $2.00 within 10 minutes.
- Connection: local (duckdb)
- Cost: $3.84 (auto-approve limit $1.00, hard limit $100.00)
- Scan: 3.7 MB (exact estimate)
- SQL fingerprint: bdd676276a901c63

SQL (comments removed):
SELECT
  u.country,
  COUNT(*) AS n
FROM events AS e
...

Statement: READ-ONLY query (reads events, users).

Only you can approve this; the agent cannot. Tick 'approve' and accept to run it now. ...
```

The first line under the title says what the statement does, taken from the parsed statement: `READ-ONLY query`,
or for a write the verb and the target, e.g. `WRITE: CREATE OR REPLACE TABLE users (reads events, users). It
replaces that object and everything in it.` It is repeated under the SQL. The SQL is shown re-rendered from the
parsed statement with its comments removed, so a comment cannot hide the statement or add lines that look like
Gulliver's. The SQL is always shown in full: when it is longer than 4,000 characters, Gulliver does not ask in
the client at all (see below).

With a rehearsal on file, the form also shows the predicted runtime, rows and cost range with the confidence.
The form has two fields: `approve` (a checkbox, off by default) and an optional `note`.

- **Accept with approve ticked**: Gulliver records the approval with `channel: "elicitation"` (and the client's
  self-reported name), then runs. Every other gate still applies. The receipt says
  "Approved by a human in the MCP client (elicitation, client `my-client`)".
- **Decline, cancel, or accept without ticking approve**: the answer is `needs_approval` as before, with
  `approval_request: {"channel": "elicitation", "answer": "declined"}` saved in the receipt, and a `next_step`
  that tells the agent not to retry the same SQL. `gulliver approve <id>` still works.
- **The client answers the form with an error** (dialog closed, timed out, cannot render it): `needs_approval`
  with `approval_request.answer: "error"` and the client's message as the reason, saved in the receipt.
  Odd form content never fails the call: only a real `true` for `approve` approves, and a note over 500
  characters is cut.
- **The client cannot show forms** (it did not declare the elicitation capability): `needs_approval`, with
  `approval_request.answer: "not asked"` and the reason.
- **Not asked for another reason**: `answer: "not asked"` with a reason that says why, e.g. the SQL is too long
  to show in full, or an approval would not let the run go ahead (the session or agent budget of the runaway
  guard is used up).
- **The cost went up between the question and the run**: nothing is approved (`answer: "stale"`).

```json
{
  "status": "ok",
  "approval": {
    "receipt_id": "gv-20261102-181951-run-f501", "sql_hash": "bdd676276a901c63", "connection": "local",
    "approved_at": "2026-11-02T18:19:51+00:00", "expires_at": "2026-11-03T18:19:51+00:00",
    "max_cost_usd": 3.84, "note": "ok for Q3 report", "channel": "elicitation", "client": "my-client"
  },
  "next_step": "Done. Show the human the receipt (gulliver_receipt) if they want proof."
}
```

The human is only asked about runs that their approval would unblock: a run that is blocked, needs a rehearsal
first, is over the per-query or daily limit, is over a session or agent budget, or is cheap enough to run anyway
never shows a form. The check before asking runs the same gates as `gulliver_run`, including the runaway guard
(it reads ledger and table metadata, no table data).

## Resources

Read-only context a client can attach without a tool call (metadata only, no table data is read):

| URI | content |
|---|---|
| `gulliver://status` | connections, budget, money spent today (JSON, same as `gulliver_status`) |
| `gulliver://calibration` | observed rehearsal range coverage by metric and confidence (JSON, same as `gulliver calibration --json`) |
| `gulliver://rules` | the loop, status values, hard rules, lint rules and this server's budget (markdown) |
| `gulliver://connections/{connection}/tables` | the biggest tables: rows, bytes, partitions (JSON) |
| `gulliver://connections/{connection}/tables/{table}` | one table's columns and partitioning (JSON) |
| `gulliver://receipts/{receipt_id}` | a receipt as markdown; `latest` for the newest |

## Prompts

| prompt | arguments | what it starts |
|---|---|---|
| `safe-query-loop` | `question`, `connection` | inspect, preflight (with autofix), rehearse, run, receipt |
| `refactor-and-prove` | `sql`, `key`, `goal`, `connection` | `gulliver_fix`, preflight before vs after, `gulliver_diff` |
| `make-incremental` | `sql`, `time_column`, `unique_key`, `connection` | `gulliver_incremental`, then `gulliver_diff` |

A server started with a smaller tool set only offers the prompts whose tools it has.

## Progress

`gulliver_rehearse` sends one MCP progress notification per finished sample (`1 of 3`, `2 of 3`, `3 of 3`,
with a message like `1% sample: 4,021 rows in 0.03s`) when the client asks for progress.

## Smaller tool sets

```bash
gulliver-mcp --toolset core                  # status, inspect, lint, fix, preflight, rehearse, run, receipt
gulliver-mcp --toolset pipeline              # status, inspect, receipt + impact, drift, diff, translate, incremental, check, top, review
gulliver-mcp --toolset core,gulliver_check   # mix sets and tool names
gulliver mcp --toolset core                  # same through the main CLI
gulliver tools --format openai --toolset core
```

Sizes measured with mcp 2.3.0 (`tools/list` JSON): all 16 tools 25.6 KB, `core` (8 tools) 10.9 KB, `pipeline`
(11 tools) 17.8 KB. Input schemas only (`gulliver tools`): 19.2 KB, 7.6 KB and 13.6 KB.

## Typed output

Every tool now publishes an `outputSchema` in which `status` is an enum of the eight status values and `status`
and `next_step` are required; all other fields are allowed. `structuredContent` and the JSON text block carry
the same object, so clients that only read text see no change. mcp 2.x already sent `structuredContent` with an
open schema before; the typed schema adds about 200 bytes per tool.

`gulliver_run` is marked `destructiveHint: true` when any connection in `gulliver.toml` has `allow_write = true`.

## Guarantees

- Off by default. Only a human can turn approval in the client on, in `gulliver.toml`.
- The agent cannot approve: the form is a server-to-client request answered by the client, and the two
  parameters that carry it are filled by the server, never by tool arguments (forged arguments are ignored; this
  is tested).
- An approval is bound to the exact SQL fingerprint and connection and holds 24 hours, like `gulliver approve`.
- Every gate in `gulliver_run` still runs after the approval (limits, rehearsal, writes).
- Works on both MCP protocol styles in mcp 2.x: a mid-call request (2025-11-25 and older) and the
  `input_required` round trip (2026-07-28). Tested in-memory on both, and over a real stdio subprocess.

## Limits

- Some clients may answer forms automatically, or let the model answer them. Turn this on only for clients that
  show elicitation forms to a person. The client name in the receipt is self-reported, not a proof of identity.
- Needs mcp >= 2. With mcp 1.x the server falls back to `needs_approval` (the CLI channel), untested there.
- With the channel on, each `gulliver_run` does one extra dry estimate and runaway check before the run (no
  table data read; the data-version signal reads table metadata); on the 2026-07-28 round trip it is two.
- Progress is best effort: a ladder that stops early ends before `3 of 3`. Cancelling a rehearsal does not yet stop
  the engine job.
- URL-mode elicitation is not used.
- On the 2026-07-28 round trip, a client whose form fails aborts the `tools/call` itself (it raises instead of
  answering), so the server never hears of it and there is no `receipt_id`; the agent gets a client error. Call
  `gulliver_run` again, or approve on the command line after a `needs_approval` on a client without forms.
