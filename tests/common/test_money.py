"""Unit tests for exact-money helpers."""

from decimal import Decimal

import pytest

from payroll.common.money import round_money


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
