"""Every place that carries the release version must agree, or a release ships mismatched pieces.

The npm launcher starts exactly its own version of the Python package, the MCP Registry checks the
versions in server.json, and the Claude plugin pins `uvx gulliver-mcp==X.Y.Z` (the plugin directory
blocks an unpinned launcher). Bumping one and forgetting another is easy, so this test lists them all.
"""

import json
import re
from pathlib import Path

import gulliver

ROOT = Path(__file__).resolve().parents[1]


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_all_release_versions_match():
    version = gulliver.__version__
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    server = _json("server.json")
    plugin = _json(".claude-plugin/plugin.json")
    marketplace = _json(".claude-plugin/marketplace.json")

    found = {
        "pyproject.toml": pyproject.group(1) if pyproject else None,
        "npm/package.json": _json("npm/package.json")["version"],
        "server.json version": server["version"],
        **{f"server.json {p['registryType']} package": p["version"] for p in server["packages"]},
        ".claude-plugin/plugin.json version": plugin["version"],
        ".claude-plugin/plugin.json uvx pin": next(
            (a.split("==", 1)[1] for a in plugin["mcpServers"]["gulliver"]["args"] if "==" in a), None),
        **{f".claude-plugin/marketplace.json {p['name']}": p["version"] for p in marketplace["plugins"]},
    }
    wrong = {where: v for where, v in found.items() if v != version}
    assert not wrong, f"these do not match gulliver.__version__ ({version}): {wrong}"
