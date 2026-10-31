from dataclasses import dataclass
from decimal import Decimal

from payroll.common.schemas import AppBaseSchema
from payroll.tax import constants as tax


@dataclass
class PayrollCalculation:
    """
    Pure-data result of the payroll calculation engine.

    All amounts are monthly unless prefixed with ``annual_``, and every amount
    is an exact ``Decimal`` (never ``float``).
    This is a plain dataclass (not Pydantic) so the engine remains
    dependency-free and is trivial to unit-test.
    """

    # Wage bases
    fica_wages: Decimal
    income_tax_wages: Decimal
    annual_income_tax_wages: Decimal

    # Social Security
    ss_taxable_wages: Decimal
    ss_ee: Decimal
    ss_er: Decimal
    ss_cap_hit: bool

    # Medicare
    medicare_ee: Decimal
    additional_medicare: Decimal
    total_medicare_ee: Decimal
    medicare_er: Decimal

    # Federal income tax
    annual_fed_tax: Decimal
    federal_withholding: Decimal

    # Georgia income tax
    ga_annual_taxable: Decimal
    annual_ga_tax: Decimal
    ga_withholding: Decimal

    # Totals
    total_employee_deductions: Decimal
    net_pay: Decimal
    total_employer_taxes: Decimal
    total_employer_cost: Decimal
    ytd_fica_after: Decimal


class TaxConstantsSchema(AppBaseSchema):
    """Read-only view of the current tax year constants."""

    # Rates and amounts are exact Decimals and serialize to JSON as strings.
    tax_year: int = tax.TAX_YEAR
    ss_wage_base: Decimal = tax.SS_WAGE_BASE
    ss_ee_rate: Decimal = tax.SS_EE_RATE
    ss_er_rate: Decimal = tax.SS_ER_RATE
    medicare_ee_rate: Decimal = tax.MEDICARE_EE_RATE
    medicare_er_rate: Decimal = tax.MEDICARE_ER_RATE
    additional_medicare_rate: Decimal = tax.ADD_MEDICARE_RATE
    additional_medicare_threshold: Decimal = tax.ADD_MEDICARE_THRESH
    ga_flat_rate: Decimal = tax.GA_RATE
    ga_standard_deduction: Decimal = tax.GA_STD_DEDUCTION
    ga_state: str = tax.GA_STATE
    pay_periods_per_year: int = tax.PAY_PERIODS_PER_YEAR
