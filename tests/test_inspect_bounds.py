import json

import pytest

from gulliver import core, guard
from gulliver.cli import main


@pytest.mark.parametrize("limit", [-1, 0, 201, 500])
@pytest.mark.parametrize("sensitive", [False, True])
def test_invalid_inspect_limit_is_refused(settings, monkeypatch, capsys, limit, sensitive):
    def unexpected_engine(*args, **kwargs):
        pytest.fail("invalid bounds must be refused before opening an engine")

    monkeypatch.setattr(core, "_engine", unexpected_engine)
    inspect = guard.inspect_sensitive if sensitive else core.inspect
    result = inspect(settings, limit=limit)
    assert result["status"] == "error"
    assert "--limit" in result["error"]
    assert result["next_step"]

    args = ["inspect", "--limit", str(limit), "--json"]
    if sensitive:
        args.append("--sensitive")
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


@pytest.mark.parametrize("scan", [-5, -1, 1001, 5000])
def test_invalid_inspect_scan_is_refused(settings, monkeypatch, capsys, scan):
    def unexpected_engine(*args, **kwargs):
        pytest.fail("invalid scan must be refused before opening an engine")

    monkeypatch.setattr(core, "_engine", unexpected_engine)
    result = guard.inspect_sensitive(settings, scan_rows=scan)
    assert result["status"] == "error"
    assert "--scan" in result["error"]
    assert result["next_step"]
    assert main(["inspect", "--sensitive", "--scan", str(scan), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


@pytest.mark.parametrize("limit", [1, 200])
def test_inspect_accepts_limit_boundaries(settings, limit):
    assert core.inspect(settings, limit=limit)["status"] == "ok"
    assert guard.inspect_sensitive(settings, limit=limit)["status"] == "ok"


@pytest.mark.parametrize("scan", [0, 1000])
def test_inspect_accepts_scan_boundaries(settings, scan):
    result = guard.inspect_sensitive(settings, table="users", scan_rows=scan)
    assert result["status"] == "ok"
    assert result["scanned_rows"] == scan


@pytest.mark.parametrize("sensitive", [False, True])
def test_inspect_ignores_limit_for_named_table(settings, demo_db, capsys, sensitive):
    inspect = guard.inspect_sensitive if sensitive else core.inspect
    assert inspect(settings, table="users", limit=-1)["status"] == "ok"
    args = ["--dsn", demo_db, "inspect", "users", "--limit", "-1", "--json"]
    if sensitive:
        args.append("--sensitive")
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
