"""The electricity price option: one rule for the flow and the reader."""

from __future__ import annotations

import pytest

from custom_components.sp_group.const import parse_electricity_price


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        (0, None),
        (0.0, None),
        (-1, None),
        (0.29, 0.29),
        ("0.29", 0.29),
        (1, 1.0),
    ],
)
def test_price_or_unset(raw: object, expected: float | None) -> None:
    assert parse_electricity_price(raw) == expected


@pytest.mark.parametrize("raw", ["free", "0.2SGD", float("nan"), float("inf")])
def test_unusable_price_is_reported(raw: object) -> None:
    """A value nobody can price against raises instead of costing nothing."""
    with pytest.raises(ValueError, match="electricity_price"):
        parse_electricity_price(raw)


@pytest.mark.parametrize("raw", [True, False, [0.29], {"price": 0.29}])
def test_wrong_type_price_is_reported(raw: object) -> None:
    with pytest.raises(ValueError, match="electricity_price"):
        parse_electricity_price(raw)
