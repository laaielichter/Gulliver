"""Runaway guard: loops, per-agent and per-session budgets, spend velocity, query tags, spend report, audit chain."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import duckdb
import pytest

from gulliver import audit, core, features, runaway
from gulliver.cli import main
from gulliver.config import ConfigError, Connection, load_settings
from gulliver.engines.base import EXACT, Estimate
from gulliver.engines.bigquery import BigQueryEngine, job_labels
from gulliver.engines.duckdb_engine import DuckDBEngine
from gulliver.engines.postgres import application_name
from gulliver.engines.snowflake import SnowflakeEngine
from gulliver.identity import Identity, from_env, use

Q = "select country, count(*) as n from users group by 1"


@pytest.fixture
def loopy(monkeypatch):
    """Treat every query as expensive enough to count as a loop (the demo data is tiny)."""
    monkeypatch.setattr(runaway, "REPEAT_FREE_BYTES", -1)


@pytest.fixture
def small_db(tmp_path):
    path = tmp_path / "small.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create table users as select i as user_id, ['NL','BE','DE'][1 + i % 3] as country "
                "from range(3000) t(i)")
    con.close()
    return str(path)


class FixedCostDuckDB(DuckDBEngine):
    """DuckDB pretending to be a warehouse where every query costs $0.30."""

    billed = True
    PRICE = 0.30

    def estimate(self, sql):
        est = super().estimate(sql)
        return Estimate(bytes=est.bytes, rows=est.rows, cost_usd=self.PRICE, accuracy=EXACT, source="fake")

    def cost_for(self, bytes_processed, runtime_s):
        return self.PRICE

    def execute(self, sql, preview_rows, timeout_s=None, max_bytes_billed=None):
        res = super().execute(sql, preview_rows, timeout_s, max_bytes_billed)
        res.cost_usd = self.PRICE if sql.strip().upper() != "SELECT 1" else 0.0
        return res


@pytest.fixture
def billed(monkeypatch, demo_db, state):
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: FixedCostDuckDB(conn, read_only))

    def make(**budget):
        base = {"max_query_usd": 1.0, "approval_over_usd": 1.0, "rehearse_over_usd": 10.0, "day_usd": 100.0,
                "velocity_usd": 0, "max_repeats": 0}
        return load_settings(dsn=demo_db, budget_overrides={**base, **budget})

    return make


def ledger(settings):
    return [json.loads(line) for line in (settings.state_dir / "ledger.jsonl").read_text().splitlines()]


# -- repeats and the loop guard ---------------------------------------------------------------
def test_identical_runs_are_blocked_after_max_repeats(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 3})
    for _ in range(3):
        assert core.run(s, Q)["status"] == "ok"
    fourth = core.run(s, Q)
    assert fourth["status"] == "block" and fourth["reason"] == "loop"
    assert fourth["cached"]["cached"] is True
    assert {r["country"]: r["n"] for r in fourth["cached"]["preview"]} == {"NL": 1000, "BE": 1000, "DE": 1000}
    assert "cached.preview" in fourth["next_step"]
    assert len(ledger(s)) == 3                                   # the blocked run cost nothing
    other = core.run(s, Q.replace("count(*)", "count(user_id)"))  # a different query is fine
    assert other["status"] == "ok"


def test_writing_to_the_duckdb_file_lifts_the_loop_block(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 2})
    core.run(s, Q)
    core.run(s, Q)
    assert core.run(s, Q)["status"] == "block"
    con = duckdb.connect(small_db)
    con.execute("insert into users values (99999, 'FR')")
    con.close()
    after = core.run(s, Q)
    assert after["status"] == "ok"
    assert "tables changed" in after["runaway"]["note"]
    assert {r["country"] for r in after["preview"]} == {"NL", "BE", "DE", "FR"}
    versions = [e.get("data_version") for e in ledger(s)]
    assert versions[0] == versions[1] != versions[2]


def test_cheap_repeats_are_not_blocked(small_db, state):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    for _ in range(3):
        res = core.run(s, Q)
        assert res["status"] == "ok"
    assert "cheap" in res["runaway"]["note"]


def test_no_change_signal_asks_a_human(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    sql = "select count(*) as n from users where current_date > date '2000-01-01'"
    assert core.run(s, sql)["status"] == "ok"
    second = core.run(s, sql)
    assert second["status"] == "needs_approval" and second["reason"] == "repeat"
    assert f"gulliver approve {second['receipt_id']}" in second["next_step"]
    core.approve(s, second["receipt_id"])
    assert core.run(s, sql)["status"] == "ok"                  # the approval buys another max_repeats runs
    assert core.run(s, sql)["status"] == "needs_approval"


def test_in_memory_duckdb_has_no_change_signal(state):
    eng = DuckDBEngine(Connection("m", "duckdb", {"path": ":memory:"}))
    assert eng.data_version(["t"]) is None
    assert runaway.deterministic(None) is False


def test_views_and_nondeterministic_queries_have_no_change_signal(small_db):
    con = duckdb.connect(small_db)
    con.execute("create view v as select * from users")
    con.close()
    eng = DuckDBEngine(Connection("f", "duckdb", {"path": small_db}))
    try:
        assert eng.data_version(["users"]) is not None
        assert eng.data_version(["v"]) is None
        assert runaway.data_version(eng, "select * from users") is not None
        assert runaway.data_version(eng, "select random() from users") is None
        assert runaway.data_version(eng, "select now() from users") is None
    finally:
        eng.close()


def test_parquet_file_change_is_seen(tmp_path):
    pq = tmp_path / "x.parquet"
    duckdb.sql(f"copy (select 1 as a) to '{pq}' (format parquet)")
    eng = DuckDBEngine(Connection("f", "duckdb", {"path": str(tmp_path / "f.duckdb")}), read_only=False)
    try:
        before = eng.data_version([f"read_parquet('{pq}')"])
        duckdb.sql(f"copy (select 1 as a union all select 2) to '{pq}' (format parquet)")
        after = eng.data_version([f"read_parquet('{pq}')"])
        assert before and after and before != after
        assert eng.data_version(["read_parquet('s3://bucket/x.parquet')"]) is None
    finally:
        eng.close()


def test_internal_queries_are_guarded_too(small_db, state, loopy):
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 1})
    assert features.check(s, "users", unique=["user_id"])["status"] == "ok"
    again = features.check(s, "users", unique=["user_id"])
    assert again["status"] == "block"
    assert "Loop" in again["next_step"]


# -- budgets per agent and session, velocity ------------------------------------------------------
def test_agent_day_budget_per_agent(billed):
    s = billed(agent_day_usd={"default": 0.5, "big": 2.0})
    with use(Identity("small")):
        assert core.run(s, Q)["status"] == "ok"
        res = core.run(s, Q + " order by 1")
        assert res["status"] == "block" and res["reason"] == "agent_budget"
        assert "small" in res["next_step"]
    with use(Identity("big")):
        assert core.run(s, Q)["status"] == "ok"
        assert core.run(s, Q + " order by 1")["status"] == "ok"
    assert {e["agent"] for e in ledger(s)} == {"small", "big"}


def test_session_budget(billed, monkeypatch):
    monkeypatch.setenv("GULLIVER_SESSION", "s1")
    s = billed(session_usd=0.5)
    assert core.run(s, Q)["status"] == "ok"
    res = core.run(s, Q + " order by 1")
    assert res["status"] == "block" and res["reason"] == "session_budget"
    monkeypatch.setenv("GULLIVER_SESSION", "s2")
    assert core.run(s, Q + " order by 1")["status"] == "ok"
    status = core.status(s)["caller"]
    assert status["session"] == "s2" and status["session_spent_usd"] == pytest.approx(0.3)


def test_spend_velocity_needs_approval(billed):
    s = billed(velocity_usd=0.5)
    assert core.run(s, Q)["status"] == "ok"
    res = core.run(s, Q + " order by 1")
    assert res["status"] == "needs_approval" and res["reason"] == "velocity"
    core.approve(s, res["receipt_id"])
    assert core.run(s, Q + " order by 1")["status"] == "ok"


def test_rehearsal_respects_agent_budget(billed, monkeypatch):
    s = billed(agent_day_usd=0.3)
    assert core.run(s, Q)["status"] == "ok"
    monkeypatch.setattr(core, "SMALL_DATA_ROWS", 10)
    res = core.rehearse(s, Q)
    assert res["status"] == "block" and res["reason"] == "agent_budget"


def test_mcp_clients_get_their_own_agent_budget(billed, monkeypatch, tmp_path):
    from mcp import Client
    from mcp.types import Implementation

    from gulliver.mcp_server import build_server

    monkeypatch.setenv("GULLIVER_DSN", billed().connection().params["path"])
    (tmp_path / "gulliver.toml").write_text(
        "[budget]\nmax_query_usd = 1.0\napproval_over_usd = 1.0\nrehearse_over_usd = 10.0\nday_usd = 100.0\n"
        "velocity_usd = 0\nmax_repeats = 0\nagent_day_usd = { default = 0.5, \"agent-b\" = 5 }\n")
    server = build_server()

    async def two_runs(name):
        async with Client(server, client_info=Implementation(name=name, version="1")) as client:
            out = []
            for sql in (Q, Q + " order by 1"):
                result = await client.call_tool("gulliver_run", {"sql": sql})
                out.append(json.loads(result.content[0].text))
            status = json.loads((await client.call_tool("gulliver_status", {})).content[0].text)
            return out, status

    a, a_status = asyncio.run(two_runs("agent-a"))
    b, _ = asyncio.run(two_runs("agent-b"))
    assert [r["status"] for r in a] == ["ok", "block"] and a[1]["reason"] == "agent_budget"
    assert [r["status"] for r in b] == ["ok", "ok"]
    assert a_status["caller"]["agent"] == "agent-a" and a_status["caller"]["agent_day_limit_usd"] == 0.5
    entries = [json.loads(x) for x in (tmp_path / "state" / "ledger.jsonl").read_text().splitlines()]
    assert {e["agent"] for e in entries} == {"agent-a", "agent-b"}
    assert len({e["session"] for e in entries}) == 2 and all(e["session"].startswith("mcp-") for e in entries)


def test_human_set_agent_wins_over_client_name(monkeypatch):
    monkeypatch.setenv("GULLIVER_AGENT", "nightly-job")
    monkeypatch.setenv("GULLIVER_TASK", "fix the dashboard")
    ident = from_env("whatever")
    assert ident.agent == "nightly-job" and ident.source == "env" and ident.task == "fix-the-dashboard"
    monkeypatch.delenv("GULLIVER_AGENT")
    assert from_env("a b*/").agent == "a-b"


# -- query tags ----------------------------------------------------------------------------------------
def test_tags_carry_identity_and_receipt(small_db, state):
    s = load_settings(dsn=small_db)
    seen = {}
    original = DuckDBEngine.execute

    def spy(self, sql, *args, **kwargs):
        seen.update(self.tags)
        return original(self, sql, *args, **kwargs)

    with use(Identity("tagger", session="s9", task="t1")):
        DuckDBEngine.execute = spy
        try:
            res = core.run(s, Q)
        finally:
            DuckDBEngine.execute = original
    assert seen == {"tool": "gulliver", "kind": "run", "agent": "tagger", "session": "s9", "task": "t1",
                    "receipt": res["receipt_id"]}


class FakeBQClient:
    def __init__(self):
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return SimpleNamespace(total_bytes_processed=1, total_bytes_billed=1, referenced_tables=[],
                               result=lambda timeout=None, page_size=None: [])

    def get_table(self, ref):
        if ref.endswith("_view"):
            return SimpleNamespace(table_type="VIEW", modified=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return SimpleNamespace(table_type="TABLE", modified=datetime(2026, 1, 1, tzinfo=timezone.utc), num_rows=5,
                               num_bytes=50, streaming_buffer=None)


def test_bigquery_job_labels_and_change_signal():
    client = FakeBQClient()
    eng = BigQueryEngine(Connection("bq", "bigquery", {"project": "p"}), _client=client)
    eng.tags = {"tool": "gulliver", "kind": "run", "agent": "Claude Code", "receipt": "gv-20261102-run-ab12"}
    eng.execute("select 1", preview_rows=1)
    assert client.calls[-1][1]["labels"] == {"gulliver": "run", "gulliver_agent": "claude-code",
                                             "gulliver_receipt": "gv-20261102-run-ab12"}
    assert eng.data_version(["ds.events"]).startswith("bigquery:ds.events:2026-01-01")
    assert eng.data_version(["ds.events_view"]) is None
    assert all(len(v) <= 63 for v in job_labels({"kind": "x" * 100}).values())


class FakeSnowCursor:
    def __init__(self, log, rows):
        self.log, self.rows, self.description, self.sfqid = log, rows, None, None

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if "information_schema.tables" in sql:
            self.description = [("TABLE_TYPE",)]
        elif sql.lower().startswith("select"):
            self.description = [("N",)]

    def fetchall(self):
        return self.rows

    def fetchmany(self, n):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class FakeSnowConn:
    def __init__(self, rows):
        self.log, self.rows = [], rows

    def cursor(self):
        return FakeSnowCursor(self.log, list(self.rows))


def test_snowflake_query_tag_and_change_signal():
    conn = FakeSnowConn([("BASE TABLE", "2026-01-01 10:00", 10, 100)])
    eng = SnowflakeEngine(Connection("sf", "snowflake", {"account": "a"}), _conn=conn)
    eng.tags = {"tool": "gulliver", "kind": "run", "agent": "a1"}
    eng.execute("select 1", preview_rows=1)
    tag_calls = [c for c in conn.log if "QUERY_TAG" in c[0]]
    assert tag_calls and json.loads(tag_calls[0][1][0])["agent"] == "a1"
    assert eng.data_version(["PUBLIC.EVENTS"]).startswith("snowflake:PUBLIC.EVENTS:2026-01-01 10:00")
    assert eng.data_version(["OTHERDB.PUBLIC.EVENTS"]) is None
    view = SnowflakeEngine(Connection("sf", "snowflake", {"account": "a"}), _conn=FakeSnowConn([("VIEW", "x", 1, 1)]))
    assert view.data_version(["V"]) is None


def test_postgres_application_name():
    name = application_name({"kind": "run", "agent": "claude-code", "receipt": "gv-20261102-120000-run-ab12"})
    assert name == "gulliver:gv-20261102-120000-run-ab12:claude-code"
    assert len(application_name({"kind": "run", "agent": "ü" * 100})) <= 63


@pytest.mark.skipif(not os.environ.get("GULLIVER_TEST_PG_DSN"), reason="set GULLIVER_TEST_PG_DSN to run live Postgres")
def test_postgres_live_application_name(state):
    s = load_settings(dsn=os.environ["GULLIVER_TEST_PG_DSN"])
    with use(Identity("pg-agent")):
        res = core.run(s, "select application_name from pg_stat_activity where pid = pg_backend_pid()")
    assert res["status"] == "ok", res
    assert res["preview"][0]["application_name"] == f"gulliver:{res['receipt_id']}:pg-agent"


# -- spend report ---------------------------------------------------------------------------------------
def test_spend_by_agent_day_and_table(billed, capsys):
    s = billed()
    with use(Identity("a")):
        core.run(s, Q)
        core.run(s, "select count(*) from events e join users u on e.user_id = u.user_id")
    with use(Identity("b")):
        core.run(s, Q)
    by_agent = runaway.spend(s, by="agent")
    assert [(r["agent"], r["usd"]) for r in by_agent["rows"]] == [("a", 0.6), ("b", 0.3)]
    assert by_agent["total"]["usd"] == pytest.approx(0.9)
    by_table = {r["table"]: r for r in runaway.spend(s, by="table")["rows"]}
    assert by_table["users"]["usd"] == pytest.approx(0.75) and by_table["events"]["usd"] == pytest.approx(0.15)
    assert sum(r["usd"] for r in by_table.values()) == pytest.approx(0.9)
    day = runaway.spend(s, by="day")["rows"]
    assert len(day) == 1 and day[0]["executions"] == 3
    assert runaway.spend(s, by="nope")["status"] == "error"
    csv_text = runaway.render_spend(by_agent, "csv")
    assert csv_text.splitlines()[0] == "agent,usd,executions,runs,bytes"
    assert "| a | $0.6000 |" in runaway.render_spend(by_agent)


def test_spend_refuses_days_above_365(billed):
    """A window wider than a year is refused before it can overflow the date arithmetic (#107)."""
    s = billed()
    for bad in (1000000, 99999999999):
        out = runaway.spend(s, days=bad)
        assert out["status"] == "error", out
        assert "between 1 and 365" in out["error"]


def test_cli_agent_flag_and_spend(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "--agent", "cli-bot", "run", "--json", Q]) == 0
    capsys.readouterr()
    assert main(["--dsn", demo_db, "spend", "--by", "agent", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["rows"][0]["agent"] == "cli-bot"
    assert main(["--dsn", demo_db, "spend", "--by", "table", "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("table,usd")


def test_cli_spend_output_file(demo_db, state, tmp_path, capsys):
    """`gulliver spend -o PATH` writes the printed report as UTF-8 and confirms the path."""
    assert main(["--dsn", demo_db, "--agent", "out-bot", "run", "--json", Q]) == 0
    capsys.readouterr()
    assert main(["--dsn", demo_db, "spend", "--by", "agent", "--format", "csv"]) == 0
    expected = capsys.readouterr().out
    out = tmp_path / "spend.csv"
    assert main(["--dsn", demo_db, "spend", "--by", "agent", "--format", "csv", "-o", str(out)]) == 0
    assert capsys.readouterr().out.strip() == f"Wrote {out}"
    assert out.read_text(encoding="utf-8") == expected
    assert expected.startswith("agent,usd") and "out-bot" in expected


def test_spend_refuses_a_window_below_one_day(settings, demo_db, capsys):
    for days in (0, -5):
        res = runaway.spend(settings, by="agent", days=days)
        assert res["status"] == "error" and "--days" in res["error"] and res["next_step"]
        assert main(["--dsn", demo_db, "spend", "--days", str(days), "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"
        assert main(["--dsn", demo_db, "spend", "--days", str(days)]) == 1
        assert "--days" in capsys.readouterr().out
    assert runaway.spend(settings, by="agent", days=1)["status"] == "ok"  # one day is still a window


# -- audit chain ------------------------------------------------------------------------------------------
def _some_history(settings):
    core.preflight(settings, Q)
    core.run(settings, Q)
    core.run(settings, "select count(*) from users")


def test_audit_verify_intact(settings):
    _some_history(settings)
    res = runaway.audit_verify(settings)
    assert res["status"] == "ok", res
    assert res["ledger"]["chained"] == 2 and res["receipts"]["files_checked"] == 3
    assert runaway.audit_verify(settings, head=res["ledger"]["head"])["status"] == "ok"


def test_audit_detects_an_edited_receipt(settings, capsys):
    _some_history(settings)
    rid = core.get_receipt(settings, "latest", fmt="json")["receipt"]["receipt_id"]
    path = settings.state_dir / "receipts" / f"{rid}.json"
    path.write_text(path.read_text().replace('"status": "ok"', '"status": "block"'))
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail"
    assert any(rid in p and "changed" in p for p in res["problems"])
    assert main(["audit", "verify", "--json"]) == 4


def test_audit_detects_edited_and_removed_ledger_lines(settings):
    _some_history(settings)
    core.run(settings, "select 1 as x")
    lines = (settings.state_dir / "ledger.jsonl").read_text().splitlines()
    head = runaway.audit_verify(settings)["ledger"]["head"]
    edited = lines[0].replace('"cost_usd": 0.0', '"cost_usd": 0.0001')
    (settings.state_dir / "ledger.jsonl").write_text("\n".join([edited, *lines[1:]]) + "\n")
    assert any("line 1 was changed" in p for p in runaway.audit_verify(settings)["problems"])
    (settings.state_dir / "ledger.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert any("line 2 does not follow" in p for p in runaway.audit_verify(settings)["problems"])
    (settings.state_dir / "ledger.jsonl").write_text("\n".join(lines[:2]) + "\n")   # newest line removed
    assert runaway.audit_verify(settings)["status"] == "ok"                          # ... invisible on its own
    assert runaway.audit_verify(settings, head=head)["status"] == "fail"            # ... but not with a kept head


def test_audit_key_makes_hashes_hmacs(settings, monkeypatch):
    monkeypatch.setenv(audit.KEY_ENV, "s3cret")
    _some_history(settings)
    assert runaway.audit_verify(settings)["status"] == "ok"
    assert all(e["alg"] == audit.HMAC for e in ledger(settings))
    monkeypatch.delenv(audit.KEY_ENV)
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and any("set GULLIVER_AUDIT_KEY" in p for p in res["problems"])


def test_audit_counts_lines_written_before_the_chain(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "ledger.jsonl").write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "kind": "run",
                                                                 "cost_usd": 0.0}) + "\n")
    _some_history(settings)
    res = runaway.audit_verify(settings)
    assert res["status"] == "warn" and not res["problems"]
    assert res["ledger"]["entries"] == 3 and res["ledger"]["chained"] == 2


# -- config and defaults ---------------------------------------------------------------------------------
def test_budget_config(tmp_path, monkeypatch, state):
    (tmp_path / "gulliver.toml").write_text('[budget]\nagent_day_usd = { default = 5, "claude-code" = 10 }\n'
                                            "session_usd = 3\n")
    s = load_settings()
    assert s.budget.agent_day_usd == {"default": 5.0, "claude-code": 10.0}
    assert runaway.agent_limit(s, "claude-code") == 10.0 and runaway.agent_limit(s, "other") == 5.0
    monkeypatch.setenv("GULLIVER_AGENT_DAY_USD", "2")
    assert load_settings().budget.agent_day_usd == {"default": 2.0}
    with pytest.raises(ConfigError):
        load_settings(budget_overrides={"agent_day_usd": {"x": "lots"}})
    b = load_settings(config_path=None, budget_overrides={}).budget
    assert b.max_repeats == 5 and b.repeat_window_min == 30 and b.velocity_usd > b.approval_over_usd


def test_status_shows_the_caller(settings):
    caller = core.status(settings)["caller"]
    assert caller["agent"] == "local" and caller["agent_day_limit_usd"] is None
    assert caller["repeat_guard"]["max_repeats"] == 5


# -- regressions from review ------------------------------------------------------------------------
def _rechain_plain(path, mutate=lambda e: e):
    """What an agent without the key can do: rewrite a chain as plain sha256."""
    prev, out = audit.GENESIS, []
    for line in path.read_text().splitlines():
        entry = mutate({k: v for k, v in json.loads(line).items() if k not in ("alg", "hash", "prev")})
        entry["prev"] = prev
        entry["hash"] = audit.entry_hash(entry)
        prev = entry["hash"]
        out.append(json.dumps(entry))
    path.write_text("\n".join(out) + "\n")


def test_audit_key_cannot_be_bypassed_by_a_plain_rechain(settings, monkeypatch):
    monkeypatch.setenv(audit.KEY_ENV, "s3cret")
    _some_history(settings)
    assert runaway.audit_verify(settings)["status"] == "ok"
    led, trail = settings.state_dir / "ledger.jsonl", settings.state_dir / "audit.jsonl"
    _rechain_plain(led, lambda e: {**e, "cost_usd": 0.0, "agent": "someone-else"})
    _rechain_plain(trail)
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and any("not keyed" in p for p in res["problems"]), res
    # ... and deleting the keyed-genesis markers does not help: with the key, line 1 must be keyed
    for marker in settings.state_dir.glob("*.keyed"):
        marker.unlink()
    assert runaway.audit_verify(settings)["status"] == "fail"


def test_audit_key_catches_stripped_hashes(settings, monkeypatch):
    monkeypatch.setenv(audit.KEY_ENV, "s3cret")
    _some_history(settings)
    led = settings.state_dir / "ledger.jsonl"
    lines = [json.loads(line) for line in led.read_text().splitlines()]
    led.write_text("".join(json.dumps({**{k: v for k, v in e.items() if k not in ("hash", "prev", "alg")},
                                       "cost_usd": 0.0}) + "\n" for e in lines))
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and "Intact" not in res["next_step"]
    monkeypatch.delenv(audit.KEY_ENV)   # without key and markers too: hashless lines newer than the trail
    for marker in settings.state_dir.glob("*.keyed"):
        marker.unlink()
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and any("newer than the chain" in p for p in res["problems"]), res


def test_audit_key_turned_on_later_keeps_older_lines_as_legacy(settings, monkeypatch):
    core.run(settings, "select 1 as x")
    monkeypatch.setenv(audit.KEY_ENV, "s3cret")
    core.run(settings, "select 2 as x")
    res = runaway.audit_verify(settings)
    assert res["status"] == "ok", res
    led = settings.state_dir / "ledger.jsonl"
    lines = led.read_text().splitlines()
    led.write_text(lines[0] + "\n")   # drop the keyed line: the marker still says keying started
    assert runaway.audit_verify(settings)["status"] == "fail"


def test_audit_reports_a_hand_written_approval_and_ignores_it(settings):
    _some_history(settings)
    rec = core.get_receipt(settings, "latest", fmt="json")["receipt"]
    forged = {"receipt_id": rec["receipt_id"], "sql_hash": rec["sql_hash"], "connection": rec["connection"],
              "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "expires_at": "2099-01-01T00:00:00+00:00", "max_cost_usd": 1000.0}
    path = settings.state_dir / "approvals" / f"{rec['receipt_id']}.json"
    path.write_text(json.dumps(forged, indent=2))
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and any("no audit entry" in p for p in res["problems"])
    assert "Intact" not in res["next_step"]
    assert main(["audit", "verify", "--json"]) == 4
    store = core.Store(settings.state_dir)
    assert store.find_approval(rec["sql_hash"], rec["connection"]) is None


def test_audit_reports_a_deleted_trail(settings):
    _some_history(settings)
    (settings.state_dir / "audit.jsonl").unlink()
    res = runaway.audit_verify(settings)
    assert res["status"] == "fail" and "Intact" not in res["next_step"]


def test_audit_warnings_are_not_called_intact(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "ledger.jsonl").write_text(json.dumps({"at": "2026-01-01T00:00:00+00:00", "kind": "run",
                                                                 "cost_usd": 0.0}) + "\n")
    _some_history(settings)
    res = runaway.audit_verify(settings)
    assert res["status"] == "warn" and "Intact" not in res["next_step"]


def test_replacement_scan_of_a_file_sees_the_file_change(tmp_path, state, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "f.duckdb"
    duckdb.connect(str(db)).close()
    duckdb.sql("copy (select * from range(1000)) to 'x.parquet' (format parquet)")
    s = load_settings(dsn=str(db), budget_overrides={"max_repeats": 2})
    sql = "select count(*) as n from 'x.parquet'"
    assert [core.run(s, sql)["preview"] for _ in range(2)] == [[{"n": 1000}]] * 2
    duckdb.sql("copy (select * from range(10)) to 'x.parquet' (format parquet)")
    third = core.run(s, sql)
    assert third["status"] == "ok" and third["preview"] == [{"n": 10}], third
    eng = DuckDBEngine(Connection("f", "duckdb", {"path": str(db)}))
    try:
        assert "x.parquet" in str(eng.data_version(['"x.parquet"']))
        assert eng.data_version(['"missing.csv"']) is None      # not a table, not a file: no signal
    finally:
        eng.close()


@pytest.mark.parametrize("sql", [
    "select count(*) from users where signup_date > get_current_timestamp() - interval 1 day",
    "select uuidv7() from users", "select uuidv4() from users", "select txid_current() from users",
    "select nextval('s') from users", "select my_random_thing() from users"])
def test_more_nondeterministic_functions(sql):
    from gulliver.sqlsafety import classify

    assert runaway.deterministic(classify(sql, "duckdb").select) is False


def test_approving_a_rehearsal_does_not_approve_the_run_cost(billed, monkeypatch):
    s = billed(approval_over_usd=0.0, velocity_usd=0.1, max_repeats=0)
    first = core.run(s, Q)
    assert first["status"] == "needs_approval"
    monkeypatch.setattr(core, "SMALL_DATA_ROWS", 10)
    store = core.Store(s.state_dir)
    store.record_spend({"kind": "run", "connection": "default", "cost_usd": 0.2})
    reh = core.rehearse(s, Q)
    assert reh["status"] == "needs_approval" and reh["reason"] == "velocity", reh
    core.approve(s, reh["receipt_id"])
    after = core.run(s, Q)
    assert after["status"] == "needs_approval" and after.get("reason") != "velocity", after
    core.approve(s, after["receipt_id"])          # a cost approval for the run itself does work
    assert core.run(s, Q)["status"] == "ok"


def test_an_approval_does_not_cover_a_higher_cost(billed, monkeypatch):
    s = billed(approval_over_usd=0.0)
    res = core.run(s, Q)
    core.approve(s, res["receipt_id"])
    monkeypatch.setattr(FixedCostDuckDB, "PRICE", 0.9)
    assert core.run(s, Q)["status"] == "needs_approval"


def test_free_queries_without_a_byte_estimate_are_cheap_repeats(settings):
    s = load_settings(dsn=settings.connection().params["path"], budget_overrides={"max_repeats": 2})
    for sql in ("select 42 as answer", "select count(*) as n from users"):
        results = [core.run(s, sql) for _ in range(4)]
        assert [r["status"] for r in results] == ["ok"] * 4, results[-1]
        assert "cheap" in results[-1]["runaway"]["note"]


def test_free_queries_on_a_view_are_cheap_repeats(tmp_path, state):
    """A view has no size in duckdb_tables(): it counts as the tables it reads (also through a view and a CTE)."""
    path = tmp_path / "views.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create table events as select i as id from range(5000) t(i)")
    con.execute("create view v_events as select * from events")
    con.execute("create view v_nested as with x as (select * from v_events) select * from x")
    con.close()
    s = load_settings(dsn=str(path), budget_overrides={"max_repeats": 2})
    for sql in ("select count(*) as n from v_events", "select count(*) as n from v_nested"):
        results = [core.run(s, sql) for _ in range(4)]
        assert [r["status"] for r in results] == ["ok"] * 4, results[-1]
        assert "cheap" in results[-1]["runaway"]["note"]


def test_velocity_counts_the_agent_across_sessions(billed, monkeypatch):
    s = billed(velocity_usd=0.7)
    with use(Identity("claude-code", session="s1")):
        assert core.run(s, Q)["status"] == "ok"
    with use(Identity("claude-code", session="s2")):
        assert core.run(s, Q + " order by 1")["status"] == "ok"
    with use(Identity("claude-code", session="s3")):
        res = core.run(s, Q + " order by 2")
        assert res["status"] == "needs_approval" and res["reason"] == "velocity"
        assert core.status(s)["caller"]["velocity"]["spent_usd"] == pytest.approx(0.6)
    with use(Identity("other", session="s4")):
        assert core.run(s, Q + " order by 2")["status"] == "ok"


def test_receipt_ids_have_enough_randomness_and_cache_checks_the_query(settings):
    store = core.Store(settings.state_dir)
    assert len({store.new_id("run") for _ in range(5000)}) == 5000
    store.save_receipt({"receipt_id": "gv-x", "status": "ok", "sql_hash": "other", "connection": "default",
                        "preview": [{"secret": 1}]})
    assert runaway._cached(store, {"receipt": "gv-x", "sql_hash": "mine", "connection": "default"}) is None
    assert runaway._cached(store, {"receipt": "gv-x", "sql_hash": "other", "connection": "default"})["preview"]


def test_a_changing_comment_is_still_the_same_query(small_db, state, loopy):
    from gulliver.sqlsafety import classify

    hashes = {classify(q, "duckdb").sql_hash for q in (
        "select count(*) from users", "select count(*) from users -- retry 2",
        "/* attempt 3 */ select count(*) from users")}
    assert len(hashes) == 1
    s = load_settings(dsn=small_db, budget_overrides={"max_repeats": 2})
    core.run(s, Q + " -- try 1")
    core.run(s, Q + " -- try 2")
    assert core.run(s, Q + " -- try 3")["status"] == "block"


def test_spend_labels_missing_session_as_none_not_old(demo_db, state, capsys):
    s = load_settings(dsn=demo_db)
    core.run(s, "select 1 as x")
    for by in ("session", "task", "table"):
        res = runaway.spend(s, by=by)
        assert [g[by] for g in res["rows"]] == ["(none)"], res
        assert not any("before Gulliver recorded" in n for n in res["notes"])



# -- prune and audit preservation -------------------------------------------------------------------------
def test_prune_removes_old_receipts_and_audit_verify_stays_ok(settings):
    from gulliver.store import iso, now_utc

    store = core.Store(settings.state_dir)
    old_time = iso(now_utc() - timedelta(days=40))

    # Save 2 old receipts and 1 recent receipt
    r1 = store.save_receipt({"receipt_id": "gv-20200101-000000-run-aaa", "action": "run",
                             "created_at": old_time, "status": "ok", "sql": "select 1"})
    r2 = store.save_receipt({"receipt_id": "gv-20200101-000001-run-bbb", "action": "run",
                             "created_at": old_time, "status": "ok", "sql": "select 2"})
    r3 = store.save_receipt({"receipt_id": "gv-20261108-000002-run-ccc", "action": "run",
                             "status": "ok", "sql": "select 3"})

    # Prior to prune, audit_verify is ok
    res_before = runaway.audit_verify(settings)
    assert res_before["status"] == "ok"
    assert (store.receipts_dir / f"{r1}.json").exists()

    # Prune older than 30d
    res_prune = runaway.prune(settings, older_than="30d")
    assert res_prune["status"] == "ok"
    assert res_prune["pruned_receipts"] == 2
    assert not (store.receipts_dir / f"{r1}.json").exists()
    assert not (store.receipts_dir / f"{r2}.json").exists()
    assert (store.receipts_dir / f"{r3}.json").exists()

    # After prune, audit_verify stays ok and notes the pruned count
    res_after = runaway.audit_verify(settings)
    assert res_after["status"] == "ok"
    assert any("2 file(s) were pruned" in n for n in res_after["notes"])


def test_prune_dry_run_leaves_files_untouched(settings):
    from gulliver.store import iso, now_utc

    store = core.Store(settings.state_dir)
    old_time = iso(now_utc() - timedelta(days=40))

    r1 = store.save_receipt({"receipt_id": "gv-20200101-000000-run-dry", "action": "run",
                             "created_at": old_time, "status": "ok"})

    res_dry = runaway.prune(settings, older_than="30d", dry_run=True)
    assert res_dry["status"] == "ok"
    assert res_dry["dry_run"] is True
    assert res_dry["pruned_receipts"] == 1
    assert (store.receipts_dir / f"{r1}.json").exists()

    # Audit trail has no prune entry
    trail = (settings.state_dir / "audit.jsonl").read_text()
    assert '"kind": "prune"' not in trail


def test_prune_keep_last_protects_newest_receipts(settings):
    from gulliver.store import iso, now_utc

    store = core.Store(settings.state_dir)
    old_time = iso(now_utc() - timedelta(days=40))

    ids = []
    for i in range(5):
        rid = store.save_receipt({"receipt_id": f"gv-20200101-00000{i}-run-k{i}",
                                 "action": "run", "created_at": old_time, "status": "ok"})
        ids.append(rid)

    # Prune with keep_last=2 -> keeps 2 newest, prunes 3
    res = runaway.prune(settings, older_than="30d", keep_last=2)
    assert res["pruned_receipts"] == 3
    # 2 newest remain
    remaining = [p.stem for p in store.receipts_dir.glob("*.json")]
    assert len(remaining) == 2


def test_prune_protects_live_rehearsals_and_approvals(settings):
    from gulliver.store import iso, now_utc

    store = core.Store(settings.state_dir)
    now = now_utc()
    recent = iso(now - timedelta(hours=2))
    old_time = iso(now - timedelta(days=40))

    # 1. Live rehearsal within 24h
    r_rehearse = store.save_receipt({"receipt_id": "gv-rehearse-live", "action": "rehearse",
                                     "created_at": recent, "sql_hash": "h1", "connection": "c1", "status": "ok"})
    # 2. Receipt with active approval
    r_approved = store.save_receipt({"receipt_id": "gv-approved-rec", "action": "run",
                                     "created_at": old_time, "status": "ok"})
    app_live = {
        "receipt_id": r_approved, "sql_hash": "h2", "connection": "c1",
        "approved_at": recent, "expires_at": iso(now + timedelta(hours=20)),
        "max_cost_usd": 10.0
    }
    (store.approvals_dir / f"{r_approved}.json").write_text(json.dumps(app_live), encoding="utf-8")
    store._audit("approval", r_approved, json.dumps(app_live).encode("utf-8"))

    # 3. Receipt with expired approval
    r_expired = store.save_receipt({"receipt_id": "gv-expired-rec", "action": "run",
                                    "created_at": old_time, "status": "ok"})
    app_exp = {
        "receipt_id": r_expired, "sql_hash": "h3", "connection": "c1",
        "approved_at": old_time, "expires_at": old_time,
        "max_cost_usd": 10.0
    }
    (store.approvals_dir / f"{r_expired}.json").write_text(json.dumps(app_exp), encoding="utf-8")
    store._audit("approval", r_expired, json.dumps(app_exp).encode("utf-8"))

    # Prune older than 1h
    res = runaway.prune(settings, older_than="1h")
    assert res["status"] == "ok"
    # Live rehearsal is protected by REHEARSAL_TTL
    assert (store.receipts_dir / f"{r_rehearse}.json").exists()
    # Live approval and its receipt are protected
    assert (store.receipts_dir / f"{r_approved}.json").exists()
    assert (store.approvals_dir / f"{r_approved}.json").exists()
    # Expired approval and its old receipt are pruned
    assert not (store.receipts_dir / f"{r_expired}.json").exists()
    assert not (store.approvals_dir / f"{r_expired}.json").exists()

    # Audit verify stays ok
    verify = runaway.audit_verify(settings)
    assert verify["status"] == "ok"


def test_audit_fails_if_receipt_deleted_manually_without_prune(settings):
    _some_history(settings)
    rid = core.get_receipt(settings, "latest", fmt="json")["receipt"]["receipt_id"]
    (settings.state_dir / "receipts" / f"{rid}.json").unlink()

    res = runaway.audit_verify(settings)
    assert res["status"] == "fail"
    assert any(rid in p and "deleted" in p for p in res["problems"])


def test_prune_cli(settings, capsys):
    _some_history(settings)
    # CLI dry run
    code = main(["prune", "--older-than", "30d", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "Freed:" in out

    # CLI JSON
    code = main(["prune", "--older-than", "30d", "--dry-run", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "ok" and data["dry_run"] is True

    # Bad argument
    with pytest.raises(SystemExit):
        main(["prune", "--older-than", "invalid_period"])
