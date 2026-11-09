"""Impact/lineage, drift, translate, diff, incremental advisor and quality checks."""

from __future__ import annotations

import duckdb
import pytest

from gulliver import features, incremental
from gulliver.cli import main
from gulliver.config import load_settings
from gulliver.lineage import render

MODELS = {
    "stg_events.sql": "{{ config(materialized='view') }}\nselect event_id, user_id, event_type, amount, ts "
                      "from {{ source('raw', 'events') }} where event_type <> 'test'",
    "stg_users.sql": "select user_id, country from {{ source('raw', 'users') }}",
    "fct_revenue.sql": "select u.country, date_trunc('day', e.ts) as day, sum(e.amount) as revenue "
                       "from {{ ref('stg_events') }} e join {{ ref('stg_users') }} u on e.user_id = u.user_id "
                       "group by 1, 2",
    "user_ltv.sql": "select e.user_id, sum(e.amount) as ltv from {{ ref('stg_events') }} e group by 1",
    "country_report.sql": "select country, sum(revenue) as revenue from {{ ref('fct_revenue') }} group by 1",
}


@pytest.fixture
def models(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    for name, sql in MODELS.items():
        (folder / name).write_text(sql, encoding="utf-8")
    return str(folder)


# -- lineage / impact ------------------------------------------------------------------------------
def test_render_dbt_jinja():
    sql, refs, sources = render("{{ config(x=1) }} select * from {{ ref('a') }} join {{ source('s','t') }} "
                                "{% if is_incremental() %} where ts > (select max(ts) from {{ this }}) {% endif %}",
                                "m")
    assert refs == {"a"} and sources == {"s.t"}
    assert "config" not in sql and "{{" not in sql and "from m" in sql


def test_load_models_reads_utf8_sql(tmp_path):
    from gulliver.lineage import load_models

    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "cafe.sql").write_text("select 'café' as city from {{ source('raw','events') }}\n", encoding="utf-8")
    loaded = load_models(str(folder))
    assert "café" in loaded["cafe"].sql


def test_lineage_overview(models):
    ov = features.lineage_overview(models)
    assert ov["status"] == "ok"
    assert ov["sources"] == ["raw.events", "raw.users"]
    assert {"from": "stg_events", "to": "user_ltv"} in ov["edges"]


def test_column_impact_is_transitive(models):
    r = features.impact("raw.events.amount", models)
    by_model = {a["model"]: a for a in r["affected"]}
    assert set(by_model) == {"stg_events", "fct_revenue", "user_ltv", "country_report"}
    assert by_model["country_report"]["columns"] == ["revenue"]
    assert by_model["country_report"]["depth"] == 3


def test_filter_only_dependency_and_unrelated_column(models):
    r = features.impact("raw.events.event_type", models)
    by_model = {a["model"]: a for a in r["affected"]}
    assert by_model["stg_events"]["columns"] == ["event_type"]
    assert "country_report" not in by_model
    assert features.impact("raw.users.signup_date", models)["count"] == 0


def test_whole_table_impact(models):
    r = features.impact("raw.users", models)
    assert {a["model"] for a in r["affected"]} == {"stg_users", "fct_revenue", "country_report"}


def test_impact_cli(models, capsys):
    assert main(["impact", "raw.events.amount", "--models", models]) == 0
    assert "country_report" in capsys.readouterr().out


# -- drift -------------------------------------------------------------------------------------------
def test_drift_detects_removed_column_and_impact(tmp_path, state, models):
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema raw")
    con.execute("create table raw.events as select 1 as event_id, 2 as user_id, 'buy' as event_type, "
                "1.0 as amount, now() as ts")
    con.close()
    s = load_settings(dsn=str(db))
    first = features.drift(s)
    assert "snapshot" in first["next_step"].lower() or first["status"] == "ok"
    con = duckdb.connect(str(db))
    con.execute("alter table raw.events drop column amount")
    con.execute("alter table raw.events add column channel varchar")
    con.close()
    r = features.drift(s, models_dir=models)
    assert r["status"] == "warn"
    kinds = {(c["table"], c.get("column"), c["change"]) for c in r["changes"]}
    assert ("raw.events", "amount", "removed") in kinds
    assert ("raw.events", "channel", "added") in kinds
    assert {"stg_events", "user_ltv"} <= {a["model"] for a in r["impact"]}
    saved = features.drift(s, save=True)
    assert saved["saved"] is True
    assert features.drift(s)["changes"] == []


# -- translate ---------------------------------------------------------------------------------------
def test_translate_and_unknown_function_warning():
    r = features.translate(None, "select date_trunc('day', ts) as d, count(*) from events group by 1",
                           "bigquery", "duckdb")
    assert "TIMESTAMP_TRUNC" in r["sql"]
    r2 = features.translate(None, "select my_udf(x) from t", "snowflake", "duckdb")
    assert r2["status"] == "warn" and "MY_UDF" in r2["unknown_functions"]


def test_translate_check_on_connection(settings):
    r = features.translate(settings, "select country, count(*) from users group by 1", "duckdb", "postgres",
                           connection="default")
    assert r["check"]["status"] == "ok"


# -- diff ----------------------------------------------------------------------------------------------
@pytest.fixture(params=[
    (("x|y", "z"), ("x", "y|z"), "x~py|z", "x|y~pz"),
    (("x~p", "z"), ("x|", "z"), "x~~p|z", "x~p|z"),
    (("x~|y", "z"), ("x~", "y|z"), "x~~~py|z", "x~~|y~pz"),
    (("", "|"), ("|", ""), "|~p", "~p|"),
])
def composite_values(tmp_path, state, request):
    left, right, left_key, right_key = request.param
    db = tmp_path / "composite.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE left_items (id INTEGER, a VARCHAR, b VARCHAR)")
        con.execute("CREATE TABLE right_items (id INTEGER, a VARCHAR, b VARCHAR)")
        con.execute("INSERT INTO left_items VALUES (1, ?, ?)", left)
        con.execute("INSERT INTO right_items VALUES (1, ?, ?)", right)
    return load_settings(dsn=str(db)), left_key, right_key


def test_diff_distinguishes_embedded_separators_in_values(composite_values):
    settings, _, _ = composite_values
    result = features.diff(settings, "left_items", "right_items", ["id"])
    assert result["status"] == "warn", result
    assert result["identical"] is False
    assert result["differences"]["changed"]["count"] == 1
    assert result["differences"]["changed"]["sample"] == ["1"]
    assert "columns" in result["differences"]["changed"]
    assert "sample_columns" in result["differences"]["changed"]
    assert result["details_complete"] is True


def test_diff_distinguishes_embedded_separators_in_composite_keys(composite_values):
    settings, left_key, right_key = composite_values
    result = features.diff(settings, "left_items", "right_items", ["a", "b"], columns=["id"])
    assert result["status"] == "warn", result
    assert result["identical"] is False
    details = result["differences"]
    assert details["only_in_left"] == {"count": 1, "sample": [left_key]}
    assert details["only_in_right"] == {"count": 1, "sample": [right_key]}
    assert details["changed"]["count"] == details["duplicate_keys"]["count"] == 0


def test_diff_identical(settings):
    r = features.diff(settings, "users", "select user_id, country, signup_date from users", ["user_id"])
    assert r["status"] == "ok" and r["identical"] is True
    assert r["left_rows"] == r["right_rows"] == 20_000


def test_diff_finds_missing_extra_changed(settings):
    right = ("select user_id, case when user_id = 7 then 'XX' else country end as country, signup_date "
             "from users where user_id <> 5 union all select 999999, 'NL', date '2020-01-01'")
    r = features.diff(settings, "users", right, ["user_id"])
    assert r["identical"] is False
    d = r["differences"]
    assert d["only_in_left"]["sample"] == ["5"]
    assert d["only_in_right"]["sample"] == ["999999"]
    assert d["changed"]["sample"] == ["7"]
    assert d["changed"]["columns"] == {"country": 1}
    assert d["changed"]["sample_columns"] == {"7": ["country"]}
    assert "Rows differ in: country (1 key)" in r["next_step"]
    assert r["details_complete"] is True


def test_diff_two_columns_changed_on_different_keys(settings):
    right = ("select user_id, case when user_id = 7 then 'XX' else country end as country, "
             "case when user_id = 8 then date '1999-01-01' else signup_date end as signup_date "
             "from users")
    r = features.diff(settings, "users", right, ["user_id"])
    d = r["differences"]
    assert d["changed"]["columns"] == {"country": 1, "signup_date": 1}
    assert d["changed"]["sample_columns"] == {"7": ["country"], "8": ["signup_date"]}
    assert "Rows differ in: country (1 key), signup_date (1 key)" in r["next_step"]


def test_diff_null_to_value(tmp_path):
    db = tmp_path / "nulls.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE t1 (id INT, val VARCHAR)")
        con.execute("CREATE TABLE t2 (id INT, val VARCHAR)")
        con.execute("INSERT INTO t1 VALUES (1, NULL)")
        con.execute("INSERT INTO t2 VALUES (1, 'hello')")
    s = load_settings(dsn=str(db))
    r = features.diff(s, "t1", "t2", ["id"])
    assert r["differences"]["changed"]["columns"] == {"val": 1}
    assert r["differences"]["changed"]["sample_columns"] == {"1": ["val"]}


def test_diff_columns_filter(settings):
    right = "select user_id, country, date '1999-01-01' as signup_date from users"
    r = features.diff(settings, "users", right, ["user_id"], columns=["user_id", "country"])
    assert r["identical"] is True

    right_diff = ("select user_id, case when user_id = 7 then 'XX' else country end as country, "
                  "date '1999-01-01' as signup_date from users")
    r_diff = features.diff(settings, "users", right_diff, ["user_id"], columns=["user_id", "country"])
    assert r_diff["identical"] is False
    assert r_diff["differences"]["changed"]["columns"] == {"country": 1}
    assert r_diff["differences"]["changed"]["sample_columns"] == {"7": ["country"]}
    assert "signup_date" not in r_diff["columns_compared"]


def test_diff_duplicate_keys_omitted_from_column_counts(tmp_path):
    db = tmp_path / "dup.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE t1 (id INT, val VARCHAR)")
        con.execute("CREATE TABLE t2 (id INT, val VARCHAR)")
        con.execute("INSERT INTO t1 VALUES (1, 'A'), (1, 'B')")
        con.execute("INSERT INTO t2 VALUES (1, 'C')")
    s = load_settings(dsn=str(db))
    r = features.diff(s, "t1", "t2", ["id"])
    d = r["differences"]
    assert d["duplicate_keys"]["count"] == 1
    assert d["changed"]["count"] == 1
    assert d["changed"]["columns"] == {}
    assert "omitted from column counts" in d["changed"]["note"]


def test_diff_zooms_in_on_big_buckets(settings):
    right = "select event_id, user_id, amount from events where event_id <> 123"
    r = features.diff(settings, "select event_id, user_id, amount from events", right, ["event_id"], buckets=4)
    assert r["differences"]["only_in_left"]["sample"] == ["123"]
    assert r["differences"]["only_in_right"]["count"] == 0


def test_diff_zooms_in_differing_columns(settings):
    right = "select event_id, user_id, case when event_id = 123 then amount + 10.0 else amount end as amount from events"
    r = features.diff(settings, "select event_id, user_id, amount from events", right, ["event_id"], buckets=4)
    assert r["differences"]["changed"]["columns"] == {"amount": 1}
    assert r["differences"]["changed"]["sample_columns"] == {"123": ["amount"]}


def test_diff_bad_key(settings):
    assert features.diff(settings, "users", "users", ["nope"])["status"] == "error"


# -- incremental -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql,expected", [
    ("select event_id, user_id, ts from events where event_type = 'buy'", "append"),
    ("select date_trunc('day', ts) as day, count(*) from events group by 1", "partition_overwrite"),
    ("select user_id, sum(amount) from events group by 1", "merge_recompute"),
    ("select user_id, ts, row_number() over (partition by user_id order by ts) from events", "merge_recompute"),
    ("select count(*) from events", "not_safe"),
    ("select user_id, ts, row_number() over (order by ts) from events", "not_safe"),
    ("select event_id, ts from events order by ts limit 10", "not_safe"),
])
def test_incremental_strategies(sql, expected):
    assert incremental.analyze(sql, time_column="ts")["strategy"] == expected


def test_incremental_sql_and_savings(settings):
    r = features.incremental(settings, "select user_id, sum(amount) as total from events group by 1",
                             time_column="ts", period="1d", lookback=0)
    assert r["strategy"] == "merge_recompute"
    assert "user_id IN (SELECT DISTINCT user_id FROM events WHERE ts > :last_loaded_at)" in r["incremental_sql"]
    assert "incremental_strategy='merge'" in r["dbt_model"]
    sav = r["savings"]
    assert sav["status"] == "ok" and sav["source_rows"] == 400_000
    assert 0 < sav["rows_per_incremental_run"] < 400_000


def test_incremental_append_dbt_uses_this(settings):
    r = features.incremental(settings, "select event_id, amount, ts from events", time_column="ts",
                             unique_key=["event_id"], estimate_savings=False)
    assert "(SELECT MAX(ts) FROM {{ this }})" in r["dbt_model"]
    assert "unique_key='event_id'" in r["dbt_model"]


def test_incremental_rejects_negative_lookback(settings):
    r = features.incremental(settings, "select user_id, count(*) from events group by 1",
                            time_column="ts", lookback=-1)
    assert r["status"] == "error"
    assert "lookback" in r["error"].lower()
    assert "next_step" in r


def test_parse_period():
    assert incremental.parse_period("24h").total_seconds() == 86400
    with pytest.raises(ValueError):
        incremental.parse_period("soon")


# -- checks -------------------------------------------------------------------------------------------------
def test_composite_unique_distinguishes_embedded_separators(composite_values):
    settings, _, _ = composite_values
    query = "SELECT a, b FROM left_items UNION ALL SELECT a, b FROM right_items"
    result = features.check(settings, query, unique=["a,b"], samples=2)
    assert result["status"] == "ok", result
    unique = next(c for c in result["checks"] if c["check"] == "unique")
    assert unique["failing_rows"] == 0
    assert "samples" not in unique

    duplicate = features.check(settings, query + " UNION ALL SELECT a, b FROM left_items",
                               unique=["a,b"], samples=2)
    assert duplicate["status"] == "fail", duplicate
    unique = next(c for c in duplicate["checks"] if c["check"] == "unique")
    assert unique["failing_rows"] == 1
    assert len(unique["samples"]) == 1
    assert unique["samples"][0]["rows"] == 2


def test_checks_pass_and_fail(settings):
    ok = features.check(settings, "events", unique=["event_id"], not_null=["user_id"],
                        accepted=["event_type=view|click|cart|buy"], ranges=["amount=0:100"], min_rows=1000)
    assert ok["status"] == "ok", ok
    bad = features.check(settings, "events", unique=["user_id"], accepted=["event_type=view|click"],
                         fresh=["ts=1h"])
    assert bad["status"] == "fail"
    failed = {c["check"] for c in bad["checks"] if not c["passed"]}
    assert failed == {"unique", "accepted_values", "freshness"}


def test_check_reports_a_bad_range_before_querying(settings):
    r = features.check(settings, "events", ranges=["amount=100:1"])
    assert r["status"] == "error"
    assert "min 100 > max 1" in r["error"]


def test_check_row_count_change(settings):
    features.check(settings, "users")
    r = features.check(settings, "select * from users where user_id < 10000", max_change_pct=10)
    assert r["status"] == "ok"  # different target, own history
    r2 = features.check(settings, "users", max_change_pct=10)
    assert r2["checks"][0]["previous_rows"] == 20_000


def test_check_history_is_keyed_on_the_resolved_connection(settings):
    features.check(settings, "users")
    r = features.check(settings, "users", connection="default", max_change_pct=10)
    assert r["checks"][0]["previous_rows"] == 20_000


def test_different_connections_keep_separate_histories(settings):
    from dataclasses import replace

    from gulliver.config import Connection

    features.check(settings, "users")
    path = settings.connection().params["path"]
    other = replace(
        settings,
        connections={**settings.connections, "second": Connection("second", "duckdb", {"path": path})},
    )
    r = features.check(other, "users", connection="second", max_change_pct=10)
    assert "previous_rows" not in r["checks"][0]


def test_a_history_saved_under_the_old_none_key_is_still_read(settings):
    from gulliver.sqlsafety import sql_fingerprint

    features.check(settings, "users")
    checks = settings.state_dir / "checks"
    (checks / f"{sql_fingerprint('default|users|')}.json").rename(checks / f"{sql_fingerprint('None|users|')}.json")
    r = features.check(settings, "users", max_change_pct=10)
    assert r["checks"][0]["previous_rows"] == 20_000


def test_check_cli_exit_code(demo_db, state):
    assert main(["--dsn", demo_db, "check", "events", "--unique", "user_id"]) == 4
    assert main(["--dsn", demo_db, "check", "events", "--unique", "event_id"]) == 0
