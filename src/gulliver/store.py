"""Local state in the .gulliver folder: receipts, the spend ledger and human approvals.

Everything is plain JSON so people (and other tools) can read it.
  receipts/<id>.json    one per preflight, rehearsal and run
  ledger.jsonl          money and bytes actually spent, one line per execution
  approvals/<id>.json   written by `gulliver approve <id>`, or by the MCP server when the human approves in
                        the client (only with [approval] channel = "elicitation")
  audit.jsonl           hash chain over receipt and approval files (see audit.py, `gulliver audit verify`)
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APPROVAL_TTL = timedelta(hours=24)
REHEARSAL_TTL = timedelta(hours=24)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


class Store:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir)

    # -- paths ---------------------------------------------------------------------
    @property
    def receipts_dir(self) -> Path:
        return self.root / "receipts"

    @property
    def approvals_dir(self) -> Path:
        return self.root / "approvals"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    def _ensure(self) -> None:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# Gulliver local state (receipts may contain SQL and result previews)\n*\n")

    # -- receipts ------------------------------------------------------------------
    def new_id(self, action: str) -> str:
        # 64 random bits: parallel writers (several MCP sessions, diff/check internals) must not share an id.
        # 32 bits collided about 1 in 300 times over 5000 ids in one second (birthday bound).
        return f"gv-{now_utc().strftime('%Y%m%d-%H%M%S')}-{action[:3]}-{secrets.token_hex(8)}"

    def save_receipt(self, receipt: dict[str, Any]) -> str:
        self._ensure()
        rid = receipt.setdefault("receipt_id", self.new_id(receipt.get("action", "rec")))
        receipt.setdefault("created_at", iso(now_utc()))
        path = self.receipts_dir / f"{rid}.json"
        tmp = path.with_name(f"{rid}.{secrets.token_hex(4)}.tmp")   # unique, so writers never share a tmp file
        data = json.dumps(receipt, indent=2, default=str).encode("utf-8")
        tmp.write_bytes(data)   # bytes, so the file is exactly what is hashed (text mode writes \r\n on Windows)
        os.replace(tmp, path)
        self._audit("receipt", rid, data)
        return rid

    def _audit(self, kind: str, item_id: str, data: bytes, **extra: Any) -> None:
        from .audit import append, content_hash

        entry = {"at": iso(now_utc()), "kind": kind, "id": item_id,
                 "sha256": content_hash(data), **extra}
        append(self.root / "audit.jsonl", entry)

    def load_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        safe = Path(receipt_id).name
        path = self.receipts_dir / f"{safe}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def receipts(self) -> list[dict[str, Any]]:
        if not self.receipts_dir.is_dir():
            return []
        out = []
        # newest first; ids only have one-second resolution, so ties are broken by the file's write time
        paths = sorted(self.receipts_dir.glob("*.json"), key=lambda p: (p.name[:18], _mtime(p), p.name), reverse=True)
        for path in paths:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def latest_rehearsal(self, sql_hash: str, connection: str) -> dict[str, Any] | None:
        cutoff = now_utc() - REHEARSAL_TTL
        for rec in self.receipts():
            try:
                cat = parse_iso(rec["created_at"]) if "created_at" in rec else None
            except (ValueError, KeyError):
                cat = None
            if cat and cat < cutoff:
                break
            if (rec.get("action") == "rehearse" and rec.get("sql_hash") == sql_hash
                    and rec.get("connection") == connection and rec.get("status") in ("ok", "warn", "block")
                    and cat and cat >= cutoff):
                return rec
        return None

    def prune(self, older_than: timedelta, keep_last: int | None = None,
              dry_run: bool = False, reason: str | None = None) -> dict[str, Any]:
        """Remove receipts older than `older_than` and expired approvals, appending an audit record for each.

        Receipts needed by an active approval (expires_at > now) or an active rehearsal
        (within REHEARSAL_TTL) are never pruned. With `keep_last`, keeps at least N newest receipts.
        """
        if keep_last is not None and keep_last < 0:
            return {
                "status": "error",
                "error": "--keep-last must be at least 0.",
                "next_step": "Pass 0 or a positive number of receipts to keep.",
            }
        now = now_utc()
        cutoff = now - older_than

        live_receipt_ids: set[str] = set()
        expired_approval_paths: list[Path] = []
        if self.approvals_dir.is_dir():
            for p in sorted(self.approvals_dir.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    rid = str(data.get("receipt_id") or p.stem)
                    exp = parse_iso(data["expires_at"]) if "expires_at" in data else None
                    if exp and exp > now:
                        live_receipt_ids.add(rid)
                    else:
                        expired_approval_paths.append(p)
                except (OSError, ValueError, KeyError):
                    continue

        kept_paths: list[Path] = []
        to_prune_receipts: list[Path] = []
        if self.receipts_dir.is_dir():
            all_receipts = sorted(self.receipts_dir.glob("*.json"),
                                  key=lambda p: (p.name[:18], _mtime(p), p.name), reverse=True)
            for i, p in enumerate(all_receipts):
                if keep_last is not None and i < keep_last:
                    kept_paths.append(p)
                    continue
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                    cat = parse_iso(rec["created_at"]) if "created_at" in rec else None
                    rid = str(rec.get("receipt_id") or p.stem)
                    action = rec.get("action")
                except (OSError, ValueError, KeyError):
                    cat = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                    rid = p.stem
                    action = None

                if cat and cat >= cutoff:
                    kept_paths.append(p)
                    continue

                if rid in live_receipt_ids:
                    kept_paths.append(p)
                    continue

                if action == "rehearse" and cat and cat >= (now - REHEARSAL_TTL):
                    kept_paths.append(p)
                    continue

                to_prune_receipts.append(p)

        to_prune_approvals = expired_approval_paths

        oldest_kept: str | None = None
        if kept_paths:
            kept_times = []
            for p in kept_paths:
                try:
                    r = json.loads(p.read_text(encoding="utf-8"))
                    if "created_at" in r:
                        kept_times.append(str(r["created_at"]))
                except Exception:
                    pass
            if kept_times:
                oldest_kept = min(kept_times)

        bytes_freed = 0
        for p in to_prune_receipts + to_prune_approvals:
            try:
                bytes_freed += p.stat().st_size
            except OSError:
                pass

        if not dry_run:
            self._ensure()
            for p in to_prune_receipts:
                try:
                    raw = p.read_bytes()
                    rid = p.stem
                    prune_reason = reason or f"older than {older_than}"
                    self._audit("prune", rid, raw, target="receipt",
                                reason=prune_reason)
                    p.unlink(missing_ok=True)
                except OSError:
                    continue
            for p in to_prune_approvals:
                try:
                    raw = p.read_bytes()
                    aid = p.stem
                    self._audit("prune", aid, raw, target="approval",
                                reason="expired")
                    p.unlink(missing_ok=True)
                except OSError:
                    continue

        if dry_run:
            next_step = "Dry run: re-run without --dry-run to remove these files and append audit prune entries."
        elif to_prune_receipts or to_prune_approvals:
            next_step = "Pruned. Check the audit trail with gulliver audit verify."
        else:
            next_step = "Intact: no receipts or approvals matched the prune criteria."

        return {
            "status": "ok",
            "pruned_receipts": len(to_prune_receipts),
            "pruned_approvals": len(to_prune_approvals),
            "pruned_files": len(to_prune_receipts) + len(to_prune_approvals),
            "bytes_freed": bytes_freed,
            "oldest_kept": oldest_kept,
            "dry_run": dry_run,
            "next_step": next_step,
        }

    # -- approvals -----------------------------------------------------------------
    def approve(self, receipt_id: str, note: str = "", channel: str = "cli",
                client: str | None = None) -> dict[str, Any]:
        receipt = self.load_receipt(receipt_id)
        if receipt is None:
            raise FileNotFoundError(f"No receipt {receipt_id}")
        self._ensure()
        approval = {
            "receipt_id": receipt_id,
            "sql_hash": receipt.get("sql_hash"),
            "connection": receipt.get("connection"),
            "approved_at": iso(now_utc()),
            "expires_at": iso(now_utc() + APPROVAL_TTL),
            "max_cost_usd": receipt.get("gate", {}).get("cost_for_gate_usd"),
            "note": note,
            "channel": channel,   # cli: `gulliver approve`; elicitation: the human answered in the MCP client
        }
        if client:
            approval["client"] = client
        data = json.dumps(approval, indent=2).encode("utf-8")
        (self.approvals_dir / f"{Path(receipt_id).name}.json").write_bytes(data)
        self._audit("approval", Path(receipt_id).name, data)
        return approval

    def _audited(self) -> dict[tuple[str, str], str]:
        """(kind, id) -> sha256 of the file as Gulliver wrote it, from audit.jsonl. With GULLIVER_AUDIT_KEY set,
        only lines whose HMAC checks out count."""
        from .audit import HMAC, _key, entry_hash

        path = self.root / "audit.jsonl"
        out: dict[tuple[str, str], str] = {}
        if not path.is_file():
            return out
        keyed = _key() is not None
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                if keyed and (entry.get("alg") != HMAC or entry_hash(entry) != entry.get("hash")):
                    continue
                out[(entry["kind"], entry["id"])] = entry["sha256"]
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return out

    def find_approval(self, sql_hash: str, connection: str, cost: float | None = None) -> dict[str, Any] | None:
        """A live human approval for this query. Only approvals Gulliver wrote count (the file must match its
        audit entry, so one written by hand is ignored). With `cost`, only an approval given for at least that
        cost counts: approving a rehearsal or a velocity request does not approve the full run's cost."""
        if not self.approvals_dir.is_dir():
            return None
        from .audit import content_hash

        audited: dict[tuple[str, str], str] | None = None
        now = now_utc()
        for path in sorted(self.approvals_dir.glob("*.json"), reverse=True):
            try:
                raw = path.read_bytes()
                data = json.loads(raw)
                live = (data.get("sql_hash") == sql_hash and data.get("connection") == connection
                        and parse_iso(data["expires_at"]) > now)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if not live:
                continue
            if cost is not None:
                approved = data.get("max_cost_usd")
                if not isinstance(approved, (int, float)) or approved < cost - 1e-9:
                    continue
            if audited is None:
                audited = self._audited()
            if audited.get(("approval", path.stem)) != content_hash(raw):
                continue
            return data
        return None

    # -- ledger --------------------------------------------------------------------
    def record_spend(self, entry: dict[str, Any]) -> None:
        """Append one hash-chained line, stamped with who asked (agent, session, task)."""
        from .audit import append
        from .identity import current

        self._ensure()
        append(self.ledger_path, {"at": iso(now_utc()), **current().fields(), **entry})

    def spent_today(self) -> dict[str, float]:
        today = now_utc().date()
        usd = 0.0
        runs = 0
        bytes_total = 0
        if self.ledger_path.is_file():
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    if parse_iso(entry["at"]).date() != today:
                        continue
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
                usd += float(entry.get("cost_usd") or 0.0)
                bytes_total += int(entry.get("bytes") or 0)
                runs += 1
        return {"usd": round(usd, 6), "executions": runs, "bytes": bytes_total}
