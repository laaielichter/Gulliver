import pytest

from gulliver.lint import lint
from gulliver.sqlsafety import classify


def rules(sql, dialect="duckdb", table_info=None):
    stmt = classify(sql, dialect)
    return {f.rule: f.severity for f in lint(stmt.select, dialect, table_info)}


def test_clean_aggregate_has_no_findings():
    assert rules("select country, count(*) from users where signup_date >= '2024-01-01' group by 1") == {}


def test_select_star_and_unbounded_output():
    r = rules("select * from events")
    assert r["GV001"] == "warn"
    assert r["GV002"] == "warn"


def test_limit_removes_unbounded_warning():
    assert "GV002" not in rules("select id from events limit 10")


def test_missing_join_condition_is_an_error():
    assert rules("select e.id from events e, users u")["GV003"] == "error"


def test_comma_join_with_where_key_is_info():
    assert rules("select e.id from events e, users u where e.user_id = u.user_id limit 5")["GV003"] == "info"


def test_cross_join_is_a_warning():
    assert rules("select e.id from events e cross join users u limit 5")["GV003"] == "warn"


def test_unnest_join_is_not_cartesian():
    r = rules("select x from t, unnest(t.arr) as x limit 5", "bigquery")
    assert "GV003" not in r


def test_non_equi_join():
    assert rules("select a.id from a join b on a.ts between b.start_ts and b.end_ts limit 5")["GV008"] == "warn"


def test_missing_partition_filter_uses_metadata():
    info = {"events": {"partitioned_by": "event_date"}}
    assert rules("select count(*) from events", table_info=info)["GV004"] == "error"
    assert "GV004" not in rules("select count(*) from events where event_date = '2026-01-01'", table_info=info)


def test_function_on_filter_column():
    assert rules("select count(*) from events where date(ts) = '2024-01-01'")["GV005"] == "warn"
    assert "GV005" not in rules("select count(*) from events where ts >= '2024-01-01'")


def test_order_without_limit():
    assert rules("select id from events order by id")["GV006"] == "warn"


@pytest.mark.parametrize("dialect, function", [("duckdb", "random()"), ("postgres", "random()"),
                                               ("bigquery", "rand()")])
@pytest.mark.parametrize("suffix", ["", " limit 100"])
def test_order_by_random_warns_even_with_limit(dialect, function, suffix):
    stmt = classify(f"select id from events order by {function}{suffix}", dialect)
    finding, = [f for f in lint(stmt.select, dialect) if f.rule == "GV016"]
    assert finding.severity == "warn"
    assert finding.name == "order-by-random"
    assert "TABLESAMPLE" in finding.fix and "USING SAMPLE" in finding.fix
    assert finding.node is not None


@pytest.mark.parametrize("sql", [
    "select random() as r from events limit 10",
    "select id from events where random() < 0.1 limit 10",
    "select random() as r from events order by id limit 10",
    "select id from events order by id limit 10",
])
def test_random_outside_order_by_does_not_warn(sql):
    assert "GV016" not in rules(sql)


def test_order_by_random_inside_subquery_and_expression():
    sql = "select id from (select id from events order by abs(random()) limit 100) sample limit 10"
    assert rules(sql)["GV016"] == "warn"


def test_order_by_random_is_listed_with_rule_metadata():
    from gulliver.lint_report import rules as rule_docs

    rule, = [r for r in rule_docs() if r["rule"] == "GV016"]
    assert rule["name"] == "order-by-random"
    assert rule["default_severity"] == "warn"
    assert rule["autofix"] == "none"
    assert "TABLESAMPLE" in rule["fix"] and "USING SAMPLE" in rule["fix"]


def test_count_distinct_union_window_not_in():
    r = rules("select count(distinct user_id) from events")
    assert r["GV007"] == "info"
    assert rules("select id from a union select id from b")["GV009"] == "info"
    assert rules("select id, row_number() over (order by ts) from events limit 5")["GV011"] == "warn"
    assert rules("select id from a where id not in (select id from b) limit 5")["GV012"] == "info"


def test_scalar_subquery_in_select():
    assert rules("select id, (select max(x) from b where b.id = a.id) from a limit 5")["GV010"] == "info"


def test_leading_wildcard_like_is_info():
    assert rules("select a from t where b like '%x'")["GV013"] == "info"
    assert rules("select a from t where b ilike '%x%' limit 5")["GV013"] == "info"
    assert "GV013" not in rules("select a from t where b like 'x%'")
