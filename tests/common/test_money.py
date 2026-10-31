"""Unit tests for exact-money helpers."""

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from payroll.common.money import Money, round_money


def test_rounds_half_up():
    assert round_money(Decimal("2.345")) == Decimal("2.35")


def test_rounds_down_below_half():
    assert round_money(Decimal("2.344")) == Decimal("2.34")


def test_negative_half_rounds_away_from_zero():
    assert round_money(Decimal("-0.005")) == Decimal("-0.01")


def test_accepts_int_and_str():
    assert round_money(5) == Decimal("5.00")
    assert round_money("1.005") == Decimal("1.01")


def test_result_has_two_decimal_places():
    assert round_money(Decimal("7")).as_tuple().exponent == -2


def test_rejects_float():
    with pytest.raises(TypeError):
        round_money(1.5)


class _Amount(BaseModel):
    value: Money


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(8000, "8000.00"), (8000.1, "8000.10"), ("1234.5", "1234.50"), ("0", "0.00")],
)
def test_money_field_normalizes_to_cents_and_serializes_as_string(raw, expected):
    amount = _Amount(value=raw)
    assert amount.value == Decimal(expected)
    assert amount.model_dump(mode="json") == {"value": expected}
    assert amount.model_dump_json() == f'{{"value":"{expected}"}}'


def test_money_field_rejects_sub_cent_values():
    with pytest.raises(ValidationError):
        _Amount(value="12.345")
