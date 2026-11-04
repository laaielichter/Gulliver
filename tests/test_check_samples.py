"""Opt-in examples of failed checks, queried and guarded like other results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import duckdb
import pytest

from gulliver import features
from gulliver.cli import main
from gulliver.config import load_settings
from gulliver.engines.base import EngineError
from gulliver.engines.duckdb_engine import DuckDBEngine
from gulliver.mcp_server import build_server
from gulliver.receipts import render_markdown


@pytest.fixture
def sample_settings(tmp_path, state):
    db = tmp_path / "checks.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE items AS SELECT * FROM (VALUES "
                    "(1, 'A', 'cart', -1), (1, 'A', 'cart', -1), (1, 'A', 'cart', 10), "
                    "(2, 'B', 'buy', 200), (2, 'B', 'buy', 200), "
                    "(NULL, NULL, 'view', 20), (NULL, NULL, 'view', 20), "
                    "(NULL, 'B', 'view', NULL)) t(user_id, region, kind, amount)")
    return load_settings(dsn=str(db))


def outcome(result, name):
    return next(c for c in result["checks"] if c["check"] == name)


def test_samples_values_counts_and_filter(sample_settings):
    result = features.check(sample_settings, "items", unique=["user_id"], accepted=["kind=view"],
                            ranges=["amount=0:100"], samples=2)
    assert result["status"] == "fail"
    assert result["untrusted"] is True
    assert outcome(result, "unique")["failing_rows"] == 3
    assert outcome(result, "unique")["samples"] == [
        {"values": {"user_id": 1}, "rows": 3}, {"values": {"user_id": 2}, "rows": 2}]
    assert outcome(result, "accepted_values")["samples"] == [
        {"values": {"kind": "cart"}, "rows": 3}, {"values": {"kind": "buy"}, "rows": 2}]
    assert {s["values"]["amount"] for s in outcome(result, "range")["samples"]} == {-1, 200}
    assert all(s["rows"] == 2 for s in outcome(result, "range")["samples"])
    filtered = features.check(sample_settings, "SELECT * FROM items", where="user_id = 2",
                              unique=["user_id"], samples=1)
    assert outcome(filtered, "unique")["samples"] == [{"values": {"user_id": 2}, "rows": 2}]



@pytest.mark.parametrize("where", ["user_id = 2 -- selected duplicates", "user_id = 2 /* selected duplicates */"])
def test_sample_filter_comments_preserve_all_check_predicates(sample_settings, where):
    result = features.check(sample_settings, "items", where=where, unique=["user_id"],
                            accepted=["kind=view"], ranges=["amount=0:100"], samples=1)
    assert result["status"] == "fail" and result["failed"] == 3
    assert outcome(result, "unique")["failing_rows"] == 1
    assert outcome(result, "unique")["samples"] == [{"values": {"user_id": 2}, "rows": 2}]
    assert outcome(result, "accepted_values")["samples"] == [{"values": {"kind": "buy"}, "rows": 2}]
    assert outcome(result, "range")["samples"] == [{"values": {"amount": 200}, "rows": 2}]


def test_composite_samples_keep_null_keys(sample_settings):
    result = features.check(sample_settings, "items", unique=["user_id,region"], samples=20)
    samples = outcome(result, "unique")["samples"]
    assert len(samples) == 3
    assert {"values": {"user_id": None, "region": None}, "rows": 2} in samples
    assert all(set(s["values"]) == {"user_id", "region"} for s in samples)


@pytest.mark.parametrize("samples,unique,expected_queries", [(0, ["user_id"], 1), (5, ["amount"], 2),
                                                           (5, ["region,amount"], 2), (5, [], 1)])
def test_only_failed_supported_checks_query(sample_settings, monkeypatch, samples, unique, expected_queries):
    calls = []
    original = features._gated_run

    def run(*args, **kwargs):
        calls.append(args[3])
        return original(*args, **kwargs)

    monkeypatch.setattr(features, "_gated_run", run)
    result = features.check(sample_settings, "items", unique=unique, not_null=["amount"], min_rows=100, samples=samples)
    assert result["status"] == "fail"
    assert len(calls) == expected_queries
    assert all("samples" not in c for c in result["checks"] if c["check"] in ("not_null", "row_count"))


def test_passing_checks_do_not_query_samples(sample_settings, monkeypatch):
    calls = []
    original = features._gated_run

    def run(*args, **kwargs):
        calls.append(args[3])
        return original(*args, **kwargs)

    monkeypatch.setattr(features, "_gated_run", run)
    result = features.check(sample_settings, "items", accepted=["kind=view|cart|buy"], ranges=["amount=-2:300"], samples=20)
    assert result["status"] == "ok"
    assert len(calls) == 1


def test_unsupported_failures_do_not_query_samples(sample_settings, monkeypatch):
    calls = []
    original = features._gated_run

    def run(*args, **kwargs):
        calls.append(args[3])
        return original(*args, **kwargs)

    monkeypatch.setattr(features, "_gated_run", run)
    result = features.check(sample_settings, "SELECT CAST(NULL AS TIMESTAMP) AS seen FROM items",
                            not_null=["seen"], fresh=["seen=1h"], min_rows=100, samples=20)
    assert result["failed"] == 3
    assert len(calls) == 1
    assert all("samples" not in c for c in result["checks"])


def test_sample_count_alias_does_not_replace_a_key(sample_settings):
    result = features.check(sample_settings, "SELECT user_id AS gv_sample_count FROM items",
                            unique=["gv_sample_count"], samples=1)
    assert outcome(result, "unique")["samples"] == [{"values": {"gv_sample_count": 1}, "rows": 3}]


def test_one_sample_error_keeps_other_samples_and_counts(sample_settings, monkeypatch):
    execute = DuckDBEngine.execute

    def run(engine, sql, **kwargs):
        if "GROUP BY" in sql and '"user_id"' in sql:
            raise EngineError("Sample unavailable")
        return execute(engine, sql, **kwargs)

    monkeypatch.setattr(DuckDBEngine, "execute", run)
    result = features.check(sample_settings, "items", unique=["user_id"], accepted=["kind=view"], samples=1)
    assert result["status"] == "fail" and result["failed"] == 2
    assert outcome(result, "unique")["samples_skipped"]["status"] == "error"
    assert outcome(result, "unique")["failing_rows"] == 3
    assert outcome(result, "accepted_values")["samples"] == [{"values": {"kind": "cart"}, "rows": 3}]


@pytest.mark.parametrize("action", ["hash", "mask", "deny"])
def test_samples_follow_column_policy_and_receipts(sample_settings, action):
    conn = sample_settings.connection(None)
    # Qualified policy names require the same source lineage as ordinary run previews.
    protected = replace(conn, params={**conn.params, "policy": {"columns": {"items.user_id": action}}})
    settings = replace(sample_settings, connections={conn.name: protected})
    result = features.check(settings, "items", unique=["user_id"], samples=2)
    check = outcome(result, "unique")
    assert result["status"] == "fail"
    assert check["failing_rows"] == 3
    if action == "deny":
        assert "samples_skipped" in check and "samples" not in check
    else:
        values = [s["values"]["user_id"] for s in check["samples"]]
        assert all(isinstance(v, str) for v in values)
        assert values != ["1", "2"]
        assert result["untrusted"] is True
        receipt = render_markdown(result)
        assert "Sample values" in receipt
        assert "'user_id': 1" not in receipt


def test_renamed_source_columns_are_guarded_before_saving(sample_settings):
    conn = sample_settings.connection(None)
    protected = replace(conn, params={**conn.params, "policy": {"columns": {"items.kind": "hash"}}})
    settings = replace(sample_settings, connections={conn.name: protected})
    result = features.check(settings, "SELECT kind AS event FROM items", accepted=["event=view"], samples=2)
    check = outcome(result, "accepted_values")
    assert check["failing_rows"] == 5
    assert [s["rows"] for s in check["samples"]] == [3, 2]
    values = [s["values"]["event"] for s in check["samples"]]
    assert all(v not in ("cart", "buy") for v in values)
    receipts = list(settings.state_dir.glob("receipts/*.json"))
    assert receipts
    saved = receipts[-1].read_text()
    assert '"cart"' not in saved and '"buy"' not in saved
    assert json.loads(saved)["checks"] == result["checks"]
    ledger = [json.loads(line) for line in (settings.state_dir / "ledger.jsonl").read_text().splitlines()]
    assert [entry["kind"] for entry in ledger] == ["check", "check_samples"]


@pytest.mark.parametrize("phase", ["estimate", "execute"])
def test_sample_provider_errors_are_guarded_before_saving(sample_settings, monkeypatch, phase):
    conn = sample_settings.connection(None)
    protected = replace(conn, params={**conn.params, "policy": {"columns": {"items.user_id": "hash"}}})
    settings = replace(sample_settings, connections={conn.name: protected})
    provider = getattr(DuckDBEngine, phase)
    sensitive = "person@example.com"

    def fail_sample(engine, sql, **kwargs):
        if "GROUP BY" in sql:
            raise EngineError(f"Provider rejected value '{sensitive}'")
        return provider(engine, sql, **kwargs)

    monkeypatch.setattr(DuckDBEngine, phase, fail_sample)
    result = features.check(settings, "items", unique=["user_id"], samples=2)
    check = outcome(result, "unique")
    assert result["status"] == "fail" and check["failing_rows"] == 3
    assert check["samples_skipped"]["status"] == "error"
    assert sensitive not in json.dumps(result)
    for receipt in settings.state_dir.glob("receipts/*.json"):
        assert sensitive not in receipt.read_text()


@pytest.mark.parametrize("cost,status", [(2, "block"), (0.75, "needs_approval")])
def test_sample_budget_failure_keeps_check_result(sample_settings, monkeypatch, cost, status):
    settings = replace(sample_settings, budget=replace(sample_settings.budget, max_query_usd=1, approval_over_usd=0.5))
    estimate = DuckDBEngine.estimate

    def priced(engine, sql):
        result = estimate(engine, sql)
        return replace(result, cost_usd=cost if "GROUP BY" in sql else 0)

    monkeypatch.setattr(DuckDBEngine, "estimate", priced)
    result = features.check(settings, "items", unique=["user_id"], samples=2)
    check = outcome(result, "unique")
    assert result["status"] == "fail" and check["failing_rows"] == 3
    assert check["samples_skipped"]["status"] == status
    assert "samples" not in check


@pytest.mark.parametrize("samples", [-1, 21, True, 1.5, "2"])
def test_samples_bounds_checked_before_queries(sample_settings, monkeypatch, samples):
    def no_query(*args, **kwargs):
        pytest.fail("invalid sample count must not reach a database")

    monkeypatch.setattr(features, "_engine", no_query)
    assert features.check(sample_settings, "items", unique=["user_id"], samples=samples)["status"] == "error"


def test_cli_and_mcp_samples_contract(sample_settings, capsys):
    db = sample_settings.connection(None).params["path"]
    assert main(["--dsn", db, "--json", "check", "items", "--unique", "user_id", "--samples", "1"]) == 4
    result = json.loads(capsys.readouterr().out)
    assert len(outcome(result, "unique")["samples"]) == 1
    tools = asyncio.run(build_server().list_tools())
    tool = next(t for t in tools if t.name == "gulliver_check")
    samples = tool.model_dump(by_alias=True)["inputSchema"]["properties"]["samples"]
    assert samples["default"] == 0 and samples["minimum"] == 0 and samples["maximum"] == 20


def test_mcp_client_receives_guarded_samples(sample_settings, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver.resolve")
    from mcp import Client

    monkeypatch.setenv("GULLIVER_DSN", sample_settings.connection(None).params["path"])

    async def call():
        async with Client(build_server()) as client:
            return await client.call_tool("gulliver_check", {"target": "items", "unique": ["user_id"], "samples": 1})

    result = asyncio.run(call()).structured_content
    assert result["status"] == "fail" and result["untrusted"] is True
    assert outcome(result, "unique")["samples"] == [{"values": {"user_id": 1}, "rows": 3}]
