"""
Unit tests for the pure FUTA / Georgia SUTA calculation.

No database or HTTP dependencies.
"""

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from payroll.tax import unemployment
from payroll.tax.unemployment import UnemploymentRates, calc_unemployment

RATES = UnemploymentRates(
    futa_rate=Decimal("0.006"),
    futa_wage_base=Decimal("7000.00"),
    suta_rate=Decimal("0.0264"),
    suta_wage_base=Decimal("9500.00"),
)


class TestFuta:
    def test_below_base(self):
        result = calc_unemployment(Decimal("0"), Decimal("5000"), RATES)
        assert result.futa_wages == Decimal("5000.00")
        assert result.futa == Decimal("30.00")

    def test_crossing_base(self):
        result = calc_unemployment(Decimal("6500"), Decimal("8500"), RATES)
        assert result.futa_wages == Decimal("500.00")
        assert result.futa == Decimal("3.00")

    def test_already_over_base(self):
        result = calc_unemployment(Decimal("7500"), Decimal("9500"), RATES)
        assert result.futa_wages == Decimal("0.00")
        assert result.futa == Decimal("0.00")

    def test_zero_wages(self):
        result = calc_unemployment(Decimal("0"), Decimal("0"), RATES)
        assert result.futa_wages == Decimal("0.00")
        assert result.futa == Decimal("0.00")
        assert result.suta_wages == Decimal("0.00")
        assert result.suta == Decimal("0.00")

    def test_zero_rate_gives_zero_amounts(self):
        rates = UnemploymentRates(
            futa_rate=Decimal("0"),
            futa_wage_base=Decimal("7000.00"),
            suta_rate=Decimal("0"),
            suta_wage_base=Decimal("9500.00"),
        )
        result = calc_unemployment(Decimal("0"), Decimal("5000"), rates)
        assert result.futa_wages == Decimal("5000.00")
        assert result.futa == Decimal("0.00")
        assert result.suta_wages == Decimal("5000.00")
        assert result.suta == Decimal("0.00")

    def test_rounds_half_up_below_half_cent_boundary(self):
        # 123.45 * 0.006 = 0.7407 -> 0.74
        result = calc_unemployment(Decimal("0"), Decimal("123.45"), RATES)
        assert result.futa_wages == Decimal("123.45")
        assert result.futa == Decimal("0.74")

    def test_rounds_half_up_at_exactly_half_cent(self):
        # 1.00 * 0.005 = 0.005 -> 0.01 (half-up, not banker's)
        rates = UnemploymentRates(
            futa_rate=Decimal("0.005"),
            futa_wage_base=Decimal("7000.00"),
            suta_rate=Decimal("0"),
            suta_wage_base=Decimal("9500.00"),
        )
        result = calc_unemployment(Decimal("0"), Decimal("1.00"), rates)
        assert result.futa == Decimal("0.01")


class TestSuta:
    def test_crossing_base(self):
        # 9,000 -> 10,000 only 500 of it is under the 9,500 base.
        result = calc_unemployment(Decimal("9000"), Decimal("10000"), RATES)
        assert result.suta_wages == Decimal("500.00")
        assert result.suta == Decimal("13.20")  # 500 * 0.0264

    def test_full_base_in_one_run(self):
        result = calc_unemployment(Decimal("0"), Decimal("12000"), RATES)
        assert result.suta_wages == Decimal("9500.00")
        assert result.suta == Decimal("250.80")  # 9500 * 0.0264

    def test_futa_and_suta_bases_are_independent(self):
        # Past the FUTA base (7,000) but still under the SUTA base (9,500).
        result = calc_unemployment(Decimal("7500"), Decimal("9000"), RATES)
        assert result.futa_wages == Decimal("0.00")
        assert result.suta_wages == Decimal("1500.00")
        assert result.suta == Decimal("39.60")

    def test_already_over_base(self):
        result = calc_unemployment(Decimal("9500"), Decimal("12000"), RATES)
        assert result.suta_wages == Decimal("0.00")
        assert result.suta == Decimal("0.00")


class TestValidation:
    def test_after_less_than_before_raises(self):
        with pytest.raises(ValueError):
            calc_unemployment(Decimal("5000"), Decimal("4000"), RATES)

    def test_negative_before_raises(self):
        with pytest.raises(ValueError):
            calc_unemployment(Decimal("-1"), Decimal("100"), RATES)

    def test_negative_after_raises(self):
        with pytest.raises(ValueError):
            calc_unemployment(Decimal("0"), Decimal("-100"), RATES)


class TestResultShape:
    def test_results_quantized_to_two_places(self):
        result = calc_unemployment(Decimal("0"), Decimal("5000"), RATES)
        for value in (result.futa_wages, result.futa, result.suta_wages, result.suta):
            assert value.as_tuple().exponent == -2

    def test_module_does_not_import_payroll_run(self):
        tree = ast.parse(Path(unemployment.__file__).read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any(name.startswith("payroll.payroll_run") for name in imported)
