import asyncio
import json

from gulliver.cli import main
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


def test_cli_translate_output_file(tmp_path, capsys):
    out = tmp_path / "model.bq.sql"
    assert main(["translate", "--from", "duckdb", "--to", "bigquery", "-o", str(out), "--json",
                 "select my_udf(x), date_trunc('day', ts) from t"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]  # still printed
    assert out.read_text(encoding="utf-8") == data["sql"]



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
