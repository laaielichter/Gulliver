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

    def _audit(self, kind: str, item_id: str, data: bytes) -> None:
        from .audit import append, content_hash

        append(self.root / "audit.jsonl", {"at": iso(now_utc()), "kind": kind, "id": item_id,
                                           "sha256": content_hash(data)})

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
            if (rec.get("action") == "rehearse" and rec.get("sql_hash") == sql_hash
                    and rec.get("connection") == connection and rec.get("status") in ("ok", "warn", "block")
                    and parse_iso(rec["created_at"]) >= cutoff):
                return rec
        return None

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
