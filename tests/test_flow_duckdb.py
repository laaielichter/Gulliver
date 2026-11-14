"""End-to-end workflow on a real DuckDB file, plus a fake 'billed' engine for the money gates."""

from __future__ import annotations

import duckdb
import pytest

from gulliver import core
from gulliver.config import Budget, Connection, load_settings
from gulliver.engines.base import EXACT, Estimate
from gulliver.engines.duckdb_engine import DuckDBEngine

GOOD = ("select u.country, count(*) as n from events e join users u on e.user_id = u.user_id "
        "where e.event_type = 'buy' group by 1")


def test_status_and_inspect(settings):
    st = core.status(settings)
    assert st["status"] == "ok" and st["spent_today"]["usd"] == 0
    listing = core.inspect(settings)
    names = [t["name"] for t in listing["tables"]]
    assert names[0] == "events" and "users" in names
    one = core.inspect(settings, table="events")
    assert one["table"]["rows"] == 400_000
    assert {c["name"] for c in one["table"]["columns"]} >= {"event_id", "user_id", "ts"}
    assert core.inspect(settings, table="nope")["status"] == "error"


def test_preflight_ok_and_receipt(settings):
    pre = core.preflight(settings, GOOD)
    assert pre["status"] == "ok"
    assert pre["estimate"]["accuracy"] == "heuristic"
    assert pre["estimate"]["bytes"] > 0
    assert pre["receipt_id"].startswith("gv-")
    md = core.get_receipt(settings, pre["receipt_id"])["markdown"]
    assert "Estimate before running" in md


def test_preflight_unknown_table_is_error(settings):
    pre = core.preflight(settings, "select id from no_such_table")
    assert pre["status"] == "error"
    assert "no_such_table" in pre["error"].lower()


def test_lint_error_blocks_run(settings):
    res = core.run(settings, "select e.event_id from events e, users u limit 5")
    assert res["status"] == "block"
    assert "GV003" in res["next_step"]


def test_blocked_statement(settings):
    res = core.run(settings, "delete from events")
    assert res["status"] == "block"


def test_rehearse_and_run_compare(settings):
    reh = core.rehearse(settings, GOOD)
    assert reh["status"] in ("ok", "warn")
    assert [r["fraction"] for r in reh["rungs"]] == [0.01, 0.03, 0.1]
    assert reh["rungs"][0]["sampled_tables"] == ["events"]
    assert reh["prediction"]["output_rows"]["value"] > 0
    run = core.run(settings, GOOD)
    assert run["status"] == "ok"
    assert run["actual"]["rows"] == 8
    assert run["rehearsal_id"] == reh["receipt_id"]
    assert "rows" in run["predicted_vs_actual"]
    from gulliver.calibration import calibration
    from gulliver.store import Store

    saved = Store(settings.state_dir).load_receipt(run["receipt_id"])
    assert saved["predicted_vs_actual"] == run["predicted_vs_actual"]
    report = calibration(settings)
    assert (report["runs"], report["no_range"]) == (1, 0)
    assert report["metrics"]["cost_usd"] == {}
    for key, prediction_key in (("rows", "output_rows"), ("runtime_s", "runtime_s")):
        expected = reh["prediction"][prediction_key]
        confidence = expected["confidence"]
        comparison = saved["predicted_vs_actual"][key]
        assert [comparison[k] for k in ("low", "high", "confidence")] == [
            expected["low"], expected["high"], confidence,
        ]
        assert report["metrics"][key][confidence]["runs"] == 1


def test_exploding_join_is_flagged(settings):
    sql = "select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id"
    reh = core.rehearse(settings, sql)
    # only one side is sampled, so the output grows linearly; the row multiplication is flagged directly
    assert 0.9 < reh["prediction"]["output_rows"]["exponent"] < 1.1
    assert any("multiplies rows" in w for w in reh["warnings"])
    both = core.rehearse(settings, sql, sample="all")
    assert both["prediction"]["output_rows"]["exponent"] > 1.3
    assert any("faster than the data" in w for w in both["warnings"])


def test_small_data_skips_rehearsal(settings):
    reh = core.rehearse(settings, "select country, count(*) from (select * from users limit 100) x group by 1")
    # users has 20,000 rows, so it is rehearsed; a really small table is skipped
    assert reh["status"] in ("ok", "warn", "skipped")


def test_tiny_table_is_skipped(tmp_path, state):
    db = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table t as select i as id from range(500) t(i)")
    con.close()
    reh = core.rehearse(load_settings(dsn=str(db)), "select count(*) from t")
    assert reh["status"] == "skipped"


def test_needs_rehearsal_when_scan_is_big(demo_db, state):
    settings = load_settings(dsn=demo_db, budget_overrides={"rehearse_over_bytes": 1000})
    res = core.run(settings, GOOD)
    assert res["status"] == "needs_rehearsal"
    core.rehearse(settings, GOOD)
    assert core.run(settings, GOOD)["status"] == "ok"


def test_preview_is_capped(settings):
    res = core.run(settings, "select event_id from events limit 1000", max_rows=5000)
    assert res["status"] == "ok"
    assert len(res["preview"]) == settings.budget.max_output_rows
    assert res["actual"]["rows"] == 1000
    assert res["truncated"] is True


def test_writes_need_double_opt_in(tmp_path, state):
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table src as select i as id from range(100) t(i)")
    con.close()
    sql = "create table dst as select id from src where id < 10"
    s = load_settings(dsn=str(db))
    assert core.run(s, sql, allow_write=True)["status"] == "block"  # connection does not allow writes
    (tmp_path / "gulliver.toml").write_text(f"[connections.w]\nengine = 'duckdb'\npath = '{db}'\nallow_write = true\n")
    s2 = load_settings(config_path=tmp_path / "gulliver.toml")
    assert core.run(s2, sql)["status"] == "block"                  # call did not ask for writes
    assert core.run(s2, sql, allow_write=True)["status"] == "ok"
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from dst").fetchone()[0] == 10


class BilledDuckDB(DuckDBEngine):
    """DuckDB pretending to be a warehouse that charges $1 per MB scanned."""

    billed = True

    def estimate(self, sql):
        est = super().estimate(sql)
        return Estimate(bytes=est.bytes, rows=est.rows, cost_usd=self.cost_for(est.bytes, None),
                        accuracy=EXACT, source="fake")

    def cost_for(self, bytes_processed, runtime_s):
        return None if bytes_processed is None else bytes_processed / 1e6


@pytest.fixture
def billed(monkeypatch, demo_db, state):
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: BilledDuckDB(conn, read_only))
    return load_settings(dsn=demo_db, budget_overrides={
        "max_query_usd": 100.0, "approval_over_usd": 1.0, "rehearse_over_usd": 1000.0, "day_usd": 1000.0})


def test_money_over_limit_is_blocked(monkeypatch, demo_db, state):
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: BilledDuckDB(conn, read_only))
    s = load_settings(dsn=demo_db, budget_overrides={"max_query_usd": 0.01})
    res = core.preflight(s, GOOD)
    assert res["status"] == "block"
    assert "per-query limit" in res["next_step"]


def test_approval_flow(billed):
    first = core.run(billed, GOOD)
    assert first["status"] == "needs_approval"
    assert first["receipt_id"] in first["next_step"]
    core.approve(billed, first["receipt_id"], note="ok for the report")
    second = core.run(billed, GOOD)
    assert second["status"] == "ok"
    assert second["approval"]["note"] == "ok for the report"
    assert core.status(billed)["spent_today"]["executions"] == 1


def test_day_budget(billed, monkeypatch):
    s = load_settings(dsn=billed.connection().params["path"], budget_overrides={
        "max_query_usd": 100.0, "approval_over_usd": 100.0, "rehearse_over_usd": 1000.0, "day_usd": 0.5})
    assert core.preflight(s, GOOD)["status"] == "block"


def test_budget_defaults_are_sane():
    b = Budget()
    assert b.approval_over_usd < b.max_query_usd < b.day_usd
    assert Connection("x", "duckdb").public_dict() == {"name": "x", "engine": "duckdb"}


def test_run_refuses_negative_max_rows_and_allows_zero(settings):
    err = core.run(settings, GOOD, max_rows=-5)
    assert err["status"] == "error"
    assert "-5" in err["error"] and "--max-rows" in err["error"] and err["next_step"]
    assert core.list_receipts(settings)["receipts"] == []

    zero = core.run(settings, GOOD, max_rows=0)
    assert zero["status"] == "ok"
    assert zero["preview"] == []
    assert zero["actual"]["rows"] == 8

    over = core.run(settings, GOOD, max_rows=settings.budget.max_output_rows + 50)
    assert over["status"] == "ok"
    assert len(over["preview"]) <= settings.budget.max_output_rows
