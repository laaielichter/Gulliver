"""gulliver top: history sources, fingerprints, ranking, priced fixes and summary-table candidates."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from gulliver import core, workload
from gulliver.cli import main
from gulliver.config import load_settings
from gulliver.engines.bigquery import TIB, BigQueryEngine
from gulliver.engines.postgres import PostgresEngine
from gulliver.engines.snowflake import SnowflakeEngine


# -- fingerprints ---------------------------------------------------------------------------------
def test_literal_variants_share_a_fingerprint():
    fp = workload.fingerprint
    a = fp("select count(*) from events where user_id = 42 and country in ('NL', 'BE')")
    b = fp("SELECT COUNT(*)\nFROM events -- daily\nWHERE user_id = 7 AND country IN ('DE')")
    c = fp("select count(*) from events where user_id = $1 and country in ($2, $3, $4)", "postgres")
    assert a == b == c
    assert fp("select count(*) from events where user_id = 42") != a  # a different shape stays apart
    assert fp("select sum(amount) from events where user_id = 42") != fp("select count(*) from events where user_id = 42")


def test_unparsable_sql_falls_back_to_engine_hash_then_text():
    broken = "select from where ((("
    assert workload.fingerprint(broken, engine_hash="abc") == workload.fingerprint(broken + " 1", engine_hash="abc")
    assert workload.fingerprint("selec x frm t where a = 1 ((") == workload.fingerprint("selec x frm t where a = 2 ((")


# -- file source ----------------------------------------------------------------------------------
def _write_csv(path, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["query", "calls", "bytes", "runtime_s", "cost_usd"])
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def workload_csv(tmp_path):
    rows = []
    for day in range(1, 8):  # one shape, seven literal variants, 10 calls each
        rows.append({"query": f"select event_type, count(*) from events where date(ts) = '2025-01-0{day}' group by 1",
                     "calls": 10, "bytes": 1000, "runtime_s": 2.0, "cost_usd": 3.0})
    rows.append({"query": "SELECT * FROM users", "calls": 3, "bytes": 50, "runtime_s": 1.0, "cost_usd": 1.0})
    rows.append({"query": "select count(*) from users where country = 'NL'", "calls": 1, "bytes": 10,
                 "runtime_s": 0.1, "cost_usd": 0.5})
    rows.append({"query": "", "calls": 1})  # no query text: skipped
    path = tmp_path / "history.csv"
    _write_csv(path, rows)
    return str(path)


def test_file_source_groups_ranks_and_projects(settings, workload_csv):
    r = workload.top(settings, source=f"csv:{workload_csv}", days=7)
    assert r["source"] == "file" and r["rank_by"] == "cost"
    assert r["totals"]["groups"] == 3 and r["totals"]["calls"] == 74 and r["totals"]["queries"] == 9
    first = r["top"][0]
    assert first["calls"] == 70 and first["variants"] == 7 and first["cost_usd"] == pytest.approx(21.0)
    assert first["cost_per_month_usd"] == pytest.approx(90.0)  # 21 USD in 7 days -> 30 days
    assert first["share_pct"] == pytest.approx(21 / 22.5 * 100, abs=0.1)
    assert [t["cost_usd"] for t in r["top"]] == sorted((t["cost_usd"] for t in r["top"]), reverse=True)
    assert any("skipped" in n for n in r["notes"])


def test_top_query_gets_a_same_rows_fix_with_honest_saving(settings, workload_csv):
    r = workload.top(settings, source="file", path=workload_csv)
    analysis = r["top"][0]["analysis"]
    assert analysis["fix"]["same_result"] and analysis["fix"]["rules"] == ["GV005"]
    assert "ts >=" in analysis["fix"]["fixed_sql"] and "date(" not in analysis["fix"]["fixed_sql"].lower()
    assert analysis["estimate"]["before"]["bytes"] and analysis["estimate"]["after"]["bytes"]
    saving = analysis["saving"]
    # DuckDB's estimate is a heuristic over whole tables: it does not move, so Gulliver claims no saving
    assert saving["accuracy"] == "heuristic" and saving["scan_saved_pct"] == 0 and saving["per_month_usd"] == 0
    assert "note" in saving
    assert r["status"] == "warn" and "no scan change" in r["next_step"]
    star = next(t for t in r["top"] if "users" in t["sql"] and "*" in t["sql"])
    assert {f["rule"] for f in star["analysis"]["lint"]} >= {"GV001"}


def test_jsonl_without_connection_dialect_is_lint_only(settings, tmp_path):
    path = tmp_path / "jobs.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"sql": "SELECT a FROM `p.d.t` WHERE DATE(ts) = '2026-01-01'", "count": 4, "cost": 2.5},
        {"sql": "SELECT a FROM `p.d.t` WHERE DATE(ts) = '2026-01-02'", "count": 1, "cost": 1.5},
    ]), encoding="utf-8")
    r = workload.top(settings, path=str(path), dialect="bigquery", analyze=1)
    assert r["dialect"] == "bigquery" and r["totals"]["groups"] == 1 and r["top"][0]["calls"] == 5
    assert "estimate" not in r["top"][0]["analysis"]
    assert any("differs from connection" in n for n in r["notes"])


def test_materialize_candidates(settings, tmp_path):
    rows = [{"query": f"select country, count(*) from users where signup_date >= '2021-0{1 + i % 9}-01' group by 1",
             "calls": 1, "bytes": 1000, "runtime_s": 1.0, "cost_usd": 0.10} for i in range(140)]  # 20/day, same bytes
    rows += [{"query": f"select count(*) from events where user_id = {i}", "calls": 1, "bytes": 10 + i * 1000,
              "runtime_s": 1.0, "cost_usd": 0.10} for i in range(140)]  # scans differ: not the same work
    rows += [{"query": f"select event_id from events where user_id = {i}", "calls": 1, "bytes": 1000,
              "runtime_s": 1.0, "cost_usd": 0.10} for i in range(140)]  # not an aggregate
    path = tmp_path / "many.csv"
    _write_csv(path, rows)
    r = workload.top(settings, path=str(path), days=7, analyze=0)
    assert len(r["materialize"]) == 1
    m = r["materialize"][0]
    assert m["calls_per_day"] == 20 and m["accuracy"] == "heuristic"
    assert m["cost_per_month_usd"] == pytest.approx(60.0)       # 0.10 x 20/day x 30
    assert m["saving_per_month_usd"] == pytest.approx(57.0)     # minus one refresh a day
    assert any("variants" in n for n in m["notes"])


def test_bad_inputs(settings, tmp_path):
    assert workload.top(settings, source="nope")["status"] == "error"
    assert workload.top(settings, source="file")["status"] == "error"
    assert "not found" in workload.top(settings, path=str(tmp_path / "missing.csv"))["error"]
    wrong = workload.top(settings, source="bigquery_jobs")
    assert wrong["status"] == "error" and "bigquery connection" in wrong["error"]
    assert workload.top(settings, days=0)["status"] == "error"
    assert workload.top(settings, limit=0)["status"] == "error"
    limited = workload.top(settings, limit=-1)
    assert limited["status"] == "error" and limited["error"] == "--limit must be at least 1." and limited["next_step"]
    analyzed = workload.top(settings, analyze=-1)
    assert analyzed["status"] == "error" and analyzed["error"] == "--analyze must be at least 0."


def test_cli_top_refuses_limit_and_analyze_below_range(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "top", "--limit", "0", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error" and "--limit must be at least 1." in data["error"]
    assert main(["--dsn", demo_db, "top", "--analyze", "-1", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert "--analyze must be at least 0." in data["error"]


def test_csv_keeps_newlines_in_quoted_sql_and_reads_a_bom(settings, tmp_path):
    path = tmp_path / "multi.csv"
    _write_csv(path, [{"query": "SELECT * -- all columns\nFROM events", "calls": 100, "cost_usd": 50},
                      {"query": "SELECT * -- all columns\nFROM users", "calls": 100, "cost_usd": 1}])
    path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8-sig")  # as Excel's "CSV UTF-8" saves it
    r = workload.top(settings, path=str(path))
    assert r["totals"]["groups"] == 2 and r["top"][0]["cost_usd"] == pytest.approx(50.0)
    assert r["top"][0]["sql"] == "SELECT * -- all columns\nFROM events"
    assert r["top"][0]["analysis"]["tables"] == ["events"]


def test_file_rows_with_infinite_numbers_are_skipped(settings, tmp_path):
    path = tmp_path / "inf.csv"
    path.write_text("query,calls,bytes,cost_usd\nSELECT 1,inf,1,1\nSELECT 2,1,1,nan\nSELECT 3,1,1,1\n",
                    encoding="utf-8")
    r = workload.top(settings, path=str(path))
    assert r["totals"]["queries"] == 1 and any("2 row(s) skipped" in n for n in r["notes"])


def test_postgres_parameters_that_cannot_be_tokenized_do_not_crash(settings, tmp_path):
    path = tmp_path / "pg.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"query": "SELECT * FROM t WHERE a IN ($1,$2)", "calls": 100},
        {"query": "SELECT g FROM generate_series($3,$4) g", "calls": 50},
        {"query": "SELECT a FROM t WHERE b = $1", "calls": 10},
    ]), encoding="utf-8")
    r = workload.top(settings, path=str(path), dialect="postgres")
    assert r["status"] in ("ok", "warn") and r["totals"]["groups"] == 3
    assert r["top"][0]["analysis"]["kind"] == "block" and "Could not parse" in r["top"][0]["analysis"]["reason"]
    assert r["top"][2]["analysis"]["kind"] == "read"


# -- receipts source ------------------------------------------------------------------------------
def test_receipts_source_counts_full_runs(settings):
    for day in ("2025-01-01", "2025-01-02", "2025-01-03"):
        assert core.run(settings, f"select event_type, count(*) from events where date(ts) = '{day}' group by 1")[
            "status"] == "ok"
    core.preflight(settings, "select count(*) from users")  # a preflight is not a run
    r = workload.top(settings)  # auto on DuckDB -> receipts
    assert r["source"] == "receipts" and r["rank_by"] == "runtime"
    assert r["totals"]["groups"] == 1 and r["top"][0]["calls"] == 3 and r["top"][0]["variants"] == 3
    assert r["top"][0]["analysis"]["fix"]["same_result"]
    assert workload.top(settings, days=7)["totals"]["calls"] == 3


def test_empty_history(settings):
    r = workload.top(settings, source="receipts")
    assert r["status"] == "ok" and r["top"] == [] and "No queries" in r["next_step"]


# -- BigQuery JOBS_BY_PROJECT (fake client) ---------------------------------------------------------
class FakeResult(list):
    def __init__(self, rows, names):
        super().__init__(rows)
        self.schema = [SimpleNamespace(name=n) for n in names]


class FakeJob:
    def __init__(self, processed, rows=(), names=()):
        self.total_bytes_processed = processed
        self.total_bytes_billed = processed
        self.referenced_tables = [object()]
        self._rows, self._names = list(rows), list(names)

    def result(self, timeout=None, page_size=None):
        return FakeResult(self._rows, self._names)


HISTORY_COLUMNS = ["gv_hash", "gv_query", "gv_calls", "gv_variants", "gv_bytes", "gv_bytes_min", "gv_bytes_max",
                   "gv_runtime_s", "gv_slot_ms"]


class FakeBigQuery:
    def __init__(self, history_bytes=20 * 1024**2):
        self.calls = []
        self.history_bytes = history_bytes
        self.rows = [
            ("h1", "SELECT country, COUNT(*) AS n FROM ds.events WHERE DATE(ts) = '2026-10-01' GROUP BY country",
             700, 7, 700 * TIB // 10, TIB // 10, TIB // 10, 1400.0, 9_000_000),
            ("h2", "SELECT country, COUNT(*) AS n FROM ds.events WHERE DATE(ts) = '2026-10-02' GROUP BY country",
             70, 1, 70 * TIB // 10, TIB // 10, TIB // 10, 140.0, 900_000),  # same shape, other engine hash
            ("h3", "SELECT * FROM ds.users", 2, 1, 2 * TIB // 100, TIB // 100, TIB // 100, 4.0, 1000),
        ]

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        if "INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql:
            if job_config.get("dry_run"):
                return FakeJob(self.history_bytes)
            return FakeJob(self.history_bytes, self.rows, HISTORY_COLUMNS)
        if "DATE(" in sql.upper() or "CAST(" in sql.upper():
            return FakeJob(TIB // 10)  # the whole table: no pruning
        if "ts >=" in sql:
            return FakeJob(TIB // 1000)  # one day partition
        return FakeJob(TIB // 100)

    def get_table(self, ref):
        if "events" in str(ref):
            return SimpleNamespace(num_rows=10**9, num_bytes=TIB // 10, table_type="TABLE",
                                   schema=[SimpleNamespace(name="country", field_type="STRING"),
                                           SimpleNamespace(name="ts", field_type="TIMESTAMP")],
                                   time_partitioning=SimpleNamespace(field="ts"), range_partitioning=None,
                                   clustering_fields=None, require_partition_filter=False)
        raise KeyError(ref)


def _patch_engine(monkeypatch, factory):
    monkeypatch.setattr(core, "get_engine", lambda conn, read_only=True: factory(conn))


@pytest.fixture
def bq_settings(state, tmp_path):
    (tmp_path / "gulliver.toml").write_text('[connections.bq]\nengine = "bigquery"\nproject = "p"\n'
                                            'location = "EU"\n')
    return load_settings()


def test_bigquery_jobs_ranked_with_exact_priced_fix(bq_settings, monkeypatch):
    client = FakeBigQuery()
    _patch_engine(monkeypatch, lambda conn: BigQueryEngine(conn, _client=client))
    r = workload.top(bq_settings, days=7)
    assert r["source"] == "bigquery_jobs" and r["status"] == "warn"
    history = [c for c in client.calls if "JOBS_BY_PROJECT" in c[0]]
    assert [bool(c[1].get("dry_run")) for c in history] == [True, False]  # estimated + gated, then run
    assert "`region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in history[0][0]
    assert "INTERVAL 7 DAY" in history[0][0] and "'SCRIPT'" in history[0][0]
    assert "maximum_bytes_billed" in history[1][1]  # the engine-side cap is on
    first = r["top"][0]
    assert first["calls"] == 770 and first["variants"] == 8  # two engine hashes merged into one shape
    assert first["cost_usd"] == pytest.approx(77 * 6.25 * 1.0, rel=1e-6)
    saving = first["analysis"]["saving"]
    assert saving["accuracy"] == "exact" and saving["scan_saved_pct"] == pytest.approx(99.0)
    month = first["cost_usd"] * 30 / 7
    assert saving["per_month_usd"] == pytest.approx(0.99 * month, rel=1e-3)
    assert "ts >=" in first["analysis"]["fix"]["fixed_sql"]
    assert "Biggest priced fix: #1" in r["next_step"] and "exact" in r["next_step"]
    assert r["materialize"] and r["materialize"][0]["fingerprint"] == first["fingerprint"]
    assert any("overlap" in n for n in r["materialize"][0]["notes"])  # never add the two savings up
    assert r["history_query_cost_usd"] > 0
    spent = [json.loads(line) for line in (bq_settings.state_dir / "ledger.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in spent] == ["top"]  # the history query is in the spend ledger


def test_bigquery_history_query_is_budget_gated(bq_settings, monkeypatch):
    client = FakeBigQuery(history_bytes=2 * TIB)  # 12.50 USD: over the 1 USD per-query limit
    _patch_engine(monkeypatch, lambda conn: BigQueryEngine(conn, _client=client))
    r = workload.top(bq_settings)
    assert r["status"] == "block" and "over budget" in r["next_step"]
    assert all(c[1].get("dry_run") for c in client.calls)  # nothing was executed


def test_bigquery_history_needs_approval_over_the_auto_limit(bq_settings, monkeypatch):
    client = FakeBigQuery(history_bytes=TIB // 10)  # 0.61 USD: under the hard limit, over auto-approve
    _patch_engine(monkeypatch, lambda conn: BigQueryEngine(conn, _client=client))
    r = workload.top(bq_settings)
    assert r["status"] == "needs_approval" and r["receipt_id"]
    core.approve(bq_settings, r["receipt_id"])  # the human step
    assert workload.top(bq_settings)["status"] == "warn"


# -- Snowflake QUERY_HISTORY (fake connection) -------------------------------------------------------
SNOW_COLUMNS = ["GV_HASH", "GV_QUERY", "GV_CALLS", "GV_VARIANTS", "GV_BYTES", "GV_BYTES_MIN", "GV_BYTES_MAX",
                "GV_RUNTIME_S", "GV_CREDITS", "GV_CREDITS_EST"]


class FakeSnowCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows, self.sfqid = conn, None, [], "q1"

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        self.description, self._rows = None, []
        if self.conn.deny_all and "ACCOUNT_USAGE" in sql:
            raise RuntimeError("Object 'SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY' does not exist or not authorized.")
        if self.conn.deny_attribution and "QUERY_ATTRIBUTION_HISTORY" in sql:
            raise RuntimeError("Object 'QUERY_ATTRIBUTION_HISTORY' does not exist or not authorized.")
        if sql.startswith("EXPLAIN"):
            self.description = [("plan",)]
            self._rows = [(json.dumps({"GlobalStats": {"partitionsTotal": 10, "partitionsAssigned": 10,
                                                       "bytesAssigned": 1000}}),)]
        elif "QUERY_HISTORY" in sql and "BY_SESSION" not in sql:
            self.description = [(c,) for c in SNOW_COLUMNS]
            credits = None if "QUERY_ATTRIBUTION_HISTORY" not in sql else 2.0
            self._rows = [("p1", "select region, sum(amount) from sales where day = '2026-10-01' group by 1",
                           300, 30, 300_000, 1000, 1000, 600.0, credits, 4.0)]

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def close(self):
        pass


class FakeSnowConn:
    def __init__(self, deny_attribution=False, deny_all=False):
        self.sql, self.deny_attribution, self.deny_all = [], deny_attribution, deny_all

    def cursor(self):
        return FakeSnowCursor(self)

    def close(self):
        pass


@pytest.fixture
def sf_settings(state, tmp_path):
    (tmp_path / "gulliver.toml").write_text('[connections.sf]\nengine = "snowflake"\naccount = "a"\n'
                                            'credit_usd = 2.0\n')
    return load_settings()


def test_snowflake_history_with_attribution(sf_settings, monkeypatch):
    fake = FakeSnowConn()
    _patch_engine(monkeypatch, lambda conn: SnowflakeEngine(conn, _conn=fake))
    r = workload.top(sf_settings, days=14)
    assert r["source"] == "snowflake_history"
    history = [s for s in fake.sql if "ACCOUNT_USAGE.QUERY_HISTORY" in s]
    assert history[0].startswith("EXPLAIN") and not history[1].startswith("EXPLAIN")  # gated, then run
    assert "query_parameterized_hash" in history[1] and "DATEADD('day', -14" in history[1]
    first = r["top"][0]
    assert first["cost_usd"] == pytest.approx(4.0)  # 2 attributed credits x 2 USD
    assert first["calls"] == 300 and first["variants"] == 30
    assert r["cost_basis"] == "credits attributed per query"


def test_snowflake_history_sql_counts_unattributed_runs_with_the_estimate():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA hist")
    con.execute("CREATE TABLE hist.qh AS SELECT 'q' || i AS query_id, 'p1' AS query_parameterized_hash, "
                "'select 1' AS query_text, 'h1' AS query_hash, 100 AS bytes_scanned, 3600000 AS execution_time, "
                "'Medium' AS warehouse_size, now() AS start_time, 'SUCCESS' AS execution_status FROM range(10) t(i)")
    con.execute("CREATE TABLE hist.qa AS SELECT 'q0' AS query_id, 4.0 AS credits_attributed_compute")
    sql = (workload.snowflake_history_sql(7)
           .replace("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", "hist.qh")
           .replace("SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY", "hist.qa")
           .replace("DATEADD('day', -7, CURRENT_TIMESTAMP())", "now() - INTERVAL 7 DAY"))
    row = con.execute(f"SELECT gv_credits, gv_attributed, gv_credits_est FROM ({sql})").fetchone()
    assert row == (40.0, 1, 40.0)  # 1 attributed run (4 credits) + 9 estimated runs (4 credits each)


def test_snowflake_partly_attributed_groups_are_labeled(sf_settings, monkeypatch):
    rows = [{"GV_HASH": "p1", "GV_QUERY": "select 1", "GV_CALLS": 10, "GV_CREDITS": 40.0, "GV_ATTRIBUTED": 1,
             "GV_CREDITS_EST": 40.0}]
    monkeypatch.setattr(workload, "_gated_run", lambda *a, **k: {"rows": rows, "cost_usd": 0})
    history = workload.read_snowflake_history(sf_settings, "sf", SimpleNamespace(credit_usd=2.0), 7)
    assert history.entries[0].cost_usd == pytest.approx(80.0)
    assert "where available" in history.cost_basis
    rows[0]["GV_ATTRIBUTED"] = 10
    history = workload.read_snowflake_history(sf_settings, "sf", SimpleNamespace(credit_usd=2.0), 7)
    assert history.cost_basis == "credits attributed per query"


def test_snowflake_falls_back_without_attribution(sf_settings, monkeypatch):
    fake = FakeSnowConn(deny_attribution=True)
    _patch_engine(monkeypatch, lambda conn: SnowflakeEngine(conn, _conn=fake))
    r = workload.top(sf_settings)
    assert r["top"][0]["cost_usd"] == pytest.approx(8.0)  # 4 estimated credits x 2 USD
    assert any("QUERY_ATTRIBUTION_HISTORY was not readable" in n for n in r["notes"])


def test_snowflake_without_privileges_explains_the_role(sf_settings, monkeypatch):
    _patch_engine(monkeypatch, lambda conn: SnowflakeEngine(conn, _conn=FakeSnowConn(deny_all=True)))
    r = workload.top(sf_settings)
    assert r["status"] == "error" and "GOVERNANCE_VIEWER" in r["next_step"]


# -- pg_stat_statements (fake connection; live test below) ----------------------------------------------
class FakePgCursor:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        self.description, self._rows = None, []
        if "pg_stat_statements" in sql and self.conn.missing:
            raise RuntimeError('relation "pg_stat_statements" does not exist')
        if "pg_stat_statements_info" in sql and self.conn.old:
            raise RuntimeError('relation "pg_stat_statements_info" does not exist')
        if sql.startswith("EXPLAIN"):
            self.description = [SimpleNamespace(name="QUERY PLAN")]
            self._rows = [([{"Plan": {"Node Type": "Result", "Plan Rows": 1, "Plan Width": 8}}],)]
        elif "pg_stat_statements_info" in sql:
            self.description = [SimpleNamespace(name="gv_since")]
            self._rows = [(self.conn.since,)]
        elif "FROM pg_stat_statements" in sql:
            self.description = [SimpleNamespace(name=n) for n in ("gv_hash", "gv_query", "gv_calls", "gv_ms",
                                                                   "gv_blocks")]
            self._rows = [(11, "select * from orders where id = $1", 5000, 90_000.0, 20),
                          (12, "<insufficient privilege>", 9, 1.0, 1),
                          (13, "select count(*) from orders where status = $1", 10, 120_000.0, 9000),
                          *self.conn.extra]

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows


class FakePgConn:
    def __init__(self, missing=False, old=False, since="2026-10-19 00:00:00+00:00", extra=()):
        self.sql, self.missing, self.old, self.since, self.extra = [], missing, old, since, list(extra)

    def cursor(self):
        return FakePgCursor(self)

    def close(self):
        pass


def _pg_settings(state):
    return load_settings(dsn="postgresql://u@localhost/db")


def test_pg_stat_statements_ranked_by_runtime(state, monkeypatch):
    fake = FakePgConn()
    _patch_engine(monkeypatch, lambda conn: PostgresEngine(conn, _conn=fake))
    r = workload.top(_pg_settings(state))
    assert r["source"] == "pg_stat_statements" and r["rank_by"] == "runtime"
    assert [t["runtime_s"] for t in r["top"]] == [120.0, 90.0]
    assert r["top"][0]["bytes"] == 9000 * 8192
    assert r["window_days"] and r["window_days"] > 1  # from pg_stat_statements_info.stats_reset
    assert any("pg_read_all_stats" in n for n in r["notes"])
    assert any(s.startswith("EXPLAIN") and "pg_stat_statements" in s for s in fake.sql)  # gated first


def test_pg_stat_statements_without_reset_time_has_no_projection(state, monkeypatch):
    _patch_engine(monkeypatch, lambda conn: PostgresEngine(conn, _conn=FakePgConn(old=True)))
    r = workload.top(_pg_settings(state))
    assert r["window_days"] is None and "calls_per_day" not in r["top"][0]
    assert r["materialize"] == []


# Gulliver's own statements as pg_stat_statements records them, and other statements that are not workload
PG_NOT_WORKLOAD = [
    (21, "EXPLAIN (FORMAT JSON) SELECT s.queryid AS gv_hash FROM pg_stat_statements AS s", 3, 500_000.0, 1),
    (22, "SELECT i.stats_reset AS gv_since FROM pg_stat_statements_info AS i", 3, 400_000.0, 1),
    (23, "SET statement_timeout = 900000", 9, 300_000.0, 0),
    (24, "SELECT set_config($1, $2, $3)", 9, 300_000.0, 0),
    (25, "SELECT n.nspname, c.relname, count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
         "GROUP BY 1, 2", 5000, 300_000.0, 10),
    (26, "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = $1", 9, 300_000.0, 1),
    (27, "SELECT pg_relation_size($1)", 9, 300_000.0, 0),
]


def test_pg_stat_statements_leaves_out_gulliver_and_catalog_statements(state, monkeypatch):
    extra = [*PG_NOT_WORKLOAD, (31, "SELECT * FROM ev WHERE a IN ($1,$2)", 50, 200_000.0, 5)]
    _patch_engine(monkeypatch, lambda conn: PostgresEngine(conn, _conn=FakePgConn(extra=extra)))
    r = workload.top(_pg_settings(state))
    assert r["status"] in ("ok", "warn")
    sqls = [t["sql"] for t in r["top"]]
    assert sqls == ["SELECT * FROM ev WHERE a IN ($1,$2)", "select count(*) from orders where status = $1",
                    "select * from orders where id = $1"]
    # Postgres keeps `$1,$2` unspaced; sqlglot cannot tokenize it: the group is reported, not a crash
    assert r["top"][0]["analysis"]["kind"] == "block"
    assert all("pg_class" not in m["sql"] for m in r["materialize"])
    assert any("7 statement(s) left out" in n for n in r["notes"])


def test_pg_stat_statements_short_window_has_no_projection(state, monkeypatch):
    from gulliver.store import now_utc

    since = (now_utc() - workload.timedelta(minutes=2)).isoformat()
    many = [(41, "select region, count(*) from sales group by 1", 5000, 900_000.0, 100)]
    _patch_engine(monkeypatch, lambda conn: PostgresEngine(conn, _conn=FakePgConn(since=since, extra=many)))
    r = workload.top(_pg_settings(state))
    assert 0 < r["window_days"] < 1
    assert "cost_per_month_usd" not in r["totals"] and r["materialize"] == []
    assert all("runtime_s_per_month" not in (t.get("analysis", {}).get("saving") or {}) for t in r["top"])
    assert any("too short to project" in n for n in r["notes"])
    assert not any("0 day(s)" in n for n in r["notes"])


def test_pg_stat_statements_missing_is_a_clear_error(state, monkeypatch):
    _patch_engine(monkeypatch, lambda conn: PostgresEngine(conn, _conn=FakePgConn(missing=True)))
    r = workload.top(_pg_settings(state))
    assert r["status"] == "error" and "shared_preload_libraries" in r["next_step"]


PG_DSN = os.environ.get("GULLIVER_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="set GULLIVER_TEST_PG_DSN to run live Postgres tests")
def test_pg_stat_statements_live(state):
    r = workload.top(load_settings(dsn=PG_DSN), source="pg_stat_statements")
    if r["status"] == "error":  # the extension is not preloaded on this server: the error must say how
        assert "pg_stat_statements" in r["error"] and "shared_preload_libraries" in r["next_step"]
        return
    assert r["status"] in ("ok", "warn") and r["rank_by"] == "runtime"


# -- CLI and MCP ------------------------------------------------------------------------------------
def test_cli_top_json_and_text(demo_db, state, workload_csv, capsys):
    assert main(["--dsn", demo_db, "top", "--source", f"csv:{workload_csv}", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["action"] == "top" and data["top"][0]["analysis"]["fix"]["same_result"]
    assert main(["--dsn", demo_db, "top", "--file", workload_csv, "--limit", "2"]) == 0
    text = capsys.readouterr().out
    assert "ranked by cost" in text and "-- #1 GV005: same rows" in text and "next:" in text
    assert main(["--dsn", demo_db, "top", "--source", "bigquery_jobs"]) == 1


def test_cli_top_output_file(demo_db, state, workload_csv, tmp_path, capsys):
    """`gulliver top -o PATH` writes the printed report as UTF-8 and confirms the path."""
    assert main(["--dsn", demo_db, "top", "--source", f"csv:{workload_csv}", "--limit", "2"]) == 0
    expected = capsys.readouterr().out
    out = tmp_path / "top.md"
    assert main(["--dsn", demo_db, "top", "--source", f"csv:{workload_csv}", "--limit", "2",
                 "-o", str(out)]) == 0
    assert capsys.readouterr().out.strip() == f"Wrote {out}"
    assert out.read_text(encoding="utf-8") == expected
    assert "ranked by cost" in expected and "next:" in expected


def test_mcp_top_tool(demo_db, state, workload_csv, monkeypatch):
    import asyncio

    from gulliver import mcp_server

    monkeypatch.setitem(mcp_server._ARGS, "dsn", demo_db)
    server = mcp_server.build_server()
    result = asyncio.run(server.call_tool("gulliver_top", {"source": "file", "path": workload_csv, "limit": 2}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "GV005" in text and "fingerprint" in text
