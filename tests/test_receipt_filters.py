import json

import pytest

from gulliver import core
from gulliver.cli import main
from gulliver.store import Store


@pytest.fixture
def receipts(settings):
    store = Store(settings.state_dir)
    records = [
        ("old-run", "run", "ok", "warehouse"),
        ("other-connection", "run", "ok", "local"),
        ("other-status", "run", "block", "warehouse"),
        ("new-run", "run", "ok", "warehouse"),
        ("newest-preflight", "preflight", "ok", "warehouse"),
    ]
    for i, (receipt_id, action, status, connection) in enumerate(records):
        store.save_receipt({
            "receipt_id": f"gv-20260101-00000{i}-{receipt_id}", "action": action,
            "status": status, "connection": connection,
            "created_at": f"2026-01-01T00:00:0{i}+00:00", "sql": "select 1", "preview": [{"private": "row"}],
        })
    return settings


@pytest.mark.parametrize("filters, expected", [
    ({"action": "run"}, ["new-run", "other-status", "other-connection", "old-run"]),
    ({"connection": "local"}, ["other-connection"]),
    ({"action": "run", "connection": "warehouse", "status": "ok"}, ["new-run", "old-run"]),
    ({"action": "missing"}, []),
    ({"connection": "missing"}, []),
])
def test_receipt_filters_combine_and_preserve_summary_only(receipts, capsys, filters, expected):
    result = core.list_receipts(receipts, **filters)
    assert result["status"] == "ok"
    assert [rec["receipt_id"].split("-", 3)[3] for rec in result["receipts"]] == expected
    assert all("preview" not in rec for rec in result["receipts"])
    args = ["receipt", "--list", "--json"]
    for key, value in filters.items():
        args.extend([f"--{key}", value])
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert [rec["receipt_id"].split("-", 3)[3] for rec in result["receipts"]] == expected


def test_receipt_limit_is_applied_after_all_filters(receipts, capsys):
    filters = {"action": "run", "connection": "warehouse", "status": "ok"}
    result = core.list_receipts(receipts, limit=1, **filters)
    assert [rec["receipt_id"].split("-", 3)[3] for rec in result["receipts"]] == ["new-run"]
    assert main(["receipt", "--list", "--action", "run", "--connection", "warehouse",
                 "--status", "ok", "--limit", "1"]) == 0
    output = capsys.readouterr().out
    assert "new-run" in output
    assert "old-run" not in output
    assert "preflight" not in output
