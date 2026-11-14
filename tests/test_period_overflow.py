"""Huge periods must be a clean error, not an OverflowError traceback.

`parse_period("99999999999d")` reached `timedelta`, whose fields are C ints,
so the CLI printed a traceback. Every command that reads a period goes through
this function: `prune --older-than`, `check --fresh`, and `incremental --period`.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from gulliver import incremental
from gulliver.checks import parse_spec
from gulliver.cli import main

HUGE = "99999999999"
UNITS = ("d", "h", "m", "min", "w")


@pytest.mark.parametrize("unit", UNITS)
def test_parse_period_refuses_out_of_range_values(unit):
    with pytest.raises(ValueError, match="out of range"):
        incremental.parse_period(f"{HUGE}{unit}")


@pytest.mark.parametrize("text,expected_seconds", [("24h", 86400), ("7 days", 604800), ("30 min", 1800)])
def test_parse_period_valid_values_are_unchanged(text, expected_seconds):
    assert incremental.parse_period(text).total_seconds() == expected_seconds


def test_parse_still_refuses_a_bad_period():
    with pytest.raises(ValueError, match="period looks like"):
        incremental.parse_period("soon")


def test_check_parse_spec_validates_fresh_early():
    with pytest.raises(ValueError, match="out of range"):
        parse_spec(fresh=[f"ts={HUGE}d"])


def _run(argv: list[str]) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


def test_check_cli_reports_a_clean_error_with_exit_1():
    code, out = _run(["check", "events", "--not-null", "event_id", "--fresh", f"ts={HUGE}d", "--json"])
    assert code == 1
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "out of range" in payload["error"]


def test_incremental_cli_reports_a_clean_error_with_exit_1():
    code, out = _run(["incremental", "select * from events", "--period", f"{HUGE}d", "--json"])
    assert code == 1
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "out of range" in payload["error"]
