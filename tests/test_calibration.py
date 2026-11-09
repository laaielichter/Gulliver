"""The calibration report uses stored observations without running any queries."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from gulliver.cli import main
from gulliver.config import load_settings
from gulliver.store import Store, iso, now_utc


def _run(store, comparison=None, *, rehearsal_id=None, connection="primary", days_ago=0, status="ok", engine="bigquery"):
    receipt = {"action": "run", "status": status, "engine": engine, "connection": connection, "actual": {"rows": 1},
               "created_at": iso(now_utc() - timedelta(days=days_ago))}
    if comparison is not None:
        receipt["predicted_vs_actual"] = comparison
    if rehearsal_id is not None:
        receipt["rehearsal_id"] = rehearsal_id
    return store.save_receipt(receipt)


def _measurement(predicted, actual, low=10, high=30, confidence="high"):
    return {"predicted": predicted, "actual": actual, "low": low, "high": high, "confidence": confidence}


def test_cli_calibration_reports_coverage_and_multiplicative_ratios(state, capsys):
    store = Store(state)
    _run(store, {"rows": _measurement(20, 10)})
    _run(store, {"rows": _measurement(20, 80)})
    _run(store, {"rows": _measurement(20, 20, confidence="low"),
                 "cost_usd": _measurement(0, 0, low=0, high=0)}, engine="postgresql")
    _run(store, {"rows": _measurement(20, 20)}, connection="secondary")
    _run(store, {"rows": _measurement(20, 20)}, days_ago=2)
    _run(store, {"rows": _measurement(20, 20)}, status="block")
    _run(store)

    assert main(["calibration", "--days", "2", "-c", "primary", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["status"], data["runs"], data["no_range"]) == ("ok", 4, 1)
    high = data["metrics"]["rows"]["high"]
    assert (high["runs"], high["in_range"], high["median_ratio"], high["worst_ratio"]) == (2, 1, 2.25, 4)
    assert high["geometric_mean_ratio"] == pytest.approx(2 ** 0.5)
    assert data["metrics"]["rows"]["low"]["in_range"] == 1
    assert data["metrics"]["runtime_s"] == data["metrics"]["cost_usd"] == {}

    assert main(["calibration", "--days", "2", "-c", "primary"]) == 0
    text = capsys.readouterr().out
    assert "| rows | high | 2 | 1 | 2.25 |" in text
    assert "cost_usd: no data" in text


def test_calibration_refuses_days_below_one(state):
    from gulliver.calibration import calibration

    for days in (0, -1):
        res = calibration(load_settings(), days=days)
        assert res["status"] == "error"
        assert res["error"] == "--days must be at least 1."
        assert res["next_step"]


def test_cli_calibration_refuses_days_below_one(state, capsys):
    assert main(["calibration", "--days", "0", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error" and "--days must be at least 1." in data["error"]
    assert main(["calibration", "--days", "-1", "--json"]) == 1
    assert "--days must be at least 1." in capsys.readouterr().out


def test_legacy_receipts_use_metric_confidence_and_overall_for_cost(state):
    from gulliver.calibration import calibration

    store = Store(state)
    rehearsal = store.save_receipt({
        "action": "rehearse", "connection": "primary", "confidence": "medium",
        "prediction": {
            "output_rows": {"value": 20, "low": 10, "high": 30, "confidence": "high"},
            "runtime_s": {"value": 10, "low": 2, "high": 40, "confidence": "low"},
            "cost_usd": {"value": 2, "low": 1, "high": 4},
        },
    })
    old = {"rows": {"predicted": 20, "actual": 30, "ratio": 1.5},
           "runtime_s": {"predicted": 10, "actual": 1, "ratio": 0.1},
           "cost_usd": {"predicted": 2, "actual": 4, "ratio": 2}}
    _run(store, old, rehearsal_id=rehearsal)
    _run(store, old, rehearsal_id="missing")
    _run(store, old, rehearsal_id=rehearsal, connection="secondary")

    data = calibration(load_settings())
    assert (data["runs"], data["no_range"]) == (3, 2)
    assert data["metrics"]["rows"]["high"]["in_range"] == 1
    assert data["metrics"]["runtime_s"]["low"]["in_range"] == 0
    assert data["metrics"]["cost_usd"]["medium"]["in_range"] == 1


def test_zero_and_small_ratios_are_not_missing_or_rounded_to_zero(state):
    from gulliver.calibration import calibration

    store = Store(state)
    _run(store, {"rows": _measurement(10, 0, low=0),
                 "runtime_s": _measurement(10, 0.001, low=0),
                 "cost_usd": _measurement(0, 0, low=0, high=0)})
    _run(store, {"rows": _measurement(10, 10, low=0), "runtime_s": _measurement(10, 0.02, low=0)})
    data = calibration(load_settings())["metrics"]
    assert data["rows"]["high"]["geometric_mean_ratio"] == data["rows"]["high"]["worst_ratio"] == 0
    assert data["runtime_s"]["high"]["worst_ratio"] == pytest.approx(0.0001)
    assert data["runtime_s"]["high"]["median_ratio"] == pytest.approx(0.00105)
    cost = data["cost_usd"]["high"]
    assert (cost["runs"], cost["in_range"]) == (1, 1)
    assert cost["median_ratio"] is cost["geometric_mean_ratio"] is cost["worst_ratio"] is None


def test_empty_calibration_has_actionable_next_step(state, capsys):
    assert main(["--json", "calibration"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["status"], data["runs"], data["no_range"]) == ("ok", 0, 0)
    assert "rehearse and run" in data["next_step"]
    assert not state.exists()
