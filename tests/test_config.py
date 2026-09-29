"""The electricity price option: one rule for the flow and the reader."""

from __future__ import annotations

import pytest

from custom_components.sp_group.const import (
    MAX_USERNAME_OCTETS,
    fold_text,
    parse_electricity_price,
    validate_username,
)


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


def test_username_bound_is_counted_in_utf8_octets() -> None:
    """The cap is the address length on the wire, not a count of code points."""
    at_cap = "a" * MAX_USERNAME_OCTETS
    assert validate_username(at_cap) == at_cap
    # 64 astral code points is 64 characters and 256 bytes: past the cap.
    with pytest.raises(ValueError, match="bytes"):
        validate_username("\N{GRINNING FACE}" * 64)
    # 62 of them is 248 bytes, so the cap is not a code-point count in the
    # other direction either.
    assert validate_username("\N{GRINNING FACE}" * 62) == "\N{GRINNING FACE}" * 62


def test_empty_and_unencodable_usernames_are_reported() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_username("")
    with pytest.raises(ValueError, match="valid Unicode"):
        validate_username("user\ud800@example.com")


def test_fold_collapses_the_spellings_of_one_name() -> None:
    """One account typed two ways is one account, spaces and all."""
    assert fold_text("User@Example.com") == fold_text("user@example.com")
    assert fold_text("user@example.com ") == fold_text("user@example.com")
    assert fold_text("user@example.com\u00a0 ") == fold_text("user@example.com")
    assert fold_text("\N{LATIN SMALL LETTER E}\N{COMBINING ACUTE ACCENT}") == fold_text(
        "\N{LATIN SMALL LETTER E WITH ACUTE}"
    )
    assert fold_text(" User@Example.com ") == "user@example.com"
