"""
Unit tests for the pure tax calculation engine.

These tests have no database or HTTP dependencies — they verify the math.
"""

from dataclasses import fields
from decimal import Decimal

import pytest

from payroll.common.money import round_money
from payroll.tax import constants as tax
from payroll.tax.engine import calc_federal_withholding, calc_payroll


class TestFederalWithholding:
    """Test the federal income tax percentage method calculation."""

    def test_zero_wages_returns_zero(self):
        assert calc_federal_withholding(Decimal("0")) == 0

    def test_negative_wages_returns_zero(self):
        assert calc_federal_withholding(Decimal("-1000")) == 0

    def test_below_standard_deduction_returns_zero(self):
        # Annualized wages below $16,100 — in the 0% bracket (2026 single std deduction)
        assert calc_federal_withholding(Decimal("16_099")) == 0

    def test_10_percent_bracket(self):
        # $22,000 annual → in 10% bracket (16,100–28,500)
        # Tax = 0 + 10% × (22,000 − 16,100) = $590
        result = calc_federal_withholding(Decimal("22_000"))
        assert result == Decimal("590.00")

    def test_12_percent_bracket(self):
        # $40,000 annual → in 12% bracket (28,500–66,500)
        # Tax = 1,240 + 12% × (40,000 − 28,500) = 1,240 + 1,380 = 2,620.00
        result = calc_federal_withholding(Decimal("40_000"))
        assert result == Decimal("2_620.00")

    def test_22_percent_bracket(self):
        # $90,000 annual → in 22% bracket (66,500–121,800)
        # Tax = 5,800 + 22% × (90,000 − 66,500) = 5,800 + 5,170 = 10,970.00
        result = calc_federal_withholding(Decimal("90_000"))
        assert result == Decimal("10_970.00")

    def test_bracket_boundary_28500(self):
        # At the exact 10%/12% boundary ($28,500)
        # Tax = 0 + 10% × (28,500 − 16,100) = 10% × 12,400 = 1,240.00
        result = calc_federal_withholding(Decimal("28_500"))
        assert result == Decimal("1_240.00")

    def test_high_income_37_percent_bracket(self):
        # $700,000 annual → in 37% bracket
        assert calc_federal_withholding(Decimal("700_000")) > Decimal("188_769.75")


class TestCalcPayroll:
    """Test the full payroll calculation with a known set of inputs."""

    @pytest.fixture
    def standard_result(self):
        """$8,000 gross + $500 health ins + $300 HSA — baseline scenario."""
        return calc_payroll(
            gross_salary=Decimal("8_000.00"),
            health_insurance=Decimal("500.00"),
            hsa_contribution=Decimal("300.00"),
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=Decimal("0"),
        )

    def test_fica_wages_excludes_health_insurance(self, standard_result):
        # Health ins is NOT subject to FICA for >2% S-Corp shareholders
        assert standard_result.fica_wages == Decimal("8_000.00")

    def test_income_tax_wages_includes_health_insurance(self, standard_result):
        # Health ins IS included in W-2 Box 1 wages
        assert standard_result.income_tax_wages == Decimal("8_500.00")

    def test_federal_withholding_is_positive(self, standard_result):
        assert standard_result.federal_withholding > 0

    def test_federal_withholding_monthly_vs_annual(self, standard_result):
        # Monthly should be annual / 12
        annual = calc_federal_withholding(Decimal("8_500") * 12)
        expected_monthly = round_money(annual / 12)
        assert standard_result.federal_withholding == expected_monthly

    def test_ga_withholding_uses_flat_rate(self, standard_result):
        # GA = (annual wages − $12,000 std deduction) × 5.19% / 12
        annual_taxable = (Decimal("8_500") * 12) - tax.GA_STD_DEDUCTION
        expected = round_money(annual_taxable * tax.GA_RATE / 12)
        assert standard_result.ga_withholding == expected

    def test_ss_employee_rate(self, standard_result):
        expected = round_money(Decimal("8_000") * tax.SS_EE_RATE)
        assert standard_result.ss_ee == expected

    def test_ss_employer_rate(self, standard_result):
        expected = round_money(Decimal("8_000") * tax.SS_ER_RATE)
        assert standard_result.ss_er == expected

    def test_medicare_employee_rate(self, standard_result):
        expected = round_money(Decimal("8_000") * tax.MEDICARE_EE_RATE)
        assert standard_result.medicare_ee == expected

    def test_net_pay_equals_gross_minus_deductions(self, standard_result):
        r = standard_result
        calculated_net = round_money(
            r.fica_wages - r.federal_withholding - r.ga_withholding - r.ss_ee - r.total_medicare_ee
        )
        assert r.net_pay == calculated_net

    def test_no_additional_medicare_below_threshold(self, standard_result):
        assert standard_result.additional_medicare == 0

    def test_additional_medicare_above_threshold(self):
        # Push YTD near threshold so this payroll crosses $200k annual
        result = calc_payroll(
            gross_salary=Decimal("15_000.00"),
            health_insurance=Decimal("0"),
            hsa_contribution=Decimal("0"),
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=Decimal("195_000.00"),
        )
        assert result.additional_medicare > 0

    def test_ss_cap_hit_when_ytd_exceeds_wage_base(self):
        result = calc_payroll(
            gross_salary=Decimal("8_000.00"),
            health_insurance=Decimal("0"),
            hsa_contribution=Decimal("0"),
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=tax.SS_WAGE_BASE,  # Already at cap
        )
        assert result.ss_ee == 0
        assert result.ss_er == 0
        assert result.ss_cap_hit is True

    def test_ss_partial_cap(self):
        # Only $1,000 remaining before cap
        result = calc_payroll(
            gross_salary=Decimal("8_000.00"),
            health_insurance=Decimal("0"),
            hsa_contribution=Decimal("0"),
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=tax.SS_WAGE_BASE - 1_000,
        )
        expected_ss = round_money(Decimal("1_000") * tax.SS_EE_RATE)
        assert result.ss_ee == expected_ss

    def test_total_employer_cost(self, standard_result):
        r = standard_result
        expected = round_money(
            r.fica_wages + Decimal("500.00") + Decimal("300.00") + r.total_employer_taxes
        )
        assert r.total_employer_cost == expected

    def test_hsa_not_in_income_tax_wages_by_default(self, standard_result):
        # HSA ($300) should NOT be included in income_tax_wages when hsa_in_income_tax=False
        assert standard_result.income_tax_wages == Decimal("8_500.00")  # gross + health only

    def test_hsa_included_when_flag_set(self):
        result = calc_payroll(
            gross_salary=Decimal("8_000.00"),
            health_insurance=Decimal("500.00"),
            hsa_contribution=Decimal("300.00"),
            health_in_income_tax=True,
            hsa_in_income_tax=True,
            ytd_fica_wages=Decimal("0"),
        )
        assert result.income_tax_wages == Decimal("8_800.00")  # 8000 + 500 + 300


def _payroll(gross: str, ytd: str = "0", health: str = "0", hsa: str = "0"):
    return calc_payroll(
        gross_salary=Decimal(gross),
        health_insurance=Decimal(health),
        hsa_contribution=Decimal(hsa),
        health_in_income_tax=True,
        hsa_in_income_tax=False,
        ytd_fica_wages=Decimal(ytd),
    )


class TestBoundaries:
    """Edge cases for the exact-Decimal engine."""

    def test_all_amounts_are_decimal(self):
        result = _payroll("8_000.00", health="500.00", hsa="300.00")
        for f in fields(result):
            value = getattr(result, f.name)
            if not isinstance(value, bool):
                assert isinstance(value, Decimal), f.name

    def test_zero_gross_yields_all_zero_result(self):
        result = _payroll("0")
        for f in fields(result):
            value = getattr(result, f.name)
            if isinstance(value, bool):
                assert value is False, f.name
            else:
                assert value == 0, f.name

    def test_ytd_exactly_at_wage_base_yields_zero_ss(self):
        result = _payroll("8_000.00", ytd="184_500.00")
        assert result.ss_taxable_wages == 0
        assert result.ss_ee == 0
        assert result.ss_er == 0
        assert result.ss_cap_hit is True

    def test_ytd_one_cent_under_wage_base_taxes_only_the_last_cent(self):
        result = _payroll("8_000.00", ytd="184_499.99")
        # Only one cent of SS wages remains; 6.2% of $0.01 rounds (half-up) to $0.00.
        assert result.ss_taxable_wages == Decimal("0.01")
        assert result.ss_ee == round_money(Decimal("0.01") * tax.SS_EE_RATE)
        assert result.ss_ee == Decimal("0.00")
        assert result.ss_er == Decimal("0.00")
        assert result.ss_cap_hit is True

    def test_negative_gross_raises(self):
        with pytest.raises(ValueError):
            _payroll("-0.01")

    def test_float_input_is_rejected(self):
        with pytest.raises(TypeError):
            calc_payroll(
                gross_salary=8000.0,  # type: ignore[arg-type]
                health_insurance=Decimal("0"),
                hsa_contribution=Decimal("0"),
                health_in_income_tax=True,
                hsa_in_income_tax=False,
                ytd_fica_wages=Decimal("0"),
            )
