"""
Unit tests for the pure tax calculation engine.

These tests have no database or HTTP dependencies — they verify the math.
"""

import pytest

from payroll.tax import constants as tax
from payroll.tax.engine import calc_federal_withholding, calc_payroll


class TestFederalWithholding:
    """Test the federal income tax percentage method calculation."""

    def test_zero_wages_returns_zero(self):
        assert calc_federal_withholding(0) == 0.0

    def test_negative_wages_returns_zero(self):
        assert calc_federal_withholding(-1000) == 0.0

    def test_below_standard_deduction_returns_zero(self):
        # Annualized wages below $16,100 — in the 0% bracket (2026 single std deduction)
        assert calc_federal_withholding(16_099) == 0.0

    def test_10_percent_bracket(self):
        # $22,000 annual → in 10% bracket (16,100–28,500)
        # Tax = 0 + 10% × (22,000 − 16,100) = $590
        result = calc_federal_withholding(22_000)
        assert abs(result - 590.0) < 0.01

    def test_12_percent_bracket(self):
        # $40,000 annual → in 12% bracket (28,500–66,500)
        # Tax = 1,240 + 12% × (40,000 − 28,500) = 1,240 + 1,380 = 2,620.00
        result = calc_federal_withholding(40_000)
        assert abs(result - 2_620.00) < 0.01

    def test_22_percent_bracket(self):
        # $90,000 annual → in 22% bracket (66,500–121,800)
        # Tax = 5,800 + 22% × (90,000 − 66,500) = 5,800 + 5,170 = 10,970.00
        result = calc_federal_withholding(90_000)
        assert abs(result - 10_970.00) < 0.01

    def test_bracket_boundary_28500(self):
        # At the exact 10%/12% boundary ($28,500)
        # Tax = 0 + 10% × (28,500 − 16,100) = 10% × 12,400 = 1,240.00
        result = calc_federal_withholding(28_500)
        assert abs(result - 1_240.00) < 0.01

    def test_high_income_37_percent_bracket(self):
        # $700,000 annual → in 37% bracket
        assert calc_federal_withholding(700_000) > 188_769.75


class TestCalcPayroll:
    """Test the full payroll calculation with a known set of inputs."""

    @pytest.fixture
    def standard_result(self):
        """$8,000 gross + $500 health ins + $300 HSA — baseline scenario."""
        return calc_payroll(
            gross_salary=8_000.00,
            health_insurance=500.00,
            hsa_contribution=300.00,
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=0.0,
        )

    def test_fica_wages_excludes_health_insurance(self, standard_result):
        # Health ins is NOT subject to FICA for >2% S-Corp shareholders
        assert standard_result.fica_wages == 8_000.00

    def test_income_tax_wages_includes_health_insurance(self, standard_result):
        # Health ins IS included in W-2 Box 1 wages
        assert standard_result.income_tax_wages == 8_500.00

    def test_federal_withholding_is_positive(self, standard_result):
        assert standard_result.federal_withholding > 0

    def test_federal_withholding_monthly_vs_annual(self, standard_result):
        # Monthly should be annual / 12
        annual = calc_federal_withholding(8_500 * 12)
        expected_monthly = round(annual / 12, 2)
        assert abs(standard_result.federal_withholding - expected_monthly) < 0.01

    def test_ga_withholding_uses_flat_rate(self, standard_result):
        # GA = (annual wages − $12,000 std deduction) × 5.19% / 12
        annual_taxable = (8_500 * 12) - tax.GA_STD_DEDUCTION
        expected = round(annual_taxable * tax.GA_RATE / 12, 2)
        assert abs(standard_result.ga_withholding - expected) < 0.01

    def test_ss_employee_rate(self, standard_result):
        expected = round(8_000 * tax.SS_EE_RATE, 2)
        assert abs(standard_result.ss_ee - expected) < 0.01

    def test_ss_employer_rate(self, standard_result):
        expected = round(8_000 * tax.SS_ER_RATE, 2)
        assert abs(standard_result.ss_er - expected) < 0.01

    def test_medicare_employee_rate(self, standard_result):
        expected = round(8_000 * tax.MEDICARE_EE_RATE, 2)
        assert abs(standard_result.medicare_ee - expected) < 0.01

    def test_net_pay_equals_gross_minus_deductions(self, standard_result):
        r = standard_result
        calculated_net = round(
            r.fica_wages - r.federal_withholding - r.ga_withholding - r.ss_ee - r.total_medicare_ee,
            2,
        )
        assert abs(r.net_pay - calculated_net) < 0.01

    def test_no_additional_medicare_below_threshold(self, standard_result):
        assert standard_result.additional_medicare == 0.0

    def test_additional_medicare_above_threshold(self):
        # Push YTD near threshold so this payroll crosses $200k annual
        result = calc_payroll(
            gross_salary=15_000.00,
            health_insurance=0.0,
            hsa_contribution=0.0,
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=195_000.0,
        )
        assert result.additional_medicare > 0

    def test_ss_cap_hit_when_ytd_exceeds_wage_base(self):
        result = calc_payroll(
            gross_salary=8_000.00,
            health_insurance=0.0,
            hsa_contribution=0.0,
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=tax.SS_WAGE_BASE,  # Already at cap
        )
        assert result.ss_ee == 0.0
        assert result.ss_er == 0.0
        assert result.ss_cap_hit is True

    def test_ss_partial_cap(self):
        # Only $1,000 remaining before cap
        result = calc_payroll(
            gross_salary=8_000.00,
            health_insurance=0.0,
            hsa_contribution=0.0,
            health_in_income_tax=True,
            hsa_in_income_tax=False,
            ytd_fica_wages=tax.SS_WAGE_BASE - 1_000,
        )
        expected_ss = round(1_000 * tax.SS_EE_RATE, 2)
        assert abs(result.ss_ee - expected_ss) < 0.01

    def test_total_employer_cost(self, standard_result):
        r = standard_result
        expected = round(r.fica_wages + 500.00 + 300.00 + r.total_employer_taxes, 2)
        assert abs(r.total_employer_cost - expected) < 0.01

    def test_hsa_not_in_income_tax_wages_by_default(self, standard_result):
        # HSA ($300) should NOT be included in income_tax_wages when hsa_in_income_tax=False
        assert standard_result.income_tax_wages == 8_500.00  # gross + health only

    def test_hsa_included_when_flag_set(self):
        result = calc_payroll(
            gross_salary=8_000.00,
            health_insurance=500.00,
            hsa_contribution=300.00,
            health_in_income_tax=True,
            hsa_in_income_tax=True,
            ytd_fica_wages=0.0,
        )
        assert result.income_tax_wages == 8_800.00  # 8000 + 500 + 300
