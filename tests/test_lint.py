import pytest

from gulliver.lint import lint
from gulliver.sqlsafety import classify


def rules(sql, dialect="duckdb", table_info=None):
    stmt = classify(sql, dialect)
    return {f.rule: f.severity for f in lint(stmt.select, dialect, table_info)}


def test_clean_aggregate_has_no_findings():
    assert rules("select country, count(*) from users where signup_date >= '2024-01-01' group by 1") == {}


def test_unbounded_output_absent_when_the_outer_query_aggregates():
    for sql in ("select count(*) from events",
                "select country, count(*) from users group by 1",
                "select exists (select 1 from orders) as any_orders",
                "select (select count(*) from events) as n",
                "select 1"):
        for dialect in ("duckdb", "postgres"):
            assert "GV002" not in rules(sql, dialect), (sql, dialect)


def test_aggregate_in_a_scalar_subquery_does_not_collapse_the_outer_rows():
    # The COUNT(*) belongs to the subquery: the outer query still returns one row per user.
    for dialect in ("duckdb", "postgres"):
        r = rules("select u.id, (select count(*) from orders o where o.user_id = u.id) as n from users u",
                  dialect)
        assert r["GV002"] == "warn", (r, dialect)


def test_unbounded_output_still_reported_for_a_plain_select():
    for dialect in ("duckdb", "postgres"):
        assert rules("select id from users", dialect)["GV002"] == "warn"


def test_select_star_and_unbounded_output():
    r = rules("select * from events")
    assert r["GV001"] == "warn"
    assert r["GV002"] == "warn"


def test_exists_subquery_star_is_not_a_scan():
    for dialect in ("duckdb", "postgres"):
        sql = "select u.id from users u where exists (select * from orders o where o.user_id = u.id) limit 10"
        assert "GV001" not in rules(sql, dialect), dialect
        not_exists = sql.replace("exists", "not exists")
        assert "GV001" not in rules(not_exists, dialect), dialect


def test_star_after_an_exists_subquery_is_still_found():
    derived = ("select u.id from users u where exists (select * from orders o where o.user_id = u.id) "
               "and u.id in (select * from ids) limit 10")
    assert rules(derived)["GV001"] == "warn"
    nested = ("select u.id from users u where exists (select * from orders o where o.user_id = u.id) "
              "and exists (select 1 from flags f where f.user_id = u.id) limit 10")
    assert "GV001" not in rules(nested)


def test_plain_and_derived_table_star_still_warn():
    assert rules("select * from users limit 10")["GV001"] == "warn"
    assert rules("select id from (select * from users) t limit 10")["GV001"] == "warn"
    assert rules("select * from users limit 10", "postgres")["GV001"] == "warn"


def test_limit_removes_unbounded_warning():
    assert "GV002" not in rules("select id from events limit 10")


def test_missing_join_condition_is_an_error():
    assert rules("select e.id from events e, users u")["GV003"] == "error"


def test_natural_join_has_an_implicit_key_and_is_not_cartesian():
    for sql in ("select a from t natural join u limit 5",
                "select a from t natural left join u limit 5"):
        for dialect in ("duckdb", "postgres"):
            assert "GV003" not in rules(sql, dialect), (sql, dialect)


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
    assert rules("select a from t where b like '_x'")["GV013"] == "info"
    assert rules("select a from t where b ilike '%x%' limit 5")["GV013"] == "info"
    assert rules("select a from t where b ilike '__x%' limit 5")["GV013"] == "info"
    assert "GV013" not in rules("select a from t where b like 'x%'")
    assert "GV013" not in rules("select a from t where b like 'a_c'")
    # an escaped underscore is a literal prefix, not a wildcard
    assert "GV013" not in rules("select a from t where b like '\\_x' escape '\\'")


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery"])
def test_deep_offset_flagged_across_dialects(dialect):
    stmt = classify("select id from events order by id limit 50 offset 200000", dialect)
    findings = [f for f in lint(stmt.select, dialect) if f.rule == "GV017"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].name == "deep-offset"
    assert "keyset pagination" in findings[0].fix.lower()
    assert findings[0].node is not None


def test_deep_offset_fetch_next_postgres():
    stmt = classify("select id from events offset 200000 rows fetch next 50 rows only", "postgres")
    findings = [f for f in lint(stmt.select, "postgres") if f.rule == "GV017"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert "keyset pagination" in findings[0].fix.lower()


def test_deep_offset_in_subquery():
    stmt = classify("select * from (select id from events offset 200000)", "duckdb")
    findings = [f for f in lint(stmt.select, "duckdb") if f.rule == "GV017"]
    assert len(findings) == 1
    assert findings[0].severity == "warn"


@pytest.mark.parametrize("sql", [
    "select id from events offset 100",
    "select id from events offset 9999",
    "select id from events offset ?",
    "select id from events limit 50",
])
def test_deep_offset_negatives(sql):
    stmt = classify(sql, "duckdb")
    findings = [f for f in lint(stmt.select, "duckdb") if f.rule == "GV017"]
    assert len(findings) == 0


COUNT_SUBQUERY = "(select count(*) from orders o where o.user_id = u.id)"


@pytest.mark.parametrize("dialect", ["duckdb", "postgres"])
@pytest.mark.parametrize("sql", [
    f"select u.id from users u where {COUNT_SUBQUERY} > 0 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} >= 1 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} <> 0 limit 10",
    f"select u.id from users u where 0 < {COUNT_SUBQUERY} limit 10",
    "select u.id from users u where (select count(1) from orders o where o.user_id = u.id) > 0 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} = 0 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} < 1 limit 10",
])
def test_count_for_existence_flagged(sql, dialect):
    stmt = classify(sql, dialect)
    findings = [f for f in lint(stmt.select, dialect) if f.rule == "GV018"]
    assert len(findings) == 1, (sql, dialect, findings)
    assert findings[0].severity == "info"
    assert findings[0].name == "count-for-existence"
    assert findings[0].node is not None


def test_count_for_existence_suggests_exists():
    stmt = classify(f"select u.id from users u where {COUNT_SUBQUERY} > 0 limit 10", "duckdb")
    finding, = [f for f in lint(stmt.select, "duckdb") if f.rule == "GV018"]
    assert "EXISTS" in finding.fix
    assert "NOT EXISTS" not in finding.fix


def test_count_for_existence_suggests_not_exists():
    stmt = classify(f"select u.id from users u where {COUNT_SUBQUERY} = 0 limit 10", "duckdb")
    finding, = [f for f in lint(stmt.select, "duckdb") if f.rule == "GV018"]
    assert "NOT EXISTS" in finding.fix


def test_count_for_existence_in_case_when():
    # GV010 may fire on the same subquery; that is fine.
    sql = "select case when (select count(*) from orders o) > 0 then 1 else 0 end as n from users limit 10"
    assert rules(sql)["GV018"] == "info"


@pytest.mark.parametrize("sql", [
    f"select u.id from users u where {COUNT_SUBQUERY} > 5 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} = 3 limit 10",
    f"select u.id from users u where {COUNT_SUBQUERY} >= 2 limit 10",
    "select u.id from users u where (select count(*) from orders o group by o.user_id) > 0 limit 10",
    "select u.id from users u where (select count(o.id) from orders o) > 0 limit 10",
    "select user_id from orders group by user_id having count(*) > 0",
    "select count(*) from users",
    "select u.id from users u where exists (select 1 from orders o where o.user_id = u.id) limit 10",
])
def test_count_for_existence_not_flagged(sql):
    assert "GV018" not in rules(sql), sql


def test_count_for_existence_is_listed_with_rule_metadata():
    from gulliver.lint_report import rules as rule_docs

    rule, = [r for r in rule_docs() if r["rule"] == "GV018"]
    assert rule["name"] == "count-for-existence"
    assert rule["default_severity"] == "info"
    assert rule["autofix"] == "none"
    assert "EXISTS" in rule["fix"]
