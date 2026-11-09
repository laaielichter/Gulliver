import json
from datetime import timedelta

import pytest

from gulliver import features, runaway
from gulliver.cli import main
from gulliver.store import Store, iso, now_utc

SQL = "SELECT date_trunc('day', ts) AS day, count(*) FROM events GROUP BY 1"


@pytest.mark.parametrize("lookback", [-1, -10])
def test_negative_lookback_is_refused(state, capsys, lookback):
    result = features.incremental(None, SQL, time_column="ts", lookback=lookback, estimate_savings=False)
    assert result["status"] == "error"
    assert "--lookback" in result["error"]
    assert result["next_step"]
    assert main(["incremental", SQL, "--time-column", "ts", "--lookback", str(lookback),
                 "--no-savings", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_zero_lookback_is_valid(state, capsys):
    result = features.incremental(None, SQL, time_column="ts", lookback=0, estimate_savings=False)
    assert result["status"] == "ok"
    assert result["lookback_periods"] == 0
    assert main(["incremental", SQL, "--time-column", "ts", "--lookback", "0",
                 "--no-savings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["lookback_periods"] == 0


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_negative_keep_last_preserves_all_files(settings, capsys, dry_run, as_json):
    store = Store(settings.state_dir)
    store.save_receipt({"action": "run", "created_at": iso(now_utc() - timedelta(days=60)), "status": "ok"})

    def snapshot():
        return {p.relative_to(store.root): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}

    before = snapshot()
    result = runaway.prune(settings, keep_last=-1, dry_run=dry_run)
    assert result["status"] == "error"
    assert "--keep-last" in result["error"]
    assert result["next_step"]
    assert snapshot() == before
    args = ["prune", "--keep-last", "-1"]
    if dry_run:
        args.append("--dry-run")
    if as_json:
        args.append("--json")
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert snapshot() == before


def test_zero_keep_last_is_valid(settings, capsys):
    assert runaway.prune(settings, keep_last=0, dry_run=True)["status"] == "ok"
    assert main(["prune", "--keep-last", "0", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
