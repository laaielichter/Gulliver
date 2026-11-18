import asyncio
import json

import pytest

from gulliver.cli import main
from gulliver.demo import MAX_EVENTS, build_demo
from gulliver.mcp_server import build_server, export_tools

TOOLS = {"gulliver_status", "gulliver_inspect", "gulliver_lint", "gulliver_preflight", "gulliver_rehearse",
         "gulliver_run", "gulliver_receipt", "gulliver_impact", "gulliver_drift", "gulliver_translate",
         "gulliver_diff", "gulliver_incremental", "gulliver_check", "gulliver_fix", "gulliver_top",
         "gulliver_review"}


def test_cli_status_json_after_command(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"


def test_cli_status_unknown_connection_refused(demo_db, state, capsys):
    assert main(["-c", "nosuch", "status", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert "Unknown connection 'nosuch'" in data["error"]
    assert "Known connections: default" in data["error"]


def test_cli_status_known_connection_ok(demo_db, state, capsys):
    assert main(["-c", "default", "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"


def test_cli_exit_codes(demo_db, state, capsys):
    assert main(["lint", "select e.id from events e, users u"]) == 2
    assert main(["--dsn", demo_db, "preflight", "--json", "select count(*) from events"]) == 0
    assert main(["--dsn", demo_db, "run", "delete from events"]) == 2


@pytest.mark.parametrize("limit", [-5, 0])
def test_cli_invalid_limit_returns_a_clean_error(state, capsys, limit):
    assert main(["fix", "--mode", "all", "--dialect", "duckdb", "--limit", str(limit),
                 "--json", "SELECT * FROM events"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert str(limit) in result["error"] and "limit" in result["error"]
    assert result["next_step"]


def test_cli_sql_from_file(demo_db, state, tmp_path, capsys):
    q = tmp_path / "q.sql"
    q.write_text("select country, count(*) from users group by 1")
    assert main(["--dsn", demo_db, "--json", "run", "-f", str(q)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["actual"]["rows"] == 8


def test_tool_export_formats():
    mcp_tools = export_tools("mcp")
    assert {t["name"] for t in mcp_tools} == TOOLS
    openai = export_tools("openai")
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in openai)
    gemini = export_tools("gemini")
    text = json.dumps(gemini)
    assert "anyOf" not in text and "title" not in json.dumps(gemini["function_declarations"][0]["parameters"])


def test_mcp_server_lists_and_calls_tools(demo_db, state, monkeypatch):
    monkeypatch.setenv("GULLIVER_DSN", demo_db)
    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == TOOLS
    result = asyncio.run(server.call_tool("gulliver_lint", {"sql": "select * from events"}))
    text = json.dumps(result if isinstance(result, (dict, list)) else
                      (result.model_dump() if hasattr(result, "model_dump") else str(result)), default=str)
    assert "GV001" in text


def test_cli_receipt_output_file(demo_db, state, tmp_path, capsys):
    assert main(["--dsn", demo_db, "preflight", "select count(*) from events"]) == 0
    md = tmp_path / "receipt.md"
    assert main(["--dsn", demo_db, "receipt", "latest", "--format", "markdown", "-o", str(md)]) == 0
    assert "select count(*) from events" in md.read_text(encoding="utf-8").lower()
    js = tmp_path / "receipt.json"
    assert main(["--dsn", demo_db, "receipt", "latest", "--format", "json", "-o", str(js)]) == 0
    assert json.loads(js.read_text(encoding="utf-8"))["receipt_id"].startswith("gv-")


def test_cli_empty_check_explains_no_validation(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "check", "users", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result.get("checks_requested") == 0
    assert "No checks were requested" in result["next_step"]


def test_cli_receipt_list_order_limit_and_status(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "preflight", "select count(*) from events"]) == 0
    capsys.readouterr()
    assert main(["--dsn", demo_db, "preflight", "select count(*) from users"]) == 0
    capsys.readouterr()

    assert main(["--dsn", demo_db, "receipt", "--list", "--limit", "1"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line and not line.startswith("next:")]
    assert len(lines) == 1
    assert "select count(*) from users" in lines[0].lower()

    assert main(["--dsn", demo_db, "receipt", "--list", "--status", "ok"]) == 0
    output = capsys.readouterr().out
    assert "select count(*) from users" in output.lower()
    assert "select count(*) from events" in output.lower()


def test_cli_receipt_list_json_has_summary_only(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "preflight", "select count(*) from events"]) == 0
    capsys.readouterr()
    assert main(["--dsn", demo_db, "receipt", "--list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["receipts"]
    assert {"receipt_id", "created_at", "action", "status", "connection", "sql_preview"} <= set(data["receipts"][0])
    assert "preview" not in data["receipts"][0]
    assert "next_step" in data


def test_cli_receipt_list_empty_store_messaging(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["receipt", "--list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("No receipts yet.\n")
    assert "next: Run gulliver preflight, rehearse or run first; each writes a receipt." in out

    assert main(["receipt", "--list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["receipts"] == []
    assert data["message"] == "No receipts yet."
    assert data["next_step"] == "Run gulliver preflight, rehearse or run first; each writes a receipt."


def test_cli_receipt_list_unknown_status_lists_existing_statuses(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "preflight", "select count(*) from events"]) == 0
    capsys.readouterr()

    assert main(["--dsn", demo_db, "receipt", "--list", "--status", "bogus"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("No receipts with status 'bogus'.\n")
    assert "next: Statuses in your receipts: ok." in out

    assert main(["--dsn", demo_db, "receipt", "--list", "--status", "bogus", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["receipts"] == []
    assert data["message"] == "No receipts with status 'bogus'."
    assert data["next_step"] == "Statuses in your receipts: ok."


def test_cli_translate_output_file(tmp_path, capsys):
    out = tmp_path / "model.bq.sql"
    assert main(["translate", "--from", "duckdb", "--to", "bigquery", "-o", str(out), "--json",
                 "select my_udf(x), date_trunc('day', ts) from t"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]  # still printed
    assert out.read_text(encoding="utf-8") == data["sql"]



@pytest.mark.parametrize("extra, message", [
    (["--min-rows", "-5"], "min_rows"),
    (["--max-change", "-5"], "max_change"),
    (["--max-change", "nan"], "max_change"),
    (["--max-change", "inf"], "max_change"),
])
def test_check_refuses_invalid_thresholds(demo_db, capsys, extra, message):
    code = main(["--dsn", demo_db, "--json", "check", "events", "--not-null", "event_id", *extra])
    assert code == 1
    assert message in capsys.readouterr().out


def test_check_accepts_zero_min_rows(demo_db):
    assert main(["--dsn", demo_db, "--json", "check", "events", "--not-null", "event_id", "--min-rows", "0"]) == 0



@pytest.mark.parametrize("command", ["lint", "preflight", "rehearse", "run"])
@pytest.mark.parametrize("sql", ["SELECT 1 FROM", "SELECT a b c FROM t", "SELECT 'abc"])
def test_cli_syntax_errors_stay_blocked_with_actionable_advice(command, sql, demo_db, state, capsys):
    assert main(["--dsn", demo_db, command, "--json", sql]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "block"
    assert result["next_step"] == (
        "Fix the syntax error and try again. If the SQL is for another engine, set --dialect."
    )
    reason = result["statement"]["reason"]
    assert "line 1, column " in reason
    assert "<Token" not in reason and "TokenType" not in reason


@pytest.mark.parametrize("sql", ["DROP TABLE t", "SELECT 1; SELECT 2"])
def test_cli_non_syntax_blocks_keep_existing_advice(sql, demo_db, state, capsys):
    for command in ("lint", "preflight", "rehearse", "run"):
        assert main(["--dsn", demo_db, command, "--json", sql]) == 2
        result = json.loads(capsys.readouterr().out)
        if command == "lint":
            assert result["next_step"] == "Send a single SELECT (or CREATE/INSERT ... SELECT)."
        elif command == "rehearse":
            assert result["next_step"] == result["statement"]["reason"]
        else:
            assert result["next_step"] == (
                "Rewrite it as one SELECT (or CREATE/INSERT ... SELECT). " + result["statement"]["reason"]
            )



def test_mcp_check_by_name_and_status_checks(demo_db, state, tmp_path, monkeypatch):
    db_path = demo_db.replace("\\", "/")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text(
        f'''default_connection = "demo"
[connections.demo]
engine = "duckdb"
path = "{db_path}"

[checks.ev]
target = "events"
unique = ["event_id"]
min_rows = 100
'''
    )
    server = build_server()
    # 1. Status lists configured checks
    status_res = asyncio.run(server.call_tool("gulliver_status", {}))
    st = status_res if isinstance(status_res, dict) else (
        status_res.model_dump() if hasattr(status_res, "model_dump") else json.loads(str(status_res))
    )
    # Extract dict if inside content
    data = st
    if hasattr(status_res, "structuredContent") and status_res.structuredContent:
        data = status_res.structuredContent
    elif isinstance(st, dict) and "checks" in st:
        data = st
    elif hasattr(status_res, "content") and status_res.content:
        data = json.loads(status_res.content[0].text)
    assert "ev" in data.get("checks", [])

    # 2. gulliver_check runs configured check by name
    check_res = asyncio.run(server.call_tool("gulliver_check", {"name": "ev"}))
    cr = check_res if isinstance(check_res, dict) else (
        check_res.model_dump() if hasattr(check_res, "model_dump") else json.loads(str(check_res))
    )
    if hasattr(check_res, "structuredContent") and check_res.structuredContent:
        cr_data = check_res.structuredContent
    elif isinstance(cr, dict) and "status" in cr:
        cr_data = cr
    elif hasattr(check_res, "content") and check_res.content:
        cr_data = json.loads(check_res.content[0].text)
    assert cr_data.get("status") == "ok"
    assert cr_data.get("target") == "events"

    # 3. Unknown check name returns error
    err_res = asyncio.run(server.call_tool("gulliver_check", {"name": "missing"}))
    if hasattr(err_res, "structuredContent") and err_res.structuredContent:
        err_data = err_res.structuredContent
    elif isinstance(err_res, dict) and "status" in err_res:
        err_data = err_res
    elif hasattr(err_res, "content") and err_res.content:
        err_data = json.loads(err_res.content[0].text)
    assert err_data.get("status") == "error"
    assert "Unknown configured check" in err_data.get("error", "")


def test_cli_run_refuses_negative_max_rows_and_allows_zero(demo_db, state, capsys):
    assert main(["--dsn", demo_db, "run", "--max-rows", "-5", "--json", "select count(*) from users"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error" and "-5" in data["error"] and data["next_step"]

    assert main(["--dsn", demo_db, "run", "--max-rows", "-2", "select count(*) from users"]) == 1
    plain = json.loads(capsys.readouterr().out)
    assert plain["status"] == "error" and "-2" in plain["error"]

    assert main(["--dsn", demo_db, "run", "--max-rows", "0", "--json", "select count(*) from users"]) == 0
    zero = json.loads(capsys.readouterr().out)
    assert zero["status"] == "ok" and zero["preview"] == [] and zero["actual"]["rows"] == 1


def test_build_demo_refuses_invalid_events_and_creates_no_file(tmp_path):
    for bad in (-5, 0):
        target = tmp_path / f"bad_{bad}.duckdb"
        res = build_demo(target, events=bad)
        assert res["status"] == "error"
        assert "--events" in res["error"] and res["next_step"]
        assert not target.exists()

    over = tmp_path / "over_limit.duckdb"
    res = build_demo(over, events=MAX_EVENTS + 1)
    assert res["status"] == "error"
    assert f"{MAX_EVENTS:,}" in res["error"] and res["next_step"]
    assert not over.exists()


def test_cli_demo_refuses_invalid_events(tmp_path, capsys):
    for bad in (-5, 0, MAX_EVENTS + 1):
        target = tmp_path / f"cli_{bad}.duckdb"
        assert main(["demo", str(target), "--events", str(bad), "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error" and "--events" in data["error"] and data["next_step"]
        assert not target.exists()

    plain_target = tmp_path / "cli_plain_bad.duckdb"
    assert main(["demo", str(plain_target), "--events", "-1"]) == 1
    plain_data = json.loads(capsys.readouterr().out)
    assert plain_data["status"] == "error" and "--events" in plain_data["error"]
    assert not plain_target.exists()

    ok_target = tmp_path / "ok.duckdb"
    res = build_demo(ok_target, events=10, users=5)
    assert res["status"] == "ok" and res["created"] is True and res["tables"] == {"events": 10, "users": 5}
    assert ok_target.exists()


def test_cli_rehearse_options_validation(demo_db, state, capsys):
    # 1. Non-numeric --fractions names the option and format
    exit_code = main(["--dsn", demo_db, "rehearse", "--fractions", "abc", "select count(*) from events"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "--fractions must be comma-separated numbers between 0 and 1" in out
    assert "(got 'abc')" in out

    # 2. Empty or malformed --fractions with --json
    exit_code = main(["--dsn", demo_db, "--json", "rehearse", "--fractions", "0.01,,0.1", "select count(*) from events"])
    assert exit_code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert "--fractions must be comma-separated numbers between 0 and 1" in data["error"]
    assert data["next_step"]

    # 3. Negative --seed refused with clear message
    exit_code = main(["--dsn", demo_db, "rehearse", "--seed", "-1", "select count(*) from events"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "--seed must be 0 or more" in out

    # 4. Valid --fractions and --seed succeed
    exit_code = main(["--dsn", demo_db, "rehearse", "--fractions", "0.01,0.03", "--seed", "7", "select count(*) from events"])
    assert exit_code == 0


def test_cli_spend_refuses_a_huge_days_but_keeps_long_windows(demo_db, state, capsys):
    for days in ("1000000000", "99999999999"):
        assert main(["--dsn", demo_db, "spend", "--days", days, "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error" and "--days must be between 1 and" in data["error"] and data["next_step"]
    assert main(["--dsn", demo_db, "spend", "--days", "400", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["days"] == 400
