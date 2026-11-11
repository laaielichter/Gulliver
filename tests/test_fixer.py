"""gulliver_fix: every 'same' rewrite must really return the same rows."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from gulliver import core, fixer
from gulliver.cli import main
from gulliver.fixer import fix, rewrite, safe_date_type
from gulliver.mcp_server import build_server


def flat(sql: str) -> str:
    return " ".join(sql.split())


def typed(ctype: str = "TIMESTAMP", table: str = "t", column: str = "ts") -> dict:
    return {table: {"columns": [{"name": column, "type": ctype}, {"name": "a", "type": "INTEGER"},
                                {"name": "id", "type": "INTEGER"}]}}


# -- rewrites per dialect (types known) ---------------------------------------------------
@pytest.mark.parametrize("dialect, sql, ctype, expect", [
    ("duckdb", "select a from t where date(ts) = '2024-01-01'", "TIMESTAMP", "ts >= '2024-01-01' AND ts < '2024-01-02'"),
    ("duckdb", "select a from t where ts::date = '2024-01-01'", "DATE", "ts >= '2024-01-01' AND ts < '2024-01-02'"),
    ("duckdb", "select a from t where '2024-01-01' = date(ts)", "TIMESTAMP", "ts >= '2024-01-01' AND ts < '2024-01-02'"),
    ("duckdb", "select a from t where year(ts) = 2024", "TIMESTAMP", "ts >= '2024-01-01' AND ts < '2025-01-01'"),
    ("duckdb", "select a from t where date_trunc('month', ts) = '2024-12-01'", "TIMESTAMP",
     "ts >= '2024-12-01' AND ts < '2025-01-01'"),
    ("duckdb", "select a from t where date_trunc('quarter', ts) = '2024-04-01'", "TIMESTAMP",
     "ts >= '2024-04-01' AND ts < '2024-07-01'"),
    ("duckdb", "select a from t where date_trunc('quarter', ts) = '2024-10-01'", "DATE",
     "ts >= '2024-10-01' AND ts < '2025-01-01'"),
    ("duckdb", "select a from t where date(ts) > '2024-01-01'", "TIMESTAMP", "ts >= '2024-01-02'"),
    ("duckdb", "select a from t where date(ts) <= '2024-02-29'", "TIMESTAMP", "ts < '2024-03-01'"),
    ("duckdb", "select a from t where date(ts) < '2024-01-01'", "TIMESTAMP", "ts < '2024-01-01'"),
    ("bigquery", "SELECT a FROM t WHERE DATE(ts) BETWEEN '2024-01-01' AND '2024-01-31'", "TIMESTAMP",
     "ts >= '2024-01-01' AND ts < '2024-02-01'"),
    ("bigquery", "SELECT a FROM t WHERE EXTRACT(YEAR FROM ts) = 2023", "DATETIME",
     "ts >= '2023-01-01' AND ts < '2024-01-01'"),
    ("bigquery", "SELECT a FROM t WHERE DATE(ts) >= DATE '2024-01-01'", "TIMESTAMP", "ts >= '2024-01-01'"),
    ("snowflake", "select a from t where to_date(ts) = '2024-01-01'", "TIMESTAMP_NTZ",
     "ts >= '2024-01-01' AND ts < '2024-01-02'"),
    ("postgres", "select a from t where date_trunc('day', ts) = '2024-01-01'", "timestamp without time zone",
     "ts >= '2024-01-01' AND ts < '2024-01-02'"),
])
def test_range_rewrites(dialect, sql, ctype, expect):
    out = rewrite(sql, dialect, table_info=typed(ctype))
    assert expect in flat(out["fixed_sql"])
    assert out["same_result"] is True
    assert [(c["rule"], c["guarantee"]) for c in out["changes"]] == [("GV005", "same")]


@pytest.mark.parametrize("ctype, ok", [
    ("DATE", True), ("TIMESTAMP", True), ("TIMESTAMP_NS", True), ("DATETIME", True), ("TIMESTAMP_NTZ", True),
    ("timestamp without time zone", True), ("TIMESTAMP WITH TIME ZONE", False), ("TIMESTAMPTZ", False),
    ("TIMESTAMP_TZ", False), ("TIMESTAMP_LTZ", False), ("timestamp with time zone", False), ("VARCHAR", False),
    ("INTERVAL", False), ("TIME", False), ("STRING", False),
])
def test_safe_date_types(ctype, ok):
    assert safe_date_type(ctype) is ok


def test_quarter_literal_must_start_a_quarter():
    for literal in ("2024-05-01", "2024-04-02"):
        sql = f"select a from t where date_trunc('quarter', ts) = '{literal}'"
        assert rewrite(sql, "duckdb", table_info=typed("TIMESTAMP"))["changes"] == []


def test_unknown_type_is_only_offered_in_mode_all():
    sql = "select a from t where date(ts) = '2024-01-01' limit 10"
    assert rewrite(sql, "duckdb")["changes"] == []
    out = rewrite(sql, "duckdb", mode="all")
    assert [(c["rule"], c["guarantee"]) for c in out["changes"]] == [("GV005", "assumes")]
    assert out["same_result"] is False


@pytest.mark.parametrize("ctype", ["VARCHAR", "TIMESTAMP WITH TIME ZONE", "INTERVAL", "TIMESTAMP_LTZ"])
def test_unsafe_types_never_rewritten(ctype):
    for sql in ("select a from t where cast(ts as date) = '2024-01-05'", "select a from t where year(ts) = 2024"):
        for mode in ("safe", "all"):
            assert not [c for c in rewrite(sql, "duckdb", mode=mode, table_info=typed(ctype))["changes"]
                        if c["rule"] == "GV005"], (sql, ctype, mode)


def test_type_must_come_from_one_base_table():
    info = {"v": {"columns": [{"name": "ts", "type": "VARCHAR"}]}, "u": {"columns": [{"name": "ts", "type": "DATE"}]}}
    sql = "select 1 from v join u on v.id = u.id where cast(ts as date) = '2024-01-01'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"] == []  # unqualified and ambiguous
    sql = "select 1 from v join u on v.id = u.id where cast(u.ts as date) = '2024-01-01'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"][0]["guarantee"] == "same"
    sql = "select 1 from v join u on v.id = u.id where cast(v.ts as date) = '2024-01-01'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"] == []
    # a derived table can redefine the column as text: do not trust the base table's type
    sql = "select 1 from (select cast(ts as varchar) as ts from u) s where cast(ts as date) = '2024-01-01'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"] == []
    sql = "with u as (select 'x' as ts) select 1 from u where cast(ts as date) = '2024-01-01'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"] == []


@pytest.mark.parametrize("sql, dialect", [
    ("select a from t where date_trunc('month', ts) = '2024-03-15'", "duckdb"),   # never true: leave it
    ("select a from t where date(ts) = something_else", "duckdb"),                # not a literal
    ("SELECT a FROM t WHERE DATE(ts, 'Europe/Amsterdam') = '2024-01-01'", "bigquery"),  # time zone argument
    ("select a from t where date(ts) = '2024-01-01 10:00'", "duckdb"),             # not a clean day literal
    ("select a from t where date(a) = date(b)", "duckdb"),
    ("select a from t where try_to_date(ts) = '2024-01-01'", "snowflake"),         # TRY_ version: errors become NULL
    ("select a from t where year(ts) = '2024-01-01'", "duckdb"),                   # an error in the original
    ("select a from t where date_trunc('year', ts) = 2024", "duckdb"),             # an error in the original
    ("select a from t where date(ts) between symmetric '2024-02-01' and '2024-01-01'", "postgres"),
    ("select a from t where date(ts) = '9999-12-31'", "duckdb"),                   # next day is past year 9999
    ("select a from t where date(ts) between '2024-01-01' and '9999-12-31'", "duckdb"),
    ("select a from t where date_trunc('month', ts) = '9999-12-01'", "duckdb"),
    ("select a from t where year(ts) > 2023", "duckdb"),                           # DuckDB infinity: one-sided YEAR
    ("select a from t where not (year(ts) = 2024)", "duckdb"),                     # DuckDB infinity: negated YEAR
    ("select a from t where (year(ts) = 2024) is null", "duckdb"),                 # DuckDB infinity: NULL vs FALSE
    ("select a from t where coalesce(year(ts) = 2024, true)", "duckdb"),
    ("select a from t where (year(ts) = 2024) = false", "duckdb"),
    ("select a from t where case when year(ts) = 2024 then false else true end", "duckdb"),
])
def test_range_rewrites_left_alone(sql, dialect):
    for mode in ("safe", "all"):
        out = rewrite(sql, dialect, mode=mode, table_info=typed("TIMESTAMP"))
        assert [c for c in out["changes"] if c["rule"] == "GV005"] == [], (sql, mode)


def test_order_by_in_subqueries_is_never_touched():
    for sql in ("select * from (select a from big order by b desc) s limit 3",
                "with x as (select a from t order by a) select string_agg(a, ',') from x",
                "select row_number() over () from (select a from t order by a) s"):
        for mode in ("safe", "all"):
            out = rewrite(sql, "duckdb", mode=mode)
            assert "ORDER BY" in out["fixed_sql"], (sql, mode)


def test_opt_in_rewrites_only_in_mode_all():
    sql = ("select e.id, count(distinct e.user_id) from events e where e.user_id not in "
           "(select user_id from banned) group by 1 union select 1, 2")
    assert rewrite(sql, "duckdb")["changes"] == []
    out = rewrite(sql, "duckdb", mode="all")
    rules = {c["rule"]: c["guarantee"] for c in out["changes"]}
    assert rules == {"GV012": "nulls", "GV007": "changes", "GV009": "changes"}
    assert out["same_result"] is False
    assert "NOT EXISTS" in out["fixed_sql"] and "UNION ALL" in out["fixed_sql"]


def test_not_in_needs_a_qualified_outer_column():
    out = rewrite("select id from events where user_id not in (select user_id from banned)", "duckdb", mode="all")
    assert "GV012" not in [c["rule"] for c in out["changes"]]


def test_limit_added_in_mode_all():
    out = rewrite("select * from events", "duckdb", mode="all", limit=50)
    assert flat(out["fixed_sql"]).endswith("LIMIT 50")
    assert rewrite("select count(*) from events", "duckdb", mode="all")["changes"] == []


def test_blocked_statement():
    assert rewrite("delete from t", "duckdb")["status"] == "block"


def test_write_statement_keeps_its_target():
    out = rewrite("create table x as select a from t where date(ts) = '2024-01-01'", "duckdb", table_info=typed())
    assert flat(out["fixed_sql"]).startswith("CREATE TABLE x AS SELECT a FROM t WHERE ts >= '2024-01-01'")


# -- the guarantee, checked on real data ---------------------------------------------------
STAMPS = ["2023-12-31 23:59:59.999999", "2024-01-01 00:00:00", "2024-01-01 12:00:00", "2024-01-01 23:59:59.999999",
          "2024-01-02 00:00:00", "2024-02-29 13:00:00", "2024-03-01 00:00:00", "2024-12-31 23:59:59",
          "2025-01-01 00:00:00", "infinity", "-infinity", None]


@pytest.fixture(scope="module")
def edge_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("fix") / "edge.duckdb")
    con = duckdb.connect(path)
    con.execute("create table t (id int, ts timestamp, d date)")
    for i, s in enumerate(STAMPS):
        con.execute("insert into t values (?, ?::timestamp, ?::timestamp::date)", [i, s, s])
    info = {"t": {"columns": [{"name": n, "type": ty} for n, ty in con.execute("select column_name, data_type "
                                                                                "from duckdb_columns() where "
                                                                                "table_name = 't'").fetchall()]}}
    con.close()
    return path, info


PREDICATES = [
    "date({c}) = '2024-01-01'", "{c}::date = '2024-01-01'", "date({c}) >= '2024-01-01'", "date({c}) > '2024-01-01'",
    "date({c}) < '2024-01-02'", "date({c}) <= '2024-01-01'", "date({c}) between '2024-01-01' and '2024-02-29'",
    "year({c}) = 2024", "extract(year from {c}) = 2024", "year({c}) between 2023 and 2024",
    "date_trunc('month', {c}) = '2024-02-01'", "date_trunc('year', {c}) = '2024-01-01'",
    "date_trunc('quarter', {c}) = '2024-01-01'", "date_trunc('quarter', {c}) >= '2024-04-01'",
    "not (date({c}) = '2024-01-01')", "date({c}) = '2024-01-01' or id = 9", "'2024-01-01' < date({c})",
]


@pytest.mark.parametrize("column", ["ts", "d"])
@pytest.mark.parametrize("predicate", PREDICATES)
def test_same_rows_on_real_data(edge_db, column, predicate):
    path, info = edge_db
    sql = f"select id from t where {predicate.format(c=column)} order by id"
    out = rewrite(sql, "duckdb", table_info=info)
    assert out["changes"], sql
    assert out["same_result"]
    con = duckdb.connect(path, read_only=True)
    before = con.execute(sql).fetchall()
    after = con.execute(out["fixed_sql"]).fetchall()
    con.close()
    assert before == after, (sql, out["fixed_sql"])


def test_timestamptz_is_left_alone_dst_at_midnight():
    # Atlantic/Azores repeats 00:00 on 2024-10-27: a literal range would miss the first hour
    con = duckdb.connect()
    con.execute("SET TimeZone = 'Atlantic/Azores'")
    con.execute("create table t as select timestamptz '2024-10-27 00:00:00+00' + i * interval 1 minute as tz "
                "from range(-120, 180) r(i)")
    info = {"t": {"columns": [{"name": "tz", "type": "TIMESTAMP WITH TIME ZONE"}]}}
    sql = "select count(*) from t where date(tz) = '2024-10-27'"
    assert rewrite(sql, "duckdb", table_info=info)["changes"] == []
    assumed = rewrite(sql, "duckdb", mode="all")  # unknown type: offered, but labelled as an assumption
    assert assumed["changes"][0]["guarantee"] == "assumes"
    assert con.execute(sql).fetchone() != con.execute(assumed["fixed_sql"]).fetchone()  # why it is not "same"


def test_not_exists_matches_not_in_without_nulls():
    con = duckdb.connect()
    con.execute("create table e as select range as id, range % 7 as user_id from range(100)")
    con.execute("create table banned as select * from (values (1), (3)) v(user_id)")
    sql = "select e.id from e where e.user_id not in (select user_id from banned) order by 1"
    out = rewrite(sql, "duckdb", mode="all")
    assert con.execute(sql).fetchall() == con.execute(out["fixed_sql"]).fetchall()
    con.execute("insert into banned values (null)")  # the documented difference
    assert con.execute(sql).fetchall() == []
    assert con.execute(out["fixed_sql"]).fetchall() != []


# -- integration --------------------------------------------------------------------------
@pytest.mark.parametrize("limit", [-5, 0])
def test_invalid_limit_is_refused_before_rewriting(monkeypatch, limit):
    def forbidden_rewrite(*args, **kwargs):
        pytest.fail("an invalid limit must be rejected before rewriting")

    monkeypatch.setattr(fixer, "rewrite", forbidden_rewrite)
    result = fix(None, "SELECT * FROM events", mode="all", limit=limit)
    assert result["status"] == "error"
    assert str(limit) in result["error"]
    assert "limit" in result["error"]
    assert result["next_step"]


def test_valid_or_omitted_limits_and_safe_mode_keep_their_behavior():
    sql = "SELECT * FROM events"
    assert "LIMIT 1" in fix(None, sql, mode="all", limit=1)["fixed_sql"]
    assert "LIMIT 1000" in fix(None, sql, mode="all")["fixed_sql"]
    safe = fix(None, sql, mode="safe")
    assert fix(None, sql, mode="safe", limit=0) == safe
    assert fix(None, sql, mode="safe", limit=-5) == safe


def test_fix_with_connection_reports_estimates(settings):
    out = fix(settings, "select event_type, count(*) from events where date(ts) = '2025-01-02' group by 1")
    assert out["status"] == "ok" and out["same_result"]
    assert out["changes"][0]["guarantee"] == "same" and "TIMESTAMP" in out["changes"][0]["note"]
    assert out["estimate"]["before"]["bytes"] and out["estimate"]["after"]["bytes"]
    assert "saving" in out and "summary" in out["saving"]
    assert out["connection"] == "default"


def test_fix_nothing_to_do(settings):
    out = fix(settings, "select count(*) from events")
    assert out["changes"] == [] and out["status"] == "ok"


def test_fix_other_dialect_works_offline(settings):
    out = fix(settings, "SELECT a FROM t WHERE DATE(ts) = '2024-01-01'", dialect="bigquery", mode="all")
    assert out["changes"] and "estimate" not in out and "connection" not in out
    assert out["changes"][0]["guarantee"] == "assumes"


def test_preflight_hands_over_the_fixed_sql(settings):
    pre = core.preflight(settings, "select event_type, count(*) from events where date(ts) = '2025-01-02' group by 1")
    assert pre["autofix"]["same_result"] and "ts >= '2025-01-02'" in flat(pre["autofix"]["fixed_sql"])
    assert "autofix.fixed_sql" in pre["next_step"]
    # offline lint cannot see column types, so it offers no same-rows fix
    assert "autofix" not in core.lint_sql("select a from t where year(ts) = 2024", "duckdb")


def test_cli_fix(demo_db, state, capsys, tmp_path):
    assert main(["--dsn", demo_db, "fix", "select count(*) from events where date(ts) = '2025-01-02'"]) == 0
    text = capsys.readouterr().out
    assert "returns exactly the same rows" in text and "ts >= '2025-01-02'" in text
    assert main(["fix", "--json", "--dialect", "bigquery", "--mode", "all", "SELECT * FROM t"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["changes"][0]["rule"] == "GV002"
    out = tmp_path / "model.fixed.sql"
    assert main(["--dsn", demo_db, "fix", "-o", str(out),
                 "select count(*) from events where date(ts) = '2025-01-02'"]) == 0
    listed = capsys.readouterr().out
    assert "returns exactly the same rows" in listed and out.is_file()
    assert "ts >= '2025-01-02'" in out.read_text(encoding="utf-8")
    # no changes → do not create/overwrite the output file
    empty = tmp_path / "unchanged.sql"
    assert main(["fix", "--dialect", "duckdb", "-o", str(empty), "select count(*) from events"]) == 0
    assert "Nothing to rewrite" in capsys.readouterr().out
    assert not empty.exists()


def test_cli_fix_utf8_sql_round_trip(demo_db, state, capsys, tmp_path):
    """SQL files with non-ASCII text must round-trip as UTF-8 on every OS (#13)."""
    src = tmp_path / "cafe.sql"
    src.write_text("select 'café' as city, count(*) from events where date(ts) = '2025-01-02'\n", encoding="utf-8")
    out = tmp_path / "cafe.fixed.sql"
    assert main(["--dsn", demo_db, "fix", "-f", str(src), "-o", str(out)]) == 0
    capsys.readouterr()
    text = out.read_text(encoding="utf-8")
    assert "café" in text
    assert "ts >= '2025-01-02'" in text


def test_mcp_fix_tool(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    server = build_server()
    result = asyncio.run(server.call_tool("gulliver_fix", {"sql": "select count(*) from events where year(ts) = 2025"}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "GV005" in text and "same" in text


def test_year_rewrite_keeps_the_rows_on_infinity_where_it_is_still_offered():
    con = duckdb.connect()
    con.execute("create table t (a integer, ts timestamp)")
    con.execute("insert into t values (1, 'infinity'), (2, '2024-05-01'), (3, '2023-05-01'), (4, null)")
    for sql in ("select a from t where year(ts) = 2024 or a = 3", "select a from t where (year(ts) = 2024 and a > 0)"):
        out = rewrite(sql, "duckdb", table_info=typed("TIMESTAMP"))
        assert [c["guarantee"] for c in out["changes"] if c["rule"] == "GV005"] == ["same"], sql
        assert sorted(con.execute(sql).fetchall()) == sorted(con.execute(out["fixed_sql"]).fetchall()), sql


def test_not_in_projecting_an_outer_column_is_left_alone():
    out = rewrite("select o.id from orders o where o.cid not in (select o.x from blocked)", "duckdb", mode="all")
    assert "GV012" not in [c["rule"] for c in out["changes"]]
    out = rewrite("select o.id from orders o where o.cid not in (select b.cid from blocked b)", "duckdb", mode="all")
    assert "GV012" in [c["rule"] for c in out["changes"]]
