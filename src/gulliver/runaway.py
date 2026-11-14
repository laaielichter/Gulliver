"""Runaway guard: stop agents that loop on the same query or spend too fast, and show who spent what.

Every execution Gulliver makes (run, rehearsal rung, the internal queries of diff/check/incremental) goes
through `check` just before it runs:

  session_usd      a session (one MCP connection, or GULLIVER_SESSION) may spend at most this      -> block
  agent_day_usd    one agent may spend at most this per day (a default plus per-agent values)      -> block
  max_repeats      the same query on the same connection more than this often within
                   repeat_window_min:
                     - cheap (under $0.01 and 1 GB)                       -> runs, with a note
                     - the tables did not change since the last run       -> block, the last result is offered
                     - the tables changed                                 -> runs
                     - no reliable change signal (Postgres, views, NOW()) -> needs_approval
  velocity_usd     one agent/session spending more than this within velocity_window_min           -> needs_approval

A human approval (`gulliver approve <id>`) for the query restarts the repeat count and lifts the velocity
check for that query for the length of the window. Budgets per agent and session are accounting: the
agent name is self-reported, so the per-query and per-day limits still apply to everyone.

Each allowed execution carries tags (Postgres application_name, Snowflake QUERY_TAG, BigQuery job labels),
and its ledger line records agent, session, task, tables, receipt and the data version it saw.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlglot import exp

from . import audit
from .config import Settings
from .core import _error, _fmt_bytes, _fmt_usd
from .engines import Engine, dialect_for
from .identity import Identity, current
from .sqlsafety import Statement, classify
from .store import Store, iso, now_utc, parse_iso

REPEAT_FREE_USD = 0.01          # repeats cheaper than this ...
REPEAT_FREE_BYTES = 1024**3     # ... and smaller than this are never treated as a loop
SPEND_BY = ("agent", "day", "table", "session", "task", "connection", "kind")

_NONDETERMINISTIC = {"CurrentDate", "CurrentTimestamp", "CurrentTime", "CurrentDatetime", "CurrentTimestampLTZ",
                     "Localtimestamp", "Localtime", "Rand", "Randn", "Uuid", "TableSample"}
_NONDETERMINISTIC_NAMES = {"now", "getdate", "sysdate", "systimestamp", "clock_timestamp", "statement_timestamp",
                           "transaction_timestamp", "timeofday", "random", "uuid", "gen_random_uuid", "newid",
                           "generate_uuid", "uuid_string", "current_timestamp", "current_date", "current_time",
                           "localtimestamp", "today", "seq4", "seq8", "random_uuid", "get_current_timestamp",
                           "get_current_time", "get_current_date", "uuidv4", "uuidv7", "txid_current", "nextval",
                           "currval", "lastval", "setseed", "session_user", "current_user", "user", "pg_backend_pid",
                           "unix_timestamp", "current_setting", "current_query", "random_string"}
# any other function whose name says it reads the clock, a sequence or a random source
_NONDETERMINISTIC_PARTS = ("current", "random", "uuid", "nextval", "txid", "sysdate", "getdate", "clock")


# -- ledger ---------------------------------------------------------------------------------
def read_ledger(store: Store) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not store.ledger_path.is_file():
        return out
    for line in store.ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            entry["_at"] = parse_iso(entry["at"])
        except (ValueError, KeyError, TypeError):
            continue
        out.append(entry)
    return out


def _usd(entries: list[dict[str, Any]]) -> float:
    return round(sum(float(e.get("cost_usd") or 0.0) for e in entries), 6)


def agent_limit(settings: Settings, agent: str) -> float | None:
    limits = settings.budget.agent_day_usd or {}
    value = limits.get(agent, limits.get("default"))
    return float(value) if value else None


def _velocity_spent(entries: list[dict[str, Any]], ident: Identity, since: datetime) -> float:
    """Spend within the velocity window of this session and of this agent across all its sessions (a new
    MCP connection or a reconnect must not start an empty window): the larger of the two."""
    recent = [e for e in entries if e["_at"] >= since]
    agent = _usd([e for e in recent if e.get("agent") == ident.agent])
    session = _usd([e for e in recent if e.get("session") == ident.session]) if ident.session else 0.0
    return max(agent, session)


# -- change signal and tags -----------------------------------------------------------------
def deterministic(tree: exp.Expression | None) -> bool:
    """False when the query can return something else on unchanged data (NOW(), RANDOM(), samples...)."""
    if tree is None:
        return False
    for node in tree.walk():
        if type(node).__name__ in _NONDETERMINISTIC:
            return False
        if isinstance(node, exp.Anonymous):
            name = str(node.name).lower()
            if name in _NONDETERMINISTIC_NAMES or any(part in name for part in _NONDETERMINISTIC_PARTS):
                return False
    return True


def data_version(engine: Engine, sql: str, statement: Statement | None = None) -> str | None:
    """A short fingerprint of the state of the tables the query reads, or None without a reliable signal."""
    statement = statement or classify(sql, dialect_for(engine.name))
    if statement.kind == "block" or not statement.tables or not deterministic(statement.select):
        return None
    try:
        raw = engine.data_version(statement.tables)
    except Exception:  # a change signal must never break a run
        return None
    return hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else None


def tags(kind: str, receipt_id: str | None = None) -> dict[str, str]:
    ident = current()
    out = {"tool": "gulliver", "kind": kind, "agent": ident.agent}
    if ident.session:
        out["session"] = ident.session
    if ident.task:
        out["task"] = ident.task
    if receipt_id:
        out["receipt"] = receipt_id
    return out


# -- the guard ------------------------------------------------------------------------------
def _approval(store: Store, sql_hash: str, connection: str) -> tuple[str | None, datetime | None]:
    approval = store.find_approval(sql_hash, connection)
    try:
        return (approval["receipt_id"], parse_iso(approval["approved_at"])) if approval else (None, None)
    except (KeyError, ValueError):
        return None, None


def _cached(store: Store, entry: dict[str, Any]) -> dict[str, Any] | None:
    rec = store.load_receipt(entry["receipt"]) if entry.get("receipt") else None
    if not rec or rec.get("status") not in ("ok", "warn"):   # warn = ran, with result-guard data warnings
        return None
    if rec.get("sql_hash") != entry.get("sql_hash") or rec.get("connection") != entry.get("connection"):
        return None   # not the receipt of this query (never hand back another query's result)
    out = {"cached": True, "receipt_id": rec.get("receipt_id"), "ran_at": rec.get("created_at") or entry.get("at"),
           "columns": rec.get("columns"), "preview": rec.get("preview"), "truncated": rec.get("truncated"),
           "rows": (rec.get("actual") or {}).get("rows")}
    # the receipt holds the preview as the result guard left it (masked); keep its untrusted-data envelope too
    for key in ("untrusted", "data_notice", "data_warnings", "sensitive_columns", "injection"):
        if rec.get(key) is not None:
            out[key] = rec[key]
    return out


def _metadata_bytes(engine: Engine, tables: list[str], depth: int = 0) -> int | None:
    """The size of what the query reads when the estimate has no byte count (count(*), constants): the sum of
    the referenced tables from metadata (a DuckDB view counts as the tables it reads), 0 for a query without
    tables, None when a table's size is unknown."""
    total = 0
    for ref in tables:
        try:
            info = engine.table_info(ref)
            sources = None
            if (info is None or info.bytes is None) and depth < 5 and hasattr(engine, "view_sources"):
                sources = engine.view_sources(ref)
        except Exception:  # metadata must never break a run
            return None
        if sources is not None:
            size = _metadata_bytes(engine, sources, depth + 1)
            if size is None:
                return None
            total += size
            continue
        if info is None or info.bytes is None:
            return None
        total += int(info.bytes)
    return total


def check(settings: Settings, engine: Engine, sql: str, *, connection: str, sql_hash: str, kind: str,
          cost: float | None, bytes_: int | None, receipt_id: str, repeats: bool = True,
          receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decide whether this execution may go ahead. Returns
         {"status": "ok", "ledger": {...fields for the spend line}, "extra": {...}}   and sets engine.tags, or
         {"status": "block" | "needs_approval", "next_step": ..., "extra": {...}}
    When `receipt` is given and the answer is not ok, that receipt is saved under receipt_id."""
    budget = settings.budget
    store = Store(settings.state_dir)
    ident = current()
    now = now_utc()
    entries = read_ledger(store)
    statement = classify(sql, dialect_for(engine.name))
    tables = statement.tables
    info: dict[str, Any] = {"agent": ident.agent}
    if ident.session:
        info["session"] = ident.session

    def stop(status: str, reason: str, next_step: str, **more: Any) -> dict[str, Any]:
        extra = {"reason": reason, "runaway": {**info, "reason": reason}, **more}
        if status == "needs_approval":
            extra["receipt_id"] = receipt_id
            next_step += f" A human approves with `gulliver approve {receipt_id}`; then call again."
        if receipt is not None:
            store.save_receipt({**receipt, "receipt_id": receipt_id, "status": status, "next_step": next_step,
                                **extra})
        return {"status": status, "next_step": next_step, "extra": extra}

    # 1. budgets per session and per agent (blocks, like the day budget)
    if budget.session_usd and ident.session:
        spent = _usd([e for e in entries if e.get("session") == ident.session])
        if spent + (cost or 0.0) > budget.session_usd or spent >= budget.session_usd:
            return stop("block", "session_budget",
                        f"This session's budget is used up: spent {_fmt_usd(spent)}"
                        + (f" + {_fmt_usd(cost)}" if cost else "") + f" > {_fmt_usd(budget.session_usd)}. "
                        "Stop and tell the human; a new session or a higher session_usd is their call.")
    limit = agent_limit(settings, ident.agent)
    if limit:
        today = now.date()
        spent = _usd([e for e in entries if e.get("agent") == ident.agent and e["_at"].date() == today])
        if spent + (cost or 0.0) > limit or spent >= limit:
            return stop("block", "agent_budget",
                        f"Agent '{ident.agent}' has used its daily budget: spent {_fmt_usd(spent)}"
                        + (f" + {_fmt_usd(cost)}" if cost else "") + f" > {_fmt_usd(limit)}. "
                        "Stop and tell the human (agent_day_usd in gulliver.toml).")

    version = data_version(engine, sql, statement)
    approval_id, approved_at = _approval(store, sql_hash, connection)

    # 2. the same query again and again
    if repeats and budget.max_repeats > 0:
        start = now - timedelta(minutes=budget.repeat_window_min)
        same = [e for e in entries if e.get("kind") == kind and e.get("sql_hash") == sql_hash
                and e.get("connection") == connection and e["_at"] >= start]
        if approval_id and approved_at and approved_at >= start:
            # a human approval buys another max_repeats runs: count only the runs made under it
            same = [e for e in same if e.get("approval") == approval_id]
        if same:
            info["repeats"] = len(same)
            info["window_min"] = budget.repeat_window_min
        if len(same) >= budget.max_repeats:
            last = same[-1]
            window = f"{len(same)} times in the last {budget.repeat_window_min:g} minutes"
            size = bytes_ if bytes_ is not None else _metadata_bytes(engine, tables)
            cheap = cost is not None and cost <= REPEAT_FREE_USD and size is not None and size <= REPEAT_FREE_BYTES
            if cheap:
                info["note"] = f"ran {window}; allowed because it is cheap"
            elif version is not None and last.get("data_version") == version:
                cached = _cached(store, last) if kind == "run" else None
                more = {"cached": cached} if cached else {}
                return stop("block", "loop",
                            f"Loop: this exact query already ran {window} and the tables have not changed, so it "
                            "would return the same result. "
                            + ("Use `cached.preview` (the last run's result, free) instead. " if cached else
                               "Reuse the result you already have. ")
                            + "Change the query if you need something else, or ask the human why it repeats.", **more)
            elif version is not None and last.get("data_version") is not None:
                info["note"] = f"ran {window}; allowed because the tables changed since the last run"
            else:
                return stop("needs_approval", "repeat",
                            f"This exact query already ran {window} and Gulliver cannot tell whether the data "
                            "changed. Reuse the earlier result if you can.")

    # 3. spending fast
    if budget.velocity_usd > 0:
        since = now - timedelta(minutes=budget.velocity_window_min)
        recent = _velocity_spent(entries, ident, since)
        lifted = approved_at is not None and approved_at >= since
        if recent + (cost or 0.0) > budget.velocity_usd and not lifted:
            return stop("needs_approval", "velocity",
                        f"Spending fast: {_fmt_usd(recent)} in the last {budget.velocity_window_min:g} minutes"
                        + (f" + {_fmt_usd(cost)} for this query" if cost else "")
                        + f" is over {_fmt_usd(budget.velocity_usd)}. Check with the human that this is intended.")

    own = receipt is None  # the caller saves a receipt under receipt_id (run, rehearsal)
    engine.tags = tags(kind, receipt_id if own else None)
    ledger: dict[str, Any] = {"receipt": receipt_id, "tables": tables} if own else {"tables": tables}
    if version:
        ledger["data_version"] = version
    if approval_id:
        ledger["approval"] = approval_id
    extra = {"runaway": info} if ("repeats" in info or "note" in info) else {}
    return {"status": "ok", "ledger": ledger, "extra": extra}


# -- status, spend report and audit ----------------------------------------------------------
def caller_status(settings: Settings) -> dict[str, Any]:
    budget = settings.budget
    ident = current()
    entries = read_ledger(Store(settings.state_dir))
    today = now_utc().date()
    since = now_utc() - timedelta(minutes=budget.velocity_window_min)
    out: dict[str, Any] = {
        "agent": ident.agent, "session": ident.session, "task": ident.task, "identity_source": ident.source,
        "agent_spent_today_usd": _usd([e for e in entries if e.get("agent") == ident.agent
                                       and e["_at"].date() == today]),
        "agent_day_limit_usd": agent_limit(settings, ident.agent),
        "velocity": {"spent_usd": _velocity_spent(entries, ident, since),
                     "limit_usd": budget.velocity_usd or None, "window_min": budget.velocity_window_min},
        "repeat_guard": {"max_repeats": budget.max_repeats or None, "window_min": budget.repeat_window_min},
    }
    if ident.session:
        out["session_spent_usd"] = _usd([e for e in entries if e.get("session") == ident.session])
        out["session_limit_usd"] = budget.session_usd or None
    return out


def spend(settings: Settings, by: str = "agent", days: int = 7) -> dict[str, Any]:
    if by not in SPEND_BY:
        return _error(f"by must be one of {', '.join(SPEND_BY)}", "Use --by agent, day or table.")
    if days < 1:
        return _error("--days must be at least 1.", "Give a positive number of days, for example --days 7.")
    if days > 365:
        return _error("--days must be between 1 and 365.", "Give a number of days between 1 and 365.")
    days = int(days)
    first = now_utc().date() - timedelta(days=days - 1)
    groups: dict[str, dict[str, Any]] = {}
    total = {"usd": 0.0, "executions": 0, "bytes": 0}
    for e in read_ledger(Store(settings.state_dir)):
        if e["_at"].date() < first:
            continue
        cost = float(e.get("cost_usd") or 0.0)
        size = int(e.get("bytes") or 0)
        total["usd"] += cost
        total["bytes"] += size
        total["executions"] += 1
        if by == "day":
            keys = [e["_at"].date().isoformat()]
        elif by == "table":
            keys = list(e.get("tables") or []) or ["(none)" if "tables" in e else "(unknown)"]
        elif by in ("session", "task") and e.get("agent"):
            keys = [str(e.get(by) or "(none)")]   # written by this version: the call simply had no session/task
        else:
            keys = [str(e.get(by) or "(unknown)")]
        share = 1.0 / len(keys)
        for key in keys:
            g = groups.setdefault(key, {by: key, "usd": 0.0, "executions": 0, "runs": 0, "bytes": 0})
            g["usd"] += cost * share
            g["bytes"] += int(size * share)
            g["executions"] += 1
            g["runs"] += 1 if e.get("kind") == "run" else 0
    rows = sorted(groups.values(), key=(lambda g: g[by]) if by == "day" else (lambda g: (-g["usd"], g[by])))
    for g in rows:
        g["usd"] = round(g["usd"], 6)
    total["usd"] = round(total["usd"], 6)
    notes = ["Money is what the engine reported or Gulliver's cost model computed for each execution."]
    if by == "table":
        notes.append("A query that reads several tables is split evenly between them; executions count every "
                     "query that read the table.")
    if any(g[by] == "(none)" for g in rows):
        notes.append("(none): queries that read no table." if by == "table" else
                     f"(none): calls without a {by} (CLI calls get one only from GULLIVER_{by.upper()}).")
    if any(g[by] == "(unknown)" for g in rows):
        notes.append("(unknown): ledger lines written before Gulliver recorded this field.")
    return {"status": "ok", "by": by, "days": days, "since": first.isoformat(), "rows": rows, "total": total,
            "notes": notes, "next_step": "Share this with the human; `gulliver spend --by day` shows the trend."}


def render_spend(result: dict[str, Any], fmt: str = "markdown") -> str:
    by = result.get("by", "agent")
    rows = result.get("rows") or []
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([by, "usd", "executions", "runs", "bytes"])
        for g in rows:
            writer.writerow([g[by], f"{g['usd']:.6f}", g["executions"], g["runs"], g["bytes"]])
        return buf.getvalue()
    total = result.get("total") or {}
    lines = [f"# Gulliver spend by {by} since {result.get('since')}", "",
             f"| {by} | Money | Executions | Full runs | Bytes |", "|---|---|---|---|---|"]
    for g in rows:
        lines.append(f"| {g[by]} | {_fmt_usd(g['usd'])} | {g['executions']} | {g['runs']} | {_fmt_bytes(g['bytes'])} |")
    lines += [f"| **total** | {_fmt_usd(total.get('usd'))} | {total.get('executions', 0)} | | "
              f"{_fmt_bytes(total.get('bytes'))} |", ""]
    lines += [f"- {n}" for n in result.get("notes") or []]
    return "\n".join(lines) + "\n"


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = parse_iso(str(text))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _born(kind: str, path: Path) -> datetime | None:
    """When Gulliver says it wrote this receipt or approval: the latest timestamp the file claims (its id, its
    created_at / approved_at). None when it claims none."""
    times: list[datetime] = []
    parts = path.stem.split("-")
    if len(parts) >= 3:
        try:
            times.append(datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for field_name in ("created_at", "approved_at"):
            when = _when(data.get(field_name))
            if when is not None:
                times.append(when)
    if kind == "approval" and not (isinstance(data, dict) and data.get("approved_at")):
        return None
    return max(times) if times else None


def audit_verify(settings: Settings, head: str | None = None) -> dict[str, Any]:
    """Recompute the hash chains and compare every receipt and approval with the hash written for it."""
    store = Store(settings.state_dir)
    ledger = audit.verify_chain(store.ledger_path)
    trail_path = store.root / "audit.jsonl"
    trail = audit.verify_chain(trail_path)
    problems = ledger["problems"] + trail["problems"]
    audited: dict[tuple[str, str], dict[str, Any]] = {}
    if trail_path.is_file():
        for line in trail_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                kind = entry.get("kind")
                item_id = entry.get("id")
                sha = entry.get("sha256")
                if not kind or not item_id or not sha:
                    continue
                if kind == "prune":
                    target = entry.get("target")
                    if not target:
                        if ("receipt", item_id) in audited:
                            target = "receipt"
                        elif ("approval", item_id) in audited:
                            target = "approval"
                        else:
                            target = "receipt"
                    key = (target, item_id)
                    if key in audited:
                        audited[key]["last_action"] = "prune"
                        audited[key]["prune_sha"] = sha
                    else:
                        audited[key] = {"sha256": sha, "last_action": "prune", "prune_sha": sha}
                else:
                    if (kind, item_id) in audited:
                        audited[(kind, item_id)]["sha256"] = sha
                        audited[(kind, item_id)]["last_action"] = kind
                    else:
                        audited[(kind, item_id)] = {"sha256": sha, "last_action": kind}
            except (ValueError, KeyError, TypeError):
                continue
    folders = {"receipt": store.receipts_dir, "approval": store.approvals_dir}
    checked = 0
    pruned_count = 0
    for (kind, item_id), item in audited.items():
        path = folders.get(kind, store.root) / f"{item_id}.json"
        if not path.is_file():
            if item.get("last_action") == "prune":
                if item.get("prune_sha") == item.get("sha256"):
                    pruned_count += 1
                else:
                    problems.append(f"{kind} {item_id} was changed before it was pruned")
            else:
                problems.append(f"{kind} {item_id} was deleted")
        elif item.get("last_action") == "prune":
            if audit.content_hash(path.read_bytes()) != item["sha256"]:
                problems.append(f"{kind} {item_id} was changed after Gulliver wrote it")
            else:
                checked += 1
        elif audit.content_hash(path.read_bytes()) != item["sha256"]:
            problems.append(f"{kind} {item_id} was changed after Gulliver wrote it")
        else:
            checked += 1
    start = min([w for w in (_when(ledger["first_at"]), _when(trail["first_at"])) if w], default=None)
    unaudited: list[str] = []
    for kind, folder in folders.items():
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.json")):
            if (kind, p.stem) in audited:
                continue
            born = _born(kind, p)
            if start is not None and (born is None or born >= start):
                # written after the trail existed, yet the trail never saw it: added by hand (or the trail lost it)
                problems.append(f"{kind} {p.stem} has no audit entry although it was written after the audit "
                                "trail started (added by hand, or audit.jsonl was removed or cut)")
            else:
                unaudited.append(f"{kind} {p.stem}")
    for chain in (ledger, trail):
        late = [t for t in chain["legacy_at"] if start is not None and (_when(t) or start) >= start]
        if late:
            problems.append(f"{len(late)} {chain['file']} line(s) have no hash although they are newer than the "
                            "chain (the hashes were stripped?)")
            chain["legacy"] -= len(late)
    if head and head not in ledger["hashes"] and head not in trail["hashes"]:
        problems.append(f"the head {head[:16]}... you kept is no longer in either chain: entries up to it were "
                        "changed or removed")
    warnings = []
    if ledger["legacy"] or trail["legacy"]:
        warnings.append(f"{ledger['legacy'] + trail['legacy']} line(s) were written before the hash chain existed "
                        "and cannot be checked")
    if unaudited:
        warnings.append(f"{len(unaudited)} file(s) have no audit entry and are older than the audit trail")
    notes = [] if audit._key() else [
        "No GULLIVER_AUDIT_KEY: anyone who can write .gulliver can rebuild the chain. Keep the heads somewhere "
        "else, or set a key the agent cannot read."]
    if pruned_count:
        notes.append(f"{pruned_count} file(s) were pruned")
    status = "fail" if problems else ("warn" if warnings else "ok")
    if problems:
        next_step = "Something in .gulliver changed after Gulliver wrote it: tell the human which entries."
    elif warnings:
        next_step = ("Nothing Gulliver can check was changed, but some entries cannot be checked (see warnings): "
                     "tell the human. Keep the two head hashes (gulliver audit verify --head <hash>).")
    else:
        next_step = ("Intact. Keep the two head hashes to prove later that nothing before them changed "
                     "(gulliver audit verify --head <hash>).")
    return {
        "status": status,
        "problems": problems,
        "warnings": warnings,
        "notes": notes,
        "ledger": {"entries": ledger["entries"], "chained": ledger["chained"], "head": ledger["head"]},
        "receipts": {"entries": trail["entries"], "files_checked": checked, "head": trail["head"],
                     "unaudited": unaudited[:20]},
        "keyed": bool(audit._key()),
        "checked_at": iso(now_utc()),
        "next_step": next_step,
    }


def prune(settings: Settings, older_than: str | timedelta = "30d",
          keep_last: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    from .incremental import parse_period

    delta = parse_period(older_than) if isinstance(older_than, str) else older_than
    store = Store(settings.state_dir)
    return store.prune(older_than=delta, keep_last=keep_last, dry_run=dry_run)


def render_prune(result: dict[str, Any]) -> str:
    from .core import _fmt_bytes

    lines = []
    prefix = "[dry-run] " if result.get("dry_run") else ""
    lines.append(f"{prefix}Pruned {result['pruned_receipts']} receipt(s) and {result['pruned_approvals']} expired approval(s).")
    lines.append(f"Freed: {_fmt_bytes(result['bytes_freed'])}")
    if result.get("oldest_kept"):
        lines.append(f"Oldest kept receipt: {result['oldest_kept']}")
    else:
        lines.append("No receipts kept.")
    if result.get("next_step"):
        lines.append(f"\nnext: {result['next_step']}")
    return "\n".join(lines)
