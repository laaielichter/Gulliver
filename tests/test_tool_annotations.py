"""MCP tool annotations must match what each tool's description says it does."""
import asyncio
import re

from mcp import Client

from gulliver.lint import RULES
from gulliver.mcp_server import build_server

WRITE_WORDS = re.compile(r"(?<!never )(?<!not )\b(saves?|writes?|replaces?|deletes?|drops?|stores?)\b",
                         re.IGNORECASE)


def _tools():
    async def go():
        async with Client(build_server()) as client:
            return await client.list_tools()
    result = asyncio.run(go())
    return {t.name: t for t in getattr(result, "tools", result)}


def _hints(tool):
    """Annotations as the wire names (readOnlyHint, ...), whatever the SDK's field names are."""
    return tool.annotations.model_dump(by_alias=True) if tool.annotations else {}


def test_drift_is_not_read_only_because_it_saves_a_snapshot():
    drift = _tools()["gulliver_drift"]
    hints = _hints(drift)
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is False
    assert hints["openWorldHint"] is False
    assert "snapshot" in drift.description


def test_read_only_tools_do_not_describe_writes():
    for name, tool in _tools().items():
        if _hints(tool).get("readOnlyHint"):
            assert not WRITE_WORDS.search(tool.description or ""), (
                f"{name} is annotated read-only, but its description mentions writing: {tool.description!r}")


def test_gulliver_lint_description_contains_latest_rule():
    lint = _tools()["gulliver_lint"]
    last_rule = sorted(RULES)[-1]
    assert last_rule in (lint.description or "")
