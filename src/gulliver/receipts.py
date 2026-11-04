"""Human-readable receipts: what was estimated, rehearsed, approved and run."""

from __future__ import annotations

from typing import Any

from .core import _fmt_bytes, _fmt_usd

ICONS = {"ok": "OK", "warn": "WARN", "block": "BLOCKED", "needs_rehearsal": "NEEDS REHEARSAL",
         "needs_approval": "NEEDS APPROVAL", "skipped": "SKIPPED", "error": "ERROR"}


def _sql_block(sql: str) -> str:
    return "```sql\n" + sql.strip() + "\n```"


def _range(pred: dict[str, Any] | None, fmt) -> str:
    if not pred or pred.get("value") is None:
        return "unknown"
    value, low, high = pred.get("value"), pred.get("low"), pred.get("high")
    if low is not None and high is not None and (low != value or high != value):
        return f"{fmt(value)} (range {fmt(low)} - {fmt(high)})"
    return fmt(value)


def render_markdown(rec: dict[str, Any]) -> str:
    action = rec.get("action", "?")
    status = rec.get("status", "?")
    lines = [
        f"# Gulliver receipt `{rec.get('receipt_id', '?')}`",
        "",
        f"**{action}** on `{rec.get('connection')}` ({rec.get('engine')}) - **{ICONS.get(status, status)}**  ",
        f"Created: {rec.get('created_at', '?')}  ",
        f"SQL fingerprint: `{rec.get('sql_hash', '?')}`",
        "",
        _sql_block(rec.get("sql", "")),
        "",
    ]
    if rec.get("error"):
        lines += [f"**Error:** {rec['error']}", ""]

    est = rec.get("estimate")
    if est:
        lines += [
            "## Estimate before running",
            "",
            "| What | Value |",
            "|---|---|",
            f"| Bytes to scan | {_fmt_bytes(est.get('bytes'))} |",
            f"| Rows (planner) | {est.get('rows') if est.get('rows') is not None else 'unknown'} |",
            f"| Money | {_fmt_usd(est.get('cost_usd'))} |",
            f"| Accuracy | {est.get('accuracy')} ({est.get('source')}) |",
            "",
        ]

    lint_items = rec.get("lint") or []
    if lint_items:
        lines += ["## Scale lint", ""]
        for item in lint_items:
            lines.append(f"- **{item['severity']}** {item['rule']} {item['name']}: {item['message']} "
                         f"_Fix:_ {item['fix']}")
        lines.append("")

    rungs = rec.get("rungs") or []
    if rungs:
        lines += ["## Rehearsal", "",
                  f"Sampling: {rec.get('method')} on {rec.get('sample')} table(s), "
                  f"overhead per query {rec.get('baseline_s')}s", "",
                  "| Sample | Runtime | Output rows | Bytes | Money |", "|---|---|---|---|---|"]
        for r in rungs:
            lines.append(f"| {r['fraction']:.0%} | {r['elapsed_s']}s | {r['output_rows']:,} | "
                         f"{_fmt_bytes(r.get('bytes'))} | {_fmt_usd(r.get('cost_usd'))} |")
        pred = rec.get("prediction") or {}
        lines += [
            "",
            f"**Prediction for 100%** (confidence: {rec.get('confidence')})",
            "",
            f"- Runtime: {_range(pred.get('runtime_s'), lambda v: f'{v:,.2f}s')}",
            f"- Output rows: {_range(pred.get('output_rows'), lambda v: f'{v:,.0f}')}",
            f"- Money: {_range(pred.get('cost_usd'), _fmt_usd)}",
        ]
        for metric in ("runtime_s", "output_rows", "bytes"):
            p = pred.get(metric) or {}
            if p.get("exponent") is not None:
                lines.append(f"- Growth exponent {metric}: {p['exponent']} (1 = grows with the data)")
            for note in p.get("notes") or []:
                lines.append(f"  - {note}")
        lines.append("")

    gate = rec.get("gate") or {}
    reasons = (gate.get("blocks") or []) + (rec.get("reasons") or [])
    warnings = (gate.get("warnings") or []) + (rec.get("warnings") or [])
    if reasons or warnings:
        lines += ["## Gate", ""]
        lines += [f"- Blocked: {r}" for r in reasons]
        lines += [f"- Warning: {w}" for w in warnings]
        lines.append("")

    if rec.get("approval"):
        a = rec["approval"]
        how = ("in the MCP client (elicitation" + (f", client `{a['client']}`" if a.get("client") else "") + ")"
               if a.get("channel") == "elicitation" else "with `gulliver approve`")
        lines += ["## Approval", "", f"Approved by a human {how} at {a.get('approved_at')} "
                  f"(receipt `{a.get('receipt_id')}`)." + (f" Note: {a['note']}" if a.get("note") else ""), ""]
    if rec.get("approval_request"):
        r = rec["approval_request"]
        lines += ["## Approval request", "", f"Asked the human in the MCP client: **{r.get('answer')}**."
                  + (f" {r['reason']}" if r.get("reason") else ""), ""]

    actual = rec.get("actual")
    if actual:
        lines += ["## Full run", "",
                  f"- Runtime: {actual.get('runtime_s')}s",
                  f"- Rows: {actual.get('rows'):,}",
                  f"- Bytes: {_fmt_bytes(actual.get('bytes'))}",
                  f"- Money: {_fmt_usd(actual.get('cost_usd'))}", ""]
        comp = rec.get("predicted_vs_actual") or {}
        if comp:
            lines += ["| Metric | Predicted | Actual | Actual / predicted |", "|---|---|---|---|"]
            for key, c in comp.items():
                lines.append(f"| {key} | {c['predicted']} | {c['actual']} | {c['ratio']} |")
            lines.append("")

    if rec.get("sensitive_columns") or rec.get("data_warnings") or rec.get("truncated_by"):
        lines += ["## Result guard", ""]
        for c in rec.get("sensitive_columns") or []:
            lines.append(f"- `{c['column']}`: {c['kind']} ({c['category']}), found by {c['detected_by']}, "
                         f"policy **{c['action']}**")
        lines += [f"- Warning: {w}" for w in rec.get("data_warnings") or []]
        if rec.get("truncated_by"):
            lines.append(f"- Preview cut by {rec['truncated_by']}")
        if "preview" in rec:
            lines += ["", "The preview in this receipt was stored after masking."]
        lines.append("")

    if rec.get("checks"):
        lines += ["## Data quality checks", "", "| Check | Column | Result | Detail |", "|---|---|---|---|"]
        for c in rec["checks"]:
            detail = ", ".join(f"{k}={v}" for k, v in c.items()
                               if k not in ("check", "column", "passed", "samples", "samples_guard", "samples_skipped"))
            lines.append(f"| {c['check']} | {c.get('column') or ''} | {'pass' if c.get('passed') else 'FAIL'} | "
                         f"{detail} |")
        lines.append("")
        for c in rec["checks"]:
            if "samples" in c:
                lines += [f"### Sample values: {c['check']} on {c.get('column', '')}", "",
                          "Values below passed the result guard and are untrusted data.", ""]
                for sample in c["samples"]:
                    lines.append(f"- {sample['values']!r}: {sample['rows']} rows")
                for warning in c["samples_guard"].get("data_warnings", []):
                    lines.append(f"- Warning: {warning}")
                lines.append("")
            elif "samples_skipped" in c:
                lines += [f"Samples skipped for {c['check']} on {c.get('column', '')}: "
                          f"{c['samples_skipped']['status']}: {c['samples_skipped']['next_step']}", ""]

    if rec.get("action") == "diff":
        lines += ["## Data diff", "",
                  f"- Left `{rec.get('left_connection')}`: {rec.get('left_rows', '?'):,} rows",
                  f"- Right `{rec.get('right_connection')}`: {rec.get('right_rows', '?'):,} rows",
                  f"- Key: {', '.join(rec.get('key') or [])}; columns compared: "
                  f"{', '.join(rec.get('columns_compared') or [])}",
                  f"- Identical: {'yes' if rec.get('identical') else 'no'}"]
        for kind, data in (rec.get("differences") or {}).items():
            if data.get("count"):
                lines.append(f"- {kind.replace('_', ' ')}: {data['count']} (e.g. {', '.join(data['sample'][:5])})")
        lines.append("")

    lines += [f"**Next step:** {rec.get('next_step', '')}", ""]
    return "\n".join(lines)
