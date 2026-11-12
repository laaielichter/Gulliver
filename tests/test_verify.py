"""gulliver fix --verify: every rewrite is tested on generated edge-case data."""

from __future__ import annotations

import asyncio
import json
import time

import duckdb
import pytest

from gulliver import core, fixer, verify
from gulliver.cli import main
from gulliver.config import load_settings
from gulliver.fixer import fix
from gulliver.mcp_server import build_server


def meta(**cols: str) -> dict:
    return {"t": {"columns": [{"name": n, "type": ty} for n, ty in cols.items()]}}


def by_rule(result: dict, rule: str) -> dict:
    return next(c for c in result["changes"] + result.get("dropped", []) if c["rule"] == rule)


def rows_in(example: dict) -> int:
    return sum(len(t["rows"]) for t in example["tables"].values())


# -- the design's acceptance cases -----------------------------------------------------------
def test_not_in_null_counterexample_is_found_and_shrunk():
    sql = "select e.id from e where e.user_id not in (select user_id from banned)"
    out = verify.verify_rewrite(sql, "duckdb", "all", cases=100)
    change = by_rule(out, "GV012")
    assert change["guarantee"] == "nulls"                      # the label stays what it was
    assert change["verified_on_generated_data"]["counterexamples"] > 0
    example = change["counterexample"]
    assert rows_in(example) <= 3
    values = [v for t in example["tables"].values() for r in t["rows"] for v in r.values()]
    assert None in values                                     # the difference is a NULL
    assert example["original_result"] != example["fixed_result"]
    # the setup SQL reproduces it in a fresh DuckDB
    con = duckdb.connect()
    con.execute(example["setup_sql"])
    assert con.execute(sql).fetchall() != con.execute(out["fixed_sql"].replace("LIMIT 1000", "")).fetchall()


def test_date_on_timestamp_has_no_counterexample():
    out = verify.verify_rewrite("select a, count(*) from t where date(ts) = '2024-02-29' group by a", "duckdb",
                                table_info=meta(ts="TIMESTAMP", a="INTEGER"), cases=200)
    change = by_rule(out, "GV005")
    assert change["guarantee"] == "same"
    assert change["verified_on_generated_data"]["cases"] == 200
    assert change["verified_on_generated_data"]["counterexamples"] == 0
    assert "not a proof" in change["verified_on_generated_data"]["meaning"]
    assert "dropped" not in out and out["verify"]["status"] == "ok" and out["verify"]["schema_source"] == "metadata"


@pytest.mark.parametrize("predicate", ["date(ts) >= '2024-01-01'", "year(ts) = 2024", "date(ts) <= '2024-12-31'",
                                       "date_trunc('month', ts) = '2024-02-01'",
                                       "date(ts) between '2023-12-31' and '2024-01-01'"])
@pytest.mark.parametrize("ctype", ["TIMESTAMP", "DATE"])
def test_same_rewrites_hold_on_generated_data(predicate, ctype):
    out = verify.verify_rewrite(f"select a from t where {predicate}", "duckdb",
                                table_info=meta(ts=ctype, a="INTEGER"), cases=60)
    assert out["changes"] and "dropped" not in out
    assert all(c["verified_on_generated_data"]["counterexamples"] == 0 for c in out["changes"])


def test_timestamptz_daylight_saving_counterexample():
    # Atlantic/Azores repeats the hour after midnight on 2024-10-27, so a day-range literal misses an hour.
    # (In Europe/Amsterdam the clock changes at 02:00/03:00, so day ranges are not affected there.)
    original = "select count(*) from t where date(tz) = '2024-10-27'"
    fixed = fixer.rewrite(original, "duckdb", mode="all")["fixed_sql"]
    report = verify.check(original, [fixed], "duckdb", meta(tz="TIMESTAMPTZ"), cases=150,
                          timezone="Atlantic/Azores")
    result = report["results"][0]
    assert report["status"] == "warn" and result["counterexamples"] > 0
    example = result["counterexample"]
    assert example["setup_sql"].startswith("SET TimeZone = 'Atlantic/Azores';")
    assert rows_in(example) == 1
    assert verify.check(original, [fixed], "duckdb", meta(tz="TIMESTAMP"), cases=150)["results"][0][
        "counterexamples"] == 0


def test_union_to_union_all_duplicate_counterexample():
    out = verify.verify_rewrite("select a from t1 union select a from t2", "duckdb", "all", cases=50)
    change = by_rule(out, "GV009")
    assert change["guarantee"] == "changes" and change["verified_on_generated_data"]["counterexamples"] > 0
    example = change["counterexample"]
    assert rows_in(example) == 2                               # one value, twice
    assert len(example["fixed_result"]) == len(example["original_result"]) + 1


def test_same_seed_same_result():
    sql = "select e.id from e where e.user_id not in (select user_id from banned) and date(e.ts) = '2024-01-01'"

    def run(seed):
        out = verify.verify_rewrite(sql, "duckdb", "all", cases=60, seed=seed)
        out["verify"].pop("elapsed_s")
        return out

    assert json.dumps(run(7), sort_keys=True) == json.dumps(run(7), sort_keys=True)


# -- labels and the safety net ----------------------------------------------------------------
def test_unknown_type_is_tried_in_every_type_that_fits_and_stays_assumes():
    out = verify.verify_rewrite("select a from t where date(ts) = '2024-01-01'", "duckdb", "all", cases=200)
    change = by_rule(out, "GV005")
    assert change["guarantee"] == "assumes" and out["same_result"] is False
    tried = {b["types"]["t.ts"]: b for b in change["verified_on_generated_data"]["by_type"]}
    assert {"DATE", "TIMESTAMP", "TIMESTAMPTZ", "VARCHAR"} <= set(tried)
    assert tried["DATE"]["counterexamples"] == tried["TIMESTAMP"]["counterexamples"] == 0
    assert tried["VARCHAR"]["counterexamples"] > 0             # text compares as text: why it is "assumes"
    assert change["counterexample"]["types"]["t.ts"] == "VARCHAR"


def test_assumes_without_counterexample_is_not_promoted():
    out = verify.verify_rewrite("select a from t where year(ts) = 2024", "duckdb", "all", cases=40)
    change = by_rule(out, "GV005")
    assert change["verified_on_generated_data"]["counterexamples"] == 0
    assert change["guarantee"] == "assumes" and out["same_result"] is False


def test_counterexample_against_same_drops_the_change(monkeypatch):
    monkeypatch.setattr(fixer, "safe_date_type", lambda ctype: True)  # simulate a fixer bug: tz called safe
    sql = "select count(*) from t where date(tz) = '2024-10-27'"
    info = meta(tz="TIMESTAMP WITH TIME ZONE")
    assert fixer.rewrite(sql, "duckdb", table_info=info)["changes"][0]["guarantee"] == "same"
    out = verify.verify_rewrite(sql, "duckdb", table_info=info, cases=150, timezone="Atlantic/Azores")
    assert out["changes"] == [] and [c["rule"] for c in out["dropped"]] == ["GV005"]
    assert out["dropped"][0]["counterexample"]["setup_sql"]
    assert "tz >=" not in out["fixed_sql"].lower() and "CAST(tz AS DATE)" in out["fixed_sql"]


def test_fix_verify_drop_returns_warn(monkeypatch, state, tmp_path):
    path = str(tmp_path / "tz.duckdb")
    con = duckdb.connect(path)
    con.execute("create table t (tz timestamptz)")
    con.close()
    monkeypatch.setattr(fixer, "safe_date_type", lambda ctype: True)
    out = fix(load_settings(dsn=path), "select count(*) from t where date(tz) = '2024-10-27'", verify=True,
              cases=150, timezone="Atlantic/Azores")
    assert out["status"] == "warn" and [c["rule"] for c in out["dropped"]] == ["GV005"]
    assert "Gulliver bug" in out["next_step"] and out["changes"] == []


def test_untestable_changes_say_why():
    out = verify.verify_rewrite("select count(distinct a) from t", "duckdb", "all", cases=10)
    assert "exact on tiny tables" in by_rule(out, "GV007")["verified_on_generated_data"]["skipped"]


@pytest.mark.parametrize("sql, reason", [
    ("select a from t where date(ts) = '2024-01-01' and random() < 0.5", "random"),
    ("select a from read_parquet('x.parquet') where date(ts) = '2024-01-01'", "table function"),
    ("select row_number() over (order by a) from t where date(ts) = '2024-01-01'", "tied rows"),
    ("select * from (select a from t where date(ts) = '2024-01-01' limit 3) s", "LIMIT inside"),
    ("select string_agg(a, ',') from t where date(ts) = '2024-01-01'", "row order"),
])
def test_queries_that_cannot_be_tested_fairly_are_skipped(sql, reason):
    out = verify.verify_rewrite(sql, "duckdb", "all", cases=10)
    assert out["verify"]["status"] == "skipped" and reason in out["verify"]["reason"]
    assert all(c["guarantee"] != "same" or "dropped" not in out for c in out["changes"])


def test_outer_limit_is_compared_without_it():
    out = verify.verify_rewrite("select a from t where date(ts) = '2024-01-01' order by a limit 5", "duckdb",
                                table_info=meta(ts="TIMESTAMP", a="INTEGER"), cases=30)
    assert out["verify"]["status"] == "ok"
    assert any("outer LIMIT" in n for n in out["verify"]["notes"])


def test_private_database_cannot_read_files(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("x\n1\n")
    runner = verify.Runner(verify.Schema({}), deadline=1e18)
    with pytest.raises(duckdb.Error):
        runner.con("UTC").execute(f"select * from read_csv('{secret}')")
    with pytest.raises(duckdb.Error):
        runner.con("UTC").execute("SET enable_external_access = true")
    runner.close()


def test_other_dialects_are_translated_or_skipped():
    out = verify.verify_rewrite("SELECT a FROM `proj.ds.t` WHERE DATE(ts) = '2024-01-01'", "bigquery",
                                table_info={"`proj.ds.t`": {"columns": [{"name": "ts", "type": "TIMESTAMP"},
                                                                        {"name": "a", "type": "INT64"}]}},
                                cases=40)
    assert by_rule(out, "GV005")["verified_on_generated_data"]["counterexamples"] == 0
    assert any("Translated from bigquery" in n for n in out["verify"]["notes"])
    out = verify.verify_rewrite("SELECT a FROM t WHERE my_udf(a) = 1 AND DATE(ts) = '2024-01-01'", "bigquery",
                                "all", cases=10)
    assert out["verify"]["status"] == "skipped" and "translat" in out["verify"]["reason"]
    out = verify.verify_rewrite("select a from t where date_trunc('day', ts) = '2024-01-01'", "postgres",
                                table_info=meta(ts="timestamp without time zone", a="integer"), cases=40)
    assert by_rule(out, "GV005")["verified_on_generated_data"]["counterexamples"] == 0


def test_write_statement_is_verified_on_its_select():
    out = verify.verify_rewrite("create table x as select a from t where date(ts) = '2024-01-01'", "duckdb",
                                table_info=meta(ts="TIMESTAMP", a="INTEGER"), cases=20)
    assert out["fixed_sql"].startswith("CREATE TABLE x")
    assert by_rule(out, "GV005")["verified_on_generated_data"]["counterexamples"] == 0


def test_columns_the_inference_misses_are_added():
    # `x` is unqualified next to a join: the binder tells which column is missing
    sql = "select e.id from e join u on e.uid = u.id where x > 3 and date(e.ts) = '2024-01-01'"
    out = verify.verify_rewrite(sql, "duckdb", "all", cases=20)
    assert out["verify"]["status"] in ("ok", "warn"), out["verify"]


# -- wiring -----------------------------------------------------------------------------------
def test_fix_verify_with_connection(settings):
    out = fix(settings, "select event_type, count(*) from events where date(ts) = '2025-01-02' group by 1",
              verify=True, cases=30)
    assert out["status"] == "ok" and out["same_result"]
    v = out["changes"][0]["verified_on_generated_data"]
    assert v["cases"] == 30 and v["counterexamples"] == 0
    assert out["verify"]["schema_source"] == "metadata" and "not a proof" in out["next_step"]


def test_preflight_autofix_carries_a_verified_block(settings):
    pre = core.preflight(settings, "select event_type, count(*) from events where date(ts) = '2025-01-02' group by 1")
    verified = pre["autofix"]["verified"]
    assert verified["status"] == "ok" and verified["cases"] > 0 and verified["semantics"] == "DuckDB semantics"


def test_cli_fix_verify(capsys):
    assert main(["fix", "--dialect", "duckdb", "--mode", "all", "--verify", "--cases", "50", "--json",
                 "select e.id from e where e.user_id not in (select user_id from banned)"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert by_rule(data, "GV012")["counterexample"] and data["verify"]["seed"] == 0
    assert main(["fix", "--dialect", "duckdb", "--mode", "all", "--verify", "--cases", "50", "--seed", "3",
                 "select a from t1 union select a from t2"]) == 0
    text = capsys.readouterr().out
    assert "tested: rows differ" in text and "repro:" in text and "INSERT INTO" in text


def test_mcp_fix_verify(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    server = build_server()
    result = asyncio.run(server.call_tool("gulliver_fix", {"sql": "select count(*) from events where year(ts) = 2025",
                                                           "verify": True, "cases": 20}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "verified_on_generated_data" in text and "DuckDB semantics" in text


# -- regressions ------------------------------------------------------------------------------
def test_a_failing_query_does_not_abort_the_queries_after_it():
    # an earlier query that overflows must not turn a correct `same` rewrite after it into an error
    sql = ("select id + 1 as n from t where not (date(a) between '2024-01-01' and '2024-01-31') "
           "union all select 0 from u where date(b) = '2024-01-15'")
    for seed in (0, 1, 2):
        out = verify.verify_rewrite(sql, "duckdb", "all", table_info={"u": {"columns": [
            {"name": "b", "type": "TIMESTAMP"}]}}, seed=seed)
        assert "dropped" not in out, seed
        assert by_rule(out, "GV005")["guarantee"] in ("same", "assumes")
    # when only the original fails, that is a difference
    out = verify.check("select a + 1 as n from t", ["select a::bigint + 1 as n from t"])
    assert out["status"] == "warn" and out["results"][0]["counterexamples"] > 0
    assert out["results"][0]["counterexample"]["fixed_result"] != out["results"][0]["counterexample"][
        "original_result"]


def test_cases_where_both_queries_fail_are_not_counted():
    sql = ("select id, cast(code as integer) as c from t where date(ts) between '2024-01-01' and '2024-01-31' "
           "and code like '%'")
    out = verify.verify_rewrite(sql, "duckdb", "all")
    v = by_rule(out, "GV005")["verified_on_generated_data"]
    assert v["both_failed"] > 0 and v["cases"] + v["both_failed"] == 200
    assert f"{v['cases']} generated cases" in v["meaning"] and "both failed" in v["meaning"]
    assert out["verify"]["cases"] < 200 and out["verify"]["original_failed"] > 0


@pytest.mark.parametrize("dialect, ctype, idtype", [("bigquery", "TIMESTAMP", "INT64"),
                                                    ("snowflake", "TIMESTAMP_NTZ", "INTEGER"),
                                                    ("postgres", "timestamp without time zone", "integer")])
def test_infinity_is_only_generated_where_the_engine_agrees_on_it(dialect, ctype, idtype):
    sql = "select id from t where extract(year from ts) >= 2024"
    info = {"t": {"columns": [{"name": "id", "type": idtype}, {"name": "ts", "type": ctype}]}}
    assert fixer.rewrite(sql, dialect, "safe", table_info=info)["changes"][0]["guarantee"] == "same"
    out = verify.verify_rewrite(sql, dialect, "safe", table_info=info, cases=100)
    assert "dropped" not in out and by_rule(out, "GV005")["verified_on_generated_data"]["counterexamples"] == 0
    assert [c["rule"] for c in verify.quick_rewrite(sql, dialect, info)["changes"]] == ["GV005"]


def test_columns_are_followed_through_ctes_and_subqueries():
    for sql in ("with x as (select ts from events) select count(*) from x where date(ts) between "
                "'2024-01-01' and '2024-01-31'",
                "select count(*) from (select * from events) s where date(s.ts) between '2024-01-01' and '2024-01-31'",
                "with x as (select ts as t2 from events) select count(*) from x where date(t2) between "
                "'2024-01-01' and '2024-01-31'"):
        out = verify.verify_rewrite(sql, "duckdb", "all")
        assert out["verify"]["schema"]["events"]["ts"].startswith("TIMESTAMP | DATE"), sql
        v = by_rule(out, "GV005")["verified_on_generated_data"]
        assert {b["types"]["events.ts"] for b in v["by_type"] if b["counterexamples"]} == {"VARCHAR"}, sql
    out = verify.verify_rewrite("with x as (select ts from events) select count(*) from x where year(ts) = 2024",
                                "duckdb", "all")
    assert by_rule(out, "GV005")["verified_on_generated_data"]["cases"] > 0


@pytest.mark.parametrize("ctype", ["TINYINT", "SMALLINT", "UTINYINT", "USMALLINT"])
def test_narrow_integer_columns_do_not_lose_cases(ctype):
    out = verify.verify_rewrite("select id from t where year(ts) = 2024", "duckdb",
                                table_info=meta(id="INTEGER", flag=ctype, ts="TIMESTAMP"), cases=100)
    assert out["verify"]["invalid_cases"] == 0 and out["verify"]["cases"] == 100


def test_repro_runs_for_schema_qualified_tables():
    for sql, dialect in (("select o.id from analytics.orders o where o.cid not in (select cid from analytics.blocked)",
                          "duckdb"),
                         ("select o.id from `proj.ds.orders` o where o.cid not in (select cid from `proj.ds.blocked`)",
                          "bigquery")):
        example = by_rule(verify.verify_rewrite(sql, dialect, "all", cases=100), "GV012")["counterexample"]
        con = duckdb.connect()
        con.execute(example["setup_sql"])
        assert [list(map(str, r)) for r in con.execute(example["original_sql"]).fetchall()] == \
            example["original_result"]
        assert [list(map(str, r)) for r in con.execute(example["fixed_sql"]).fetchall()] == example["fixed_result"]


def test_a_short_time_budget_is_kept_even_when_cases_are_slow():
    joins = ", ".join(f"t {a}" for a in "abcdefg")
    chain = " and ".join(f"{x}.id <> {y}.id" for x, y in zip("abcdef", "bcdefg", strict=True))
    sql = f"select count(*) as n from {joins} where {chain}"
    started = time.monotonic()
    verify.check(sql.replace("where ", "where a.id is not null and "), [sql], "duckdb",
                 {"t": {"columns": [{"name": "id", "type": "BIGINT"}]}}, cases=40, time_budget_s=2.0)
    assert time.monotonic() - started < 3.5


@pytest.mark.parametrize("cases", ["0", "-5"])
def test_cli_fix_verify_refuses_cases_below_one(capsys, cases):
    # `cases or DEFAULT_CASES` turned 0 into 200, and a negative number reached the
    # verifier as an empty range that reported having checked one case and passed.
    sql = "select e.id from e where e.user_id not in (select user_id from banned)"
    assert main(["fix", "--dialect", "duckdb", "--mode", "all", "--verify", "--cases", cases, "--json", sql]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert cases in data["error"]
    assert data["next_step"]
    assert "verify" not in data


def test_fix_refuses_cases_below_one_without_verify_ignored(capsys):
    # --cases without --verify was ignored before and stays ignored, so the run still succeeds.
    assert main(["fix", "--dialect", "duckdb", "--cases", "0", "--json", "select a from t1 union select a from t2"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] in ("ok", "warn")


def test_fix_cases_below_one_through_the_function():
    for cases in (0, -5):
        result = fix(None, "select a from t1 union select a from t2", dialect="duckdb", verify=True, cases=cases)
        assert result["status"] == "error"
        assert str(cases) in result["error"]
