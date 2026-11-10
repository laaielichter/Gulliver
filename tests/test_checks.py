import pytest

from gulliver.checks import parse_spec


def _bounds(text):
    return parse_spec(ranges=[f"amount={text}"]).ranges["amount"]

@pytest.mark.parametrize("value", [-1, -5])
def test_min_rows_refuses_negative(value):
    with pytest.raises(ValueError, match=r"min_rows needs 0 or more"):
        parse_spec(min_rows=value)

@pytest.mark.parametrize("value", [-5.0, float("nan"), float("inf"), float("-inf")])
def test_max_change_refuses_negative_or_non_finite(value):
    with pytest.raises(ValueError, match=r"max_change needs a finite number"):
        parse_spec(max_change_pct=value)



@pytest.mark.parametrize("text", ["nan:", ":inf", "-inf:", ":nan"])
def test_range_refuses_non_finite_bounds(text):
    with pytest.raises(ValueError, match="finite"):
        parse_spec(ranges=[f"amount={text}"])

def test_zero_thresholds_still_parse():
    spec = parse_spec(min_rows=0, max_change_pct=0.0)
    assert spec.min_rows == 0
    assert spec.max_change_pct == 0.0

def test_range_refuses_a_non_number():
    with pytest.raises(ValueError, match=r"range for 'amount' needs numbers, got 'abc'"):
        parse_spec(ranges=["amount=abc:1"])


def test_range_refuses_inverted_bounds():
    with pytest.raises(ValueError, match=r"range for 'amount' has min 100 > max 1"):
        parse_spec(ranges=["amount=100:1"])


def test_range_bounds_still_parse():
    assert _bounds("0:") == (0.0, None)
    assert _bounds(":10") == (None, 10.0)
    assert _bounds("5:5") == (5.0, 5.0)
