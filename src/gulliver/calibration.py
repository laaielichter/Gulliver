"""Compare actual runs with the rehearsal ranges stored in local receipts."""

from __future__ import annotations

import math
from datetime import date, timedelta, timezone
from statistics import geometric_mean
from typing import Any

from .config import Settings
from .extrapolate import BANDS
from .store import Store, now_utc, parse_iso

METRICS = {"runtime_s": "runtime_s", "rows": "output_rows", "cost_usd": "cost_usd"}
LOCAL_ENGINES = ("duckdb", "postgres", "postgresql")


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            return None
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _range(entry: dict[str, Any]) -> tuple[float, float, str] | None:
    low, high = _number(entry.get("low")), _number(entry.get("high"))
    confidence = entry.get("confidence")
    if low is not None and high is not None and low <= high and isinstance(confidence, str) and confidence in BANDS:
        return low, high, confidence
    return None


def _rehearsal_range(receipt: dict[str, Any], metric: str) -> dict[str, Any]:
    prediction = receipt.get("prediction")
    entry = prediction.get(METRICS[metric]) if isinstance(prediction, dict) else None
    if not isinstance(entry, dict):
        return {}
    if metric == "cost_usd":
        return {**entry, "confidence": receipt.get("confidence")}
    return entry


def _receipt_day(receipt: dict[str, Any]) -> date | None:
    try:
        stamp = parse_iso(receipt["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(timezone.utc).date()
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def _summary(observations: list[tuple[bool, float | None]]) -> dict[str, Any]:
    ratios = sorted(ratio for _, ratio in observations if ratio is not None)
    middle = len(ratios) // 2
    median = geo = worst = None
    if ratios:
        median = ratios[middle]
        if len(ratios) % 2 == 0:
            lower = ratios[middle - 1]
            median = lower + (median - lower) / 2
        # A zero actual over a positive prediction is a real zero, while a zero
        # prediction never produced a ratio. The farthest multiplicative miss
        # is always at one of the ends of the sorted ratios.
        geo = 0.0 if ratios[0] == 0 else geometric_mean(ratios)
        worst = max((ratios[-1], ratios[0]), key=lambda r: abs(math.log(r)) if r else math.inf)
    return {"runs": len(observations), "in_range": sum(hit for hit, _ in observations),
            "median_ratio": median, "geometric_mean_ratio": geo, "worst_ratio": worst}


def calibration(settings: Settings, days: int = 30, connection: str | None = None) -> dict[str, Any]:
    """Summarize successful runs in the window, including the current UTC day."""
    today = now_utc().date()
    window = min(max(days, 1), today.toordinal())
    first = today - timedelta(days=window - 1)
    receipts = [receipt for receipt in Store(settings.state_dir).receipts() if isinstance(receipt, dict)]
    rehearsals = {receipt["receipt_id"]: receipt for receipt in receipts if receipt.get("action") == "rehearse"
                  and isinstance(receipt.get("receipt_id"), str)}
    metrics: dict[str, dict[str, list[tuple[bool, float | None]]]] = {metric: {} for metric in METRICS}
    runs = no_range = 0
    for receipt in receipts:
        actuals = receipt.get("actual")
        if (receipt.get("action") != "run" or receipt.get("status") not in ("ok", "warn")
                or not isinstance(actuals, dict) or not actuals
                or (connection is not None and receipt.get("connection") != connection)):
            continue
        day = _receipt_day(receipt)
        if day is None or not first <= day <= today:
            continue
        runs += 1
        comparisons = receipt.get("predicted_vs_actual")
        rehearsal_id = receipt.get("rehearsal_id")
        rehearsal = rehearsals.get(rehearsal_id, {}) if isinstance(rehearsal_id, str) else {}
        if rehearsal.get("connection") != receipt.get("connection"):
            rehearsal = {}
        compared = False
        for metric in METRICS:
            # Local adapters record zero for budget accounting, without measuring money.
            if metric == "cost_usd" and receipt.get("engine") in LOCAL_ENGINES:
                continue
            entry = comparisons.get(metric) if isinstance(comparisons, dict) else None
            entry = entry if isinstance(entry, dict) else {}
            bounds = _range(entry)
            prediction = _number(entry.get("predicted"))
            if bounds is None:
                old = _rehearsal_range(rehearsal, metric)
                bounds = _range(old)
                if prediction is None:
                    prediction = _number(old.get("value"))
            actual = _number(entry.get("actual", actuals.get(metric)))
            if bounds is None or actual is None or prediction is None:
                continue
            low, high, confidence = bounds
            ratio = actual / prediction if prediction else None
            if ratio is not None and not math.isfinite(ratio):
                ratio = None
            metrics[metric].setdefault(confidence, []).append((low <= actual <= high, ratio))
            compared = True
        no_range += not compared

    return {
        "status": "ok", "runs": runs, "no_range": no_range,
        "metrics": {metric: {level: _summary(groups[level]) for level in BANDS if level in groups}
                    for metric, groups in metrics.items()},
        "next_step": ("Share the observed range coverage with the human." if runs > no_range else
                      "Use Gulliver to rehearse and run a query first, then check calibration again."),
    }


def render_calibration(result: dict[str, Any]) -> str:
    lines = ["# Gulliver rehearsal calibration", "",
             f"Runs: {result['runs']} ({result['no_range']} with no comparable range).", "",
             "| Metric | Confidence | Runs | In range | Median ratio | Geometric mean ratio | Worst ratio |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    missing = []
    for metric, groups in result["metrics"].items():
        if not groups:
            missing.append(f"- {metric}: no data")
        for confidence, group in groups.items():
            ratios = [f"{group[key]:.6g}" if group[key] is not None else "—"
                      for key in ("median_ratio", "geometric_mean_ratio", "worst_ratio")]
            cells = [metric, confidence, str(group["runs"]), str(group["in_range"]), *ratios]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([*lines, "", *missing, "", result["next_step"]])
