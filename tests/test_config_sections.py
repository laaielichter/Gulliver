import json

import pytest

from gulliver.cli import main
from gulliver.config import ConfigError, load_settings
from gulliver.lint_files import load_lint_config
from gulliver.review import Policy, load_policy

CASES = [
    ("budget", "budget = {value}\n", load_settings, ["status"]),
    ("connections", "connections = {value}\n", load_settings, ["status"]),
    ("connections.local", "[connections]\nlocal = {value}\n", load_settings, ["status"]),
    ("approval", "approval = {value}\n", load_settings, ["status"]),
    ("lint", "lint = {value}\n", load_lint_config, ["lint", "SELECT 1"]),
    ("review", "review = {value}\n", load_policy, ["review", "--models", "models"]),
]


@pytest.mark.parametrize("section,template,loader,args", CASES)
@pytest.mark.parametrize("value", ["3", "0", "false", '\"\"', "[]", '[["ignored", 1]]'])
def test_non_table_sections_raise_config_error(tmp_path, section, template, loader, args, value):
    config = tmp_path / "bad.toml"
    config.write_text(template.format(value=value))
    with pytest.raises(ConfigError) as exc:
        loader(config)
    assert f"[{section}] must be a table" in str(exc.value)


@pytest.mark.parametrize("section,template,loader,args", CASES)
def test_cli_reports_bad_section_without_traceback(state, tmp_path, capsys, section, template, loader, args):
    config = tmp_path / "bad.toml"
    config.write_text(template.format(value="3"))
    assert main(["--config", str(config), "--json", *args]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "error"
    assert f"[{section}] must be a table" in result["error"]
    assert result["next_step"] == "Fix gulliver.toml or the flags."
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize("file_mode", [False, True])
def test_lint_reports_bad_section_with_explicit_dialect(state, tmp_path, capsys, file_mode):
    config = tmp_path / "bad.toml"
    config.write_text("lint = false\n")
    query = tmp_path / "query.sql"
    query.write_text("SELECT 1;")
    sql = str(query) if file_mode else "SELECT 1"
    assert main(["--config", str(config), "--json", "lint", sql, "--dialect", "duckdb"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert "[lint] must be a table" in result["error"]
    assert result["next_step"] == "Fix gulliver.toml or the flags."


def test_empty_and_inline_tables_keep_defaults(tmp_path):
    config = tmp_path / "valid.toml"
    config.write_text('connections = {}\nbudget = {}\napproval = {}\nlint = {}\nreview = {}\n')
    assert load_settings(config).budget.max_query_usd == 1.0
    assert load_settings(config).approval_channel == "cli"
    assert load_lint_config(config).fail_on == "error"
    assert load_policy(config).runs_per_day == Policy().runs_per_day


def test_inline_tables_keep_values_and_overrides(tmp_path):
    config = tmp_path / "valid.toml"
    config.write_text('connections = { local = { dsn = "duckdb://:memory:" } }\n'
                      'budget = { max_query_usd = 2.5 }\napproval = { channel = "cli" }\n'
                      'lint = { fail_on = "warn" }\nreview = { runs_per_day = 2 }\n')
    settings = load_settings(config, budget_overrides={"max_query_usd": 3.0})
    assert settings.connection("local").params["path"] == ":memory:"
    assert settings.budget.max_query_usd == 3.0
    assert load_lint_config(config).fail_on == "warn"
    assert load_policy(config, {"runs_per_day": 4}).runs_per_day == 4
