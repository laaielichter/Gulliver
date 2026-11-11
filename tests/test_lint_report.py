"""CI-ready lint: files and folders, positions, ignores, [lint] config, output formats, fix --check/--write."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from gulliver import core
from gulliver.cli import main
from gulliver.config import ConfigError
from gulliver.lint_files import (
    LintConfig,
    discover,
    lint_config_from_mapping,
    lint_file,
    lint_paths,
    render_jinja,
    split_statements,
)
from gulliver.lint_report import render, render_rules_markdown, rules


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _by_rule(result):
    return {f.rule: f for f in result.findings}


# -- positions and statements -------------------------------------------------------------------------
def test_positions_on_a_multi_line_file(tmp_path):
    f = _write(tmp_path / "q.sql", "-- header\nselect *\nfrom events e, users u\n")
    res = lint_file(f)
    found = _by_rule(res)
    assert (found["GV001"].line, found["GV001"].col) == (2, 8)
    assert (found["GV003"].line, found["GV003"].col) == (3, 14)   # the comma of the comma join
    assert (found["GV002"].line, found["GV002"].col) == (2, 1)    # the SELECT keyword
    assert found["GV003"].severity == "error"


def test_multi_statement_file_splits_on_real_semicolons_only(tmp_path):
    f = _write(tmp_path / "q.sql", "select 'a;b' as x from t limit 1;\n-- note; not a split\n"
                                   "select y from t order by y;\ndrop table t;\ncreate table s as select * from t\n")
    res = lint_file(f)
    assert res.statements == 4
    assert [s["statement"] for s in res.skipped] == [3]            # DROP: not a read, skipped, not reported
    rules_by_statement = {(x.statement, x.rule) for x in res.findings}
    assert (2, "GV006") in rules_by_statement and (4, "GV001") in rules_by_statement
    gv006 = next(x for x in res.findings if x.rule == "GV006")
    assert (gv006.line, gv006.col) == (3, 17)                      # ORDER BY


def test_split_statements_keeps_offsets():
    chunks = split_statements("select 1;\n  select 2 ;", "duckdb")
    assert [(c.start, c.end) for c in chunks] == [(0, 8), (12, 20)]


def test_parse_error_is_reported_not_fatal(tmp_path):
    f = _write(tmp_path / "bad.sql", "select 1 from t limit 1;\nselect from from where;\n")
    res = lint_file(f)
    errors = [x for x in res.findings if x.rule == "GV900"]
    assert len(errors) == 1 and errors[0].line == 2 and errors[0].severity == "warn"


# -- dbt Jinja ------------------------------------------------------------------------------------------
def test_dbt_jinja_positions_point_at_the_source_file(tmp_path):
    text = ("{{ config(materialized='table') }}\n{# a comment\n   on two lines #}\n"
            "select o.id, u.name\nfrom {{ ref('orders') }} o\njoin {{ source('raw', 'users') }} u on o.user_id > u.id\n"
            "where {{ var('x') }} = 1 and date(o.ts) = '2024-01-01'\n")
    res = lint_file(_write(tmp_path / "m.sql", text))
    found = _by_rule(res)
    assert (found["GV008"].line, found["GV008"].col) == (6, 1)
    assert (found["GV005"].line, found["GV005"].col) == (7, 30)
    assert found["GV002"].line == 4


def test_render_jinja_maps_offsets_back():
    src = "select a from {{ ref('orders') }} where b = 1"
    r = render_jinja(src)
    assert r.text == "select a from orders where b = 1"
    assert r.to_source(r.text.index("where")) == src.index("where")
    assert r.to_source(r.text.index("orders")) == src.index("{{")
    assert r.clean(0, 8) and not r.clean(14, 20)


# -- ignores -------------------------------------------------------------------------------------------
def test_ignore_same_line_line_above_statement_and_file(tmp_path):
    text = ("-- gulliver: ignore-file GV002\n"
            "select *  -- gulliver: ignore GV001 we want every column\n"
            "from t limit 5;\n"
            "-- gulliver: ignore GV006\n"
            "select x\nfrom t\norder by x;\n"
            "select y\n"
            "-- gulliver: ignore GV011\n"
            ", z from t limit 1;\n")
    res = lint_file(_write(tmp_path / "q.sql", text), warn_unused_ignores=True)
    ignored = {(x.rule, x.ignored_by) for x in res.ignored}
    assert ("GV001", "line 2") in ignored
    assert ("GV006", "statement") in ignored
    assert ("GV002", "file") in ignored
    kept = [(x.rule, x.line) for x in res.findings]
    assert kept == [("GV901", 9)]   # GV011 is not there: the ignore is unused


def test_ignore_without_rules_ignores_everything_on_that_line(tmp_path):
    res = lint_file(_write(tmp_path / "q.sql", "select * from a, b  -- gulliver: ignore\n"))
    assert res.findings == [] and len(res.ignored) == 3


def test_unused_ignores_only_on_request(tmp_path):
    f = _write(tmp_path / "q.sql", "select x from t limit 1 -- gulliver: ignore GV001\n")
    assert lint_file(f).findings == []
    unused = lint_file(f, warn_unused_ignores=True).findings
    assert [x.rule for x in unused] == ["GV901"] and "GV001" in unused[0].message


# -- config and discovery ------------------------------------------------------------------------------
def test_lint_config_disable_severity_and_errors():
    cfg = lint_config_from_mapping({"disable": ["GV001", "unbounded-output"], "severity": {"GV006": "error"},
                                    "fail_on": "warn", "exclude": "target/"})
    assert cfg.disable == {"GV001", "GV002"} and cfg.severity == {"GV006": "error"} and cfg.exclude == ("target/",)
    with pytest.raises(ConfigError):
        lint_config_from_mapping({"disabled": ["GV001"]})
    with pytest.raises(ConfigError):
        lint_config_from_mapping({"disable": ["GV777x"]})
    with pytest.raises(ConfigError):
        lint_config_from_mapping({"severity": {"GV001": "fatal"}})


def test_unknown_rule_ids_are_refused():
    """GV099 (a typo for GV009) is well-formed but no rule uses it: it must not load and silently disable nothing."""
    with pytest.raises(ConfigError, match="unknown rule 'GV099'"):
        lint_config_from_mapping({"disable": ["GV099"]})
    with pytest.raises(ConfigError, match="unknown rule 'GV099'"):
        lint_config_from_mapping({"severity": {"GV099": "error"}})
    # any case, names and the two file-lint meta rules still load
    cfg = lint_config_from_mapping({"disable": ["gv001", "select-star", "GV900", "GV901"],
                                    "severity": {"gv006": "warn"}})
    assert cfg.disable == {"GV001", "GV900", "GV901"} and cfg.severity == {"GV006": "warn"}


def test_config_changes_findings_and_exit(tmp_path):
    f = _write(tmp_path / "q.sql", "select * from t order by 1\n")
    cfg = LintConfig(disable=frozenset({"GV001"}), severity={"GV006": "error"})
    res = lint_file(f, config=cfg)
    assert {x.rule: x.severity for x in res.findings} == {"GV002": "warn", "GV006": "error"}
    out = lint_paths([str(f)], config=cfg)
    assert out["status"] == "block" and out["failing"] == 1
    assert lint_paths([str(f)], config=cfg, fail_on="never")["status"] == "warn"


def test_folder_walk_with_exclude(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "models" / "a.sql", "select 1")
    _write(tmp_path / "models" / "staging" / "b.sql", "select 1")
    _write(tmp_path / "models" / "legacy" / "c.sql", "select 1")
    _write(tmp_path / "models" / "notes.md", "not sql")
    _write(tmp_path / "target" / "compiled" / "a.sql", "select 1")
    _write(tmp_path / ".venv" / "x.sql", "select 1")
    files, missing = discover(["."], exclude=["models/legacy"])
    assert [p.as_posix() for p in files] == ["models/a.sql", "models/staging/b.sql"]
    files, _ = discover(["models/legacy/c.sql", "models/a.sql"], exclude=["**/legacy/*"])
    assert [p.as_posix() for p in files] == ["models/a.sql"]
    _, missing = discover(["nope.sql"])
    assert missing == ["nope.sql"]


# -- output formats ------------------------------------------------------------------------------------
@pytest.fixture
def two_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "models" / "a.sql", "select *\nfrom events e, users u\n")
    _write(tmp_path / "models" / "b.sql", "select count(distinct user_id) from events\n")
    return lint_paths(["models"])


def test_sarif_structure(two_files):
    doc = json.loads(render(two_files, "sarif"))
    assert doc["version"] == "2.1.0" and doc["$schema"].endswith("sarif-2.1.0.json")
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    ids = [r["id"] for r in driver["rules"]]
    assert ids[:3] == ["GV001", "GV002", "GV003"] and "GV900" in ids
    assert all({"id", "shortDescription", "fullDescription", "helpUri", "defaultConfiguration"} <= set(r)
               for r in driver["rules"])
    levels = {r["ruleId"]: r["level"] for r in run["results"]}
    assert levels == {"GV001": "warning", "GV002": "warning", "GV003": "error", "GV007": "note"}
    first = run["results"][0]
    assert driver["rules"][first["ruleIndex"]]["id"] == first["ruleId"]
    loc = first["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "models/a.sql"
    assert loc["region"]["startLine"] >= 1 and loc["region"]["startColumn"] >= 1
    prints = [r["partialFingerprints"]["gulliver/v1"] for r in run["results"]]
    assert len(set(prints)) == len(prints)


def test_github_golden(two_files):
    lines = render(two_files, "github").splitlines()
    assert lines[0] == ("::warning file=models/a.sql,line=1,col=1,endLine=1,endColumn=9,title=GV002 unbounded-output::"
                        "The result has no LIMIT and no aggregation, so it can return every row. Fix: Add a LIMIT for "
                        "exploration, or aggregate (GROUP BY / COUNT) before returning rows.")
    assert lines[2].startswith("::error file=models/a.sql,line=2,col=14,endLine=2,endColumn=23,"
                               "title=GV003 cartesian-join::")
    assert lines[3].startswith("::notice file=models/b.sql,line=1,col=8,")
    assert lines[-1].startswith("gulliver lint: 2 file(s), 1 error, 2 warn, 1 info, 1 failing")


def test_gitlab_code_quality(two_files):
    issues = json.loads(render(two_files, "gitlab"))
    assert len(issues) == 4
    assert {i["severity"] for i in issues} == {"major", "minor", "info"}
    assert all(i["location"]["path"].startswith("models/") and i["location"]["lines"]["begin"] >= 1 for i in issues)
    assert len({i["fingerprint"] for i in issues}) == 4


def test_junit_one_suite_per_file(two_files):
    root = ET.fromstring(render(two_files, "junit"))
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["models/a.sql", "models/b.sql"]
    assert root.get("failures") == "1" and suites[0].get("failures") == "1"
    assert suites[0].find("testcase/failure").get("type") == "error"


def test_text_and_json(two_files):
    text = render(two_files, "text")
    assert "models/a.sql:2:14: error GV003 cartesian-join" in text
    assert "2 file(s), 2 statement(s): 1 error, 2 warn, 1 info" in text
    data = json.loads(render(two_files, "json"))
    assert data["status"] == "block" and data["findings"][0]["position"] == {"line": 1, "col": 1, "end_line": 1,
                                                                             "end_col": 9}
    assert "_results" not in data


def test_rules_registry_covers_every_rule():
    ids = [r["rule"] for r in rules()]
    assert ids[:12] == [f"GV{n:03d}" for n in range(1, 13)]
    assert all(r["description"] and r["default_severity"] in ("error", "warn", "info") for r in rules())
    from gulliver.lint import RULES
    from gulliver.lint_files import META_RULES
    from gulliver.lint_report import RULE_DOCS
    assert set(RULE_DOCS) == set(RULES) | set(META_RULES)


def _readme_lint_table() -> str:
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    marker = "## Scale lint rules\n\n"
    idx = text.index(marker) + len(marker)
    lines = text[idx:].splitlines()
    i = 0
    while i < len(lines) and not lines[i].startswith("|"):
        i += 1
    rows = []
    while i < len(lines) and lines[i].startswith("|"):
        rows.append(lines[i])
        i += 1
    return "\n".join(rows)


def test_readme_lint_table_matches_generated():
    assert _readme_lint_table() == render_rules_markdown()


# -- CLI -------------------------------------------------------------------------------------------------
def test_single_sql_lint_is_unchanged(state, capsys):
    sql = "select * from events e, users u"
    assert main(["lint", "--json", sql]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data == json.loads(json.dumps(core.lint_sql(sql, "duckdb"), default=str))
    assert "position" not in data["lint"][0]


def test_cli_files_formats_exit_codes_and_output(state, tmp_path, capsys):
    _write(tmp_path / "models" / "a.sql", "select x from t limit 5\n")
    _write(tmp_path / "models" / "b.sql", "select * from t\n")
    assert main(["lint", "models"]) == 0                                   # only warnings: passes
    assert main(["lint", "models", "--fail-on", "warn"]) == 2
    capsys.readouterr()
    assert main(["lint", "models", "--format", "sarif", "-o", "out.sarif"]) == 0
    assert json.loads((tmp_path / "out.sarif").read_text())["version"] == "2.1.0"
    assert "2 file(s), 2 statement(s)" in capsys.readouterr().out          # the text summary still prints
    assert main(["lint", "models/a.sql", "models/b.sql", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == 2
    assert main(["lint", "missing_dir", "models"]) == 1
    assert "Not found: missing_dir" in capsys.readouterr().out
    assert main(["rules", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"][0]["rule"] == "GV001"
    assert main(["rules", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"][0]["rule"] == "GV001"
    assert main(["rules", "--format", "markdown"]) == 0
    md = capsys.readouterr().out.strip()
    assert md == render_rules_markdown()
    assert md.startswith("| Rule | Name | Severity |")
    assert "| GV001 | select-star | warn |" in md


def test_cli_reads_lint_section_from_toml(state, tmp_path, capsys):
    _write(tmp_path / "gulliver.toml", '[lint]\nfail_on = "warn"\ndisable = ["GV002"]\nexclude = ["legacy"]\n')
    _write(tmp_path / "q.sql", "select x from t\n")
    _write(tmp_path / "legacy" / "old.sql", "select * from a, b\n")
    assert main(["lint", "."]) == 0                                        # GV002 disabled, legacy excluded
    _write(tmp_path / "q.sql", "select x from t order by x\n")
    assert main(["lint", "."]) == 2                                        # GV006 warn fails with fail_on=warn


def test_cli_refuses_an_unknown_rule_id_in_the_toml(state, tmp_path, capsys):
    _write(tmp_path / "gulliver.toml", '[lint]\ndisable = ["GV099"]\n')
    _write(tmp_path / "models" / "q.sql", "select * from t limit 1\n")
    assert main(["lint", "models"]) == 1
    assert "unknown rule 'GV099'" in capsys.readouterr().out


# -- fix --check / --write ---------------------------------------------------------------------------------
FIXABLE = ("-- daily report\n"
           "select e.user_id, count(*) as n  -- keep this comment\n"
           "from events e\n"
           "where DATE(e.ts) = '2025-01-03'\n"
           "group by 1;\n"
           "select count(*) from users;\n")


def test_fix_check_and_write_in_place(demo_db, state, tmp_path, capsys):
    f = _write(tmp_path / "models" / "daily.sql", FIXABLE)
    assert main(["--dsn", demo_db, "fix", "--check", "models"]) == 2
    out = capsys.readouterr().out
    assert "models/daily.sql:4:7: GV005 [returns exactly the same rows]" in out
    assert main(["--dsn", demo_db, "fix", "--write", "models", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["written"] == ["models/daily.sql"]
    new = f.read_text()
    assert new == FIXABLE.replace("DATE(e.ts) = '2025-01-03'", "e.ts >= '2025-01-03' AND e.ts < '2025-01-04'")
    assert main(["--dsn", demo_db, "fix", "--check", "models"]) == 0      # nothing left to fix


def test_fix_write_gives_the_same_rows(demo_db, state, tmp_path):
    import duckdb

    f = _write(tmp_path / "q.sql", FIXABLE.split(";")[0])
    before = FIXABLE.split(";")[0]
    assert main(["--dsn", demo_db, "fix", "--write", str(f)]) == 0
    con = duckdb.connect(demo_db, read_only=True)
    try:
        assert sorted(con.execute(before).fetchall()) == sorted(con.execute(f.read_text()).fetchall())
    finally:
        con.close()


def test_fix_without_metadata_finds_nothing(state, tmp_path, capsys):
    _write(tmp_path / "q.sql", FIXABLE)
    assert main(["fix", "--check", "q.sql", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fixable"] == 0


def test_fix_refuses_edits_inside_jinja(demo_db, state, tmp_path, capsys):
    f = _write(tmp_path / "m.sql", "select user_id from {{ ref('events') }}\n"
                                   "where DATE({{ ref('events') }}.ts) = '2025-02-01'\n")
    original = f.read_text()
    assert main(["--dsn", demo_db, "fix", "--write", "m.sql", "--json"]) == 0   # warn: one rewrite not placed
    data = json.loads(capsys.readouterr().out)
    assert data["fixable"] == 0 and data["unplaced"] == 1 and "Jinja" in data["fixes"][0]["notes"][0]
    assert f.read_text() == original
    _write(tmp_path / "m.sql", "select user_id from {{ ref('events') }}\nwhere DATE(ts) = '2025-02-01'\n")
    assert main(["--dsn", demo_db, "fix", "--write", "m.sql"]) == 0
    assert f.read_text() == ("select user_id from {{ ref('events') }}\n"
                             "where ts >= '2025-02-01' AND ts < '2025-02-02'\n")


def test_lint_metadata_adds_sarif_fixes(demo_db, state, tmp_path, capsys):
    _write(tmp_path / "q.sql", FIXABLE)
    assert main(["--dsn", demo_db, "lint", "q.sql", "--metadata", "--format", "sarif"]) == 0
    doc = json.loads(capsys.readouterr().out)
    fixed = [r for r in doc["runs"][0]["results"] if r.get("fixes")]
    assert len(fixed) == 1 and fixed[0]["ruleId"] == "GV005"
    change = fixed[0]["fixes"][0]["artifactChanges"][0]
    assert change["artifactLocation"]["uri"] == "q.sql"
    rep = change["replacements"][0]
    assert rep["deletedRegion"] == {"startLine": 4, "startColumn": 7, "endLine": 4, "endColumn": 32}
    assert rep["insertedContent"]["text"] == "e.ts >= '2025-01-03' AND e.ts < '2025-01-04'"
    assert "same rows" in fixed[0]["fixes"][0]["description"]["text"]


# -- GV015 fan trap ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql,hit", [
    ("select o.id, sum(o.shipping) from orders o join order_items i on i.order_id = o.id group by 1", True),
    ("select c.country, sum(c.credit) from orders o left join customers c on c.id = o.customer_id group by 1", True),
    ("select sum(i.price) from orders o join order_items i on i.order_id = o.id", False),          # many side
    ("select sum(o.shipping) from orders o join (select order_id, sum(price) p from order_items group by 1) i "
     "on i.order_id = o.id", False),                                                              # pre-aggregated
    ("select sum(o.shipping) from orders o join order_items i on i.order_id = o.order_id", False),  # naming unknown
    ("select i.id, sum(o.shipping) from orders o join order_items i on i.order_id = o.id group by i.id", False),
    ("select count(distinct o.id) from orders o join order_items i on i.order_id = o.id", False),
    ("select o.id, sum(o.shipping) from orders o join order_items i on i.order_id = o.id and i.n = 1 group by 1",
     False),                                                                                      # extra condition
    ("select count(u.id) from users u left join events e on u.id = e.user_id where e.user_id is null", False),
    ("select u.country, count(u.id) from users u left join events e on u.id = e.user_id "
     "where e.event_id is null and u.country <> 'NL' group by 1", False),                         # anti-join
    ("select count(u.id) from events e right join users u on u.id = e.user_id where e.user_id is null", False),
    ("select count(u.id) from users u left join events e on u.id = e.user_id where u.name is null", True),
])
def test_gv015_fan_trap(sql, hit):
    from gulliver.lint import lint
    from gulliver.sqlsafety import classify

    found = [f.rule for f in lint(classify(sql).select)]
    assert ("GV015" in found) is hit


def test_gv015_position_in_file(tmp_path):
    f = _write(tmp_path / "q.sql", "select o.id,\n       sum(o.shipping) as s\nfrom orders o\n"
                                   "join order_items i on i.order_id = o.id\ngroup by 1\n")
    gv = _by_rule(lint_file(f))["GV015"]
    assert (gv.line, gv.col, gv.severity) == (2, 8, "warn")


def test_other_dialects_and_odd_files(tmp_path):
    f = _write(tmp_path / "bq.sql", "select *\nfrom `proj.ds.events`\r\nwhere DATE(ts) = '2024-01-01';\n")
    found = _by_rule(lint_file(f, dialect="bigquery"))
    assert (found["GV005"].line, found["GV005"].col) == (3, 7) and found["GV001"].line == 1
    assert lint_file(_write(tmp_path / "empty.sql", "")).statements == 0
    assert lint_file(_write(tmp_path / "c.sql", "-- only a comment\n")).findings == []
    pg = lint_file(_write(tmp_path / "pg.sql", "select x::date from t limit 1;"), dialect="postgres")
    assert pg.findings == []


# -- regressions -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("tail", ["{% if is_incremental() %} and ts > (select max(ts) from {{ this }}){% endif %}",
                                  "{# keep this note #}"])
def test_fix_write_keeps_empty_jinja_right_after_the_edit(demo_db, state, tmp_path, capsys, tail):
    head = "select user_id from {{ ref('events') }}\nwhere DATE(ts) = '2025-01-03'"
    f = _write(tmp_path / "m.sql", head + tail + "\nlimit 10\n")
    assert main(["--dsn", demo_db, "fix", "--write", "m.sql", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["fixes"][0]["edits"][0]["before"] == "DATE(ts) = '2025-01-03'"
    assert f.read_text() == ("select user_id from {{ ref('events') }}\nwhere ts >= '2025-01-03' AND ts < '2025-01-04'"
                             + tail + "\nlimit 10\n")
    assert not any("not found" in n.lower() for n in data.get("notes") or [])   # {{ this }} is the model itself


def test_render_jinja_end_offsets_stay_before_an_empty_region():
    r = render_jinja("a{# x #}b")
    assert r.text == "ab"
    assert r.to_source(1) == 8 and r.to_source(1, end=True) == 1


@pytest.mark.parametrize("text,config", [
    ("-- gulliver: ignore GV005 we need DATE() here on purpose\n"
     "select user_id from events where DATE(ts) = '2025-01-03' limit 10;\n", None),
    ("-- gulliver: ignore-file\nselect user_id from events where DATE(ts) = '2025-01-03' limit 10;\n", None),
    ("select user_id from events where DATE(ts) = '2025-01-03' limit 10;\n", '[lint]\ndisable = ["GV005"]\n'),
])
def test_fix_respects_ignores_and_disable(demo_db, state, tmp_path, capsys, text, config):
    f = _write(tmp_path / "a.sql", text)
    if config:
        _write(tmp_path / "gulliver.toml", config)
    assert main(["--dsn", demo_db, "fix", "--check", "a.sql", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["fixes"] == []
    assert main(["--dsn", demo_db, "fix", "--write", "a.sql"]) == 0
    assert f.read_text() == text


@pytest.mark.parametrize("text", [
    "-- gulliver: ignore select_star\nselect * from events e, users u;\n",
    "select * from events e, users u; -- gulliver: ignore GV-001\n",
    "select * from events e, users u; -- gulliver: ignore GV0O1\n",
    "-- gulliver: ignore: GV001\nselect * from events e, users u;\n",
    "-- gulliver: ignore(GV001)\nselect * from events e, users u;\n",
])
def test_mistyped_ignore_ignores_nothing_and_is_reported(tmp_path, text):
    res = lint_file(_write(tmp_path / "a.sql", text))
    rules_found = {f.rule for f in res.findings}
    assert {"GV001", "GV002", "GV003", "GV901"} <= rules_found and res.ignored == []


def test_ignore_inside_a_string_literal_is_not_a_directive(tmp_path):
    res = lint_file(_write(tmp_path / "a.sql", "select 'see -- gulliver: ignore-file' as note, * from events e, users u;\n"))
    assert "GV003" in {f.rule for f in res.findings} and res.ignored == []
    # a bare directive and an explicit `all` still ignore every rule, and a reason after it is fine
    for d in ("-- gulliver: ignore", "-- gulliver: ignore all", "-- gulliver: ignore legacy report"):
        res = lint_file(_write(tmp_path / "b.sql", d + "\nselect * from events e, users u;\n"))
        assert res.findings == [] and len(res.ignored) == 3


def test_fix_write_keeps_crlf_and_bom(demo_db, state, tmp_path):
    text = "-- header\r\nselect user_id\r\nfrom events\r\nwhere DATE(ts) = '2025-01-03'\r\nlimit 10;\r\n"
    f = tmp_path / "a.sql"
    f.write_bytes(("\ufeff" + text).encode("utf-8"))
    assert main(["--dsn", demo_db, "fix", "--write", "a.sql"]) == 0
    assert f.read_bytes().decode("utf-8") == "\ufeff" + text.replace(
        "DATE(ts) = '2025-01-03'", "ts >= '2025-01-03' AND ts < '2025-01-04'")


def test_bom_file_is_linted(tmp_path):
    f = tmp_path / "a.sql"
    f.write_bytes(b"\xef\xbb\xbfselect * from a, b;\n")
    found = _by_rule(lint_file(f))
    assert "GV900" not in found and (found["GV001"].line, found["GV001"].col) == (1, 8)
    assert found["GV003"].severity == "error"


def test_fix_refuses_edits_that_would_drop_a_comment(demo_db, state, tmp_path, capsys):
    text = "select user_id from events\nwhere DATE(ts) /* the day we care about */ = '2025-01-03'\nlimit 10;\n"
    f = _write(tmp_path / "a.sql", text)
    assert main(["--dsn", demo_db, "fix", "--write", "a.sql", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["unplaced"] == 1 and "comment" in data["fixes"][0]["notes"][0]
    assert f.read_text() == text


def test_junit_is_well_formed_with_control_characters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "a.sql", "select user_id from events where event_type like '%a\x01b' limit 5;\n")
    xml = render(lint_paths(["a.sql"], fail_on="info"), "junit")
    ET.fromstring(xml)
    assert "\x01" not in xml


def test_missing_single_path_is_not_found(state, tmp_path, capsys):
    for value in ("models", "missing_dir/", "does/not/exist.sql"):
        assert main(["lint", value]) == 1
        assert f"Not found: {value}" in capsys.readouterr().out
    assert main(["lint", "select x from t limit 1"]) == 0               # one SQL string is still SQL


def test_fix_without_a_connection_says_tables_were_not_checked(state, tmp_path, capsys):
    _write(tmp_path / "a.sql", "select user_id from events where DATE(ts) = '2025-01-03' limit 3;\n")
    assert main(["fix", "--check", "a.sql", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "warn" and any("events" in n for n in data["notes"])


# -- baseline ----------------------------------------------------------------------------------------------
BAD = "select * from t limit 5\n"        # one finding: GV001


def _baseline_project(tmp_path):
    _write(tmp_path / "models" / "a.sql", BAD)
    _write(tmp_path / "models" / "sub" / "b.sql", "-- b\n" + BAD)
    return tmp_path / "gulliver-baseline.json"


def test_write_baseline_is_stable_json_without_sql_text(state, tmp_path, capsys):
    base = _baseline_project(tmp_path)
    assert main(["lint", "models", "--write-baseline", str(base)]) == 0
    first = base.read_text(encoding="utf-8")
    assert main(["lint", "models", "--write-baseline", str(base)]) == 0
    assert base.read_text(encoding="utf-8") == first
    doc = json.loads(first)
    assert doc["version"] == 1
    assert [(e["path"], e["rule"], e["count"]) for e in doc["entries"]] == [
        ("models/a.sql", "GV001", 1), ("models/sub/b.sql", "GV001", 1)]
    assert "limit" not in first and "select" not in first


def test_baseline_hides_old_findings_and_keeps_the_exit_code_clean(state, tmp_path, capsys):
    base = _baseline_project(tmp_path)
    main(["lint", "models", "--write-baseline", str(base)])
    capsys.readouterr()
    assert main(["lint", "models", "--fail-on", "warn"]) == 2                      # without the baseline: fails
    assert main(["lint", "models", "--fail-on", "warn", "--baseline", str(base)]) == 0
    out = lint_paths(["models"], baseline=str(base), fail_on="warn")
    assert out["status"] == "ok" and out["failing"] == 0 and out["findings"] == []
    assert out["baselined"] == 2 and out["baseline_fixed"] == 0 and out["ignored"] == 0
    assert {f["ignored_by"] for f in out["ignored_findings"]} == {"baseline"}
    for fmt in ("sarif", "gitlab", "github", "junit"):                              # left out of every format
        text = render(out, fmt)
        assert "GV001" not in text or fmt == "sarif"                                # sarif lists the rule metadata
    assert json.loads(render(out, "sarif"))["runs"][0]["results"] == []
    assert json.loads(render(out, "gitlab")) == []


def test_baseline_survives_moved_lines_but_not_copies_or_edits(state, tmp_path):
    base = _baseline_project(tmp_path)
    main(["lint", "models", "--write-baseline", str(base)])
    a = tmp_path / "models" / "a.sql"
    a.write_text("-- a new header\n-- and another\n" + BAD, encoding="utf-8")        # the line moved down
    assert lint_paths(["models"], baseline=str(base))["findings"] == []
    a.write_text(BAD + ";\n" + BAD, encoding="utf-8")                               # a second copy is new
    out = lint_paths(["models"], baseline=str(base))
    assert len(out["findings"]) == 1 and out["findings"][0]["path"] == "models/a.sql" and out["baselined"] == 2
    a.write_text("select *, y from t limit 5\n", encoding="utf-8")          # an edited line is new
    out = lint_paths(["models"], baseline=str(base))
    assert len(out["findings"]) == 1 and out["baseline_fixed"] == 1


def test_baseline_fixed_counts_only_linted_files(state, tmp_path, capsys):
    base = _baseline_project(tmp_path)
    main(["lint", "models", "--write-baseline", str(base)])
    (tmp_path / "models" / "a.sql").write_text("select x from t limit 5\n", encoding="utf-8")
    only_a = lint_paths(["models/a.sql"], baseline=str(base))
    assert only_a["baselined"] == 0 and only_a["baseline_fixed"] == 1               # b.sql was not linted
    everything = lint_paths(["models"], baseline=str(base))
    assert everything["baselined"] == 1 and everything["baseline_fixed"] == 1
    capsys.readouterr()
    assert main(["lint", "models", "--baseline", str(base)]) == 0
    assert "1 in baseline, 1 fixed: run --write-baseline to shrink it" in capsys.readouterr().out


def test_missing_or_invalid_baseline_is_an_error(state, tmp_path, capsys):
    _baseline_project(tmp_path)
    assert main(["lint", "models", "--baseline", "nope.json"]) == 1
    assert "Baseline file not found" in capsys.readouterr().out
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert main(["lint", "models", "--baseline", "bad.json"]) == 1
    assert "not valid JSON" in capsys.readouterr().out
    (tmp_path / "other.json").write_text('{"hello": 1}', encoding="utf-8")
    assert main(["lint", "models", "--baseline", "other.json"]) == 1
    assert "not a Gulliver baseline" in capsys.readouterr().out


def test_baseline_from_toml_is_relative_to_the_toml_and_works_from_a_subfolder(state, tmp_path, monkeypatch, capsys):
    _write(tmp_path / "gulliver.toml", '[lint]\nbaseline = "gulliver-baseline.json"\n')
    _baseline_project(tmp_path)
    assert main(["lint", "models", "--write-baseline"]) == 0                        # path from [lint] baseline
    assert (tmp_path / "gulliver-baseline.json").exists()
    capsys.readouterr()
    monkeypatch.chdir(tmp_path / "models")                                         # a different working folder
    assert main(["lint", ".", "--fail-on", "warn", "--config", str(tmp_path / "gulliver.toml")]) == 0
    assert "2 in baseline" in capsys.readouterr().out
    assert main(["lint", "sub/b.sql", "--baseline", "../gulliver-baseline.json", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["baselined"] == 1 and data["baseline_fixed"] == 0 and data["findings"] == []


def test_write_baseline_without_any_path_is_refused(state, tmp_path, capsys):
    _baseline_project(tmp_path)
    assert main(["lint", "models", "--write-baseline"]) == 1
    assert "--write-baseline needs a path" in capsys.readouterr().out


def test_lint_baseline_key_must_be_a_path():
    with pytest.raises(ConfigError):
        lint_config_from_mapping({"baseline": 3})


@pytest.mark.parametrize("sql", ["SELECT 1 FROM", "SELECT a b c FROM t", "SELECT 'abc"])
def test_file_syntax_errors_have_readable_messages(tmp_path, sql):
    result = lint_file(_write(tmp_path / "bad.sql", sql))
    finding, = [f for f in result.findings if f.rule == "GV900"]
    assert "line 1, column " in finding.message
    assert "<Token" not in finding.message and "TokenType" not in finding.message
    assert finding.fix == (
        "Fix the syntax error and try again. If the SQL is for another engine, set --dialect."
    )
    if sql == "SELECT 'abc":
        assert (finding.line, finding.col) == (1, len(sql) + 1)
        assert "end of input" in finding.message
