import asyncio
import json
import os

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


def test_cli_exit_codes(demo_db, state, capsys):
    assert main(["lint", "select e.id from events e, users u"]) == 2
    assert main(["--dsn", demo_db, "preflight", "--json", "select count(*) from events"]) == 0
    assert main(["--dsn", demo_db, "run", "delete from events"]) == 2


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


def test_cli_translate_output_file(tmp_path, capsys):
    out = tmp_path / "model.bq.sql"
    assert main(["translate", "--from", "duckdb", "--to", "bigquery", "-o", str(out), "--json",
                 "select my_udf(x), date_trunc('day', ts) from t"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]  # still printed
    assert out.read_text(encoding="utf-8") == data["sql"]


def test_cli_demo_refuses_events_below_one(state, tmp_path, capsys):
    target = str(tmp_path / "never.duckdb")
    for value in ("-5", "0"):
        assert main(["demo", target, "--events", value, "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error"
        assert value in data["error"]
        assert "1 or more" in data["error"]
        assert data["next_step"]
    assert not os.path.exists(target)


def test_cli_demo_refuses_events_above_the_limit(state, tmp_path, capsys):
    target = str(tmp_path / "never.duckdb")
    assert main(["demo", target, "--events", str(MAX_EVENTS + 1), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert f"{MAX_EVENTS:,}" in data["error"]
    assert not os.path.exists(target)


def test_cli_demo_builds_inside_the_limits(state, tmp_path, capsys):
    target = str(tmp_path / "small.duckdb")
    assert main(["demo", target, "--events", "50", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["created"] is True
    assert data["tables"]["events"] == 50


def test_build_demo_refuses_events_below_one(tmp_path):
    target = str(tmp_path / "never.duckdb")
    for value in (-5, 0):
        result = build_demo(target, events=value)
        assert result["status"] == "error"
        assert "1 or more" in result["error"]
        assert str(value) in result["error"]
    assert not os.path.exists(target)


def test_build_demo_refuses_events_above_the_limit(tmp_path):
    target = str(tmp_path / "never.duckdb")
    result = build_demo(target, events=MAX_EVENTS + 1)
    assert result["status"] == "error"
    assert f"{MAX_EVENTS:,}" in result["error"]
    assert not os.path.exists(target)
