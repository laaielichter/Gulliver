import pytest

from gulliver.sqlsafety import classify, sql_fingerprint


def test_select_is_read():
    s = classify("select a, b from t where a > 1;")
    assert s.kind == "read"
    assert s.tables == ["t"]


def test_cte_names_are_not_tables():
    s = classify("with x as (select * from events) select * from x join users u on x.id = u.id")
    assert s.kind == "read"
    assert sorted(s.tables) == ["events", "users"]


def test_create_table_as_select_is_write():
    s = classify("create table out as select id from events", "snowflake")
    assert s.kind == "write"
    assert s.target == "out"
    assert s.tables == ["events"]


def test_insert_select_is_write():
    s = classify("insert into out select id from events", "postgres")
    assert s.kind == "write"
    assert s.tables == ["events"]


def test_destructive_statements_are_blocked():
    for sql in ("delete from t", "drop table t", "update t set a = 1", "truncate table t",
                "alter table t add column c int"):
        assert classify(sql).kind == "block", sql


def test_multiple_statements_are_blocked():
    s = classify("select 1; delete from t")
    assert s.kind == "block"
    assert "one statement" in s.reason


def test_select_into_is_blocked():
    assert classify("select * into new_t from t", "postgres").kind == "block"


def test_unparsable_sql_is_blocked():
    s = classify("selec * fro")
    assert s.kind == "block"


def test_fingerprint_ignores_whitespace_and_case():
    assert sql_fingerprint("SELECT  a\nFROM t;") == sql_fingerprint("select a from t")


def test_sql_that_cannot_be_tokenized_is_blocked_not_raised():
    # Postgres normalized text keeps `$1,$2` unspaced, which sqlglot reads as a dollar quote; an open quote too
    for sql, dialect in (("SELECT * FROM t WHERE a IN ($1,$2)", "postgres"),
                         ("SELECT g FROM generate_series($3,$4) g", "postgres"), ("SELECT 'oops AS a", "duckdb")):
        st = classify(sql, dialect)
        assert st.kind == "block" and "Could not parse" in st.reason


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "snowflake"])
@pytest.mark.parametrize("sql, location, description", [
    ("SELECT 1 FROM", 'line 1, column 13, near "FROM"', "Expected table name."),
    ("SELECT FROM WHERE (", 'line 1, column 17, near "WHERE"', "Expected table name."),
    ("SELECT a b c FROM t", 'line 1, column 12, near "c"', "Invalid expression / Unexpected token."),
    ("\n  SELECT a b c FROM t", 'line 2, column 14, near "c"', "Invalid expression / Unexpected token."),
])
def test_syntax_error_message_uses_structured_details(sql, location, description, dialect):
    statement = classify(sql, dialect)
    assert statement.kind == "block"
    assert statement.reason == f"Could not parse SQL for dialect '{dialect}' at {location}: {description}"
    assert "<Token" not in statement.reason and "TokenType" not in statement.reason


@pytest.mark.parametrize("sql", ["SELECT 'abc", "SELECT /* unclosed", "\nSELECT 'café\nhello"])
def test_tokenizer_failure_has_an_explicit_end_of_input_fallback(sql):
    statement = classify(sql)
    line = sql.count("\n") + 1
    column = len(sql.rsplit("\n", 1)[-1]) + 1
    assert statement.kind == "block"
    assert statement.reason == (
        f"Could not parse SQL for dialect 'duckdb' at line {line}, column {column} (end of input): "
        "Could not tokenize SQL; check for an unterminated quote or comment."
    )
