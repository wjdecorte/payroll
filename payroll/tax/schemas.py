from dataclasses import dataclass

from payroll.common.schemas import AppBaseSchema
from payroll.tax import constants as tax


@dataclass
class PayrollCalculation:
    """
    Pure-data result of the payroll calculation engine.

    All amounts are monthly unless prefixed with ``annual_``.
    This is a plain dataclass (not Pydantic) so the engine remains
    dependency-free and is trivial to unit-test.
    """

    # Wage bases
    fica_wages: float
    income_tax_wages: float
    annual_income_tax_wages: float

    # Social Security
    ss_taxable_wages: float
    ss_ee: float
    ss_er: float
    ss_cap_hit: bool

    # Medicare
    medicare_ee: float
    additional_medicare: float
    total_medicare_ee: float
    medicare_er: float

    # Federal income tax
    annual_fed_tax: float
    federal_withholding: float

    # Georgia income tax
    ga_annual_taxable: float
    annual_ga_tax: float
    ga_withholding: float

    # Totals
    total_employee_deductions: float
    net_pay: float
    total_employer_taxes: float
    total_employer_cost: float
    ytd_fica_after: float


class TaxConstantsSchema(AppBaseSchema):
    """Read-only view of the current tax year constants."""

    tax_year: int = tax.TAX_YEAR
    ss_wage_base: float = tax.SS_WAGE_BASE
    ss_ee_rate: float = tax.SS_EE_RATE
    ss_er_rate: float = tax.SS_ER_RATE
    medicare_ee_rate: float = tax.MEDICARE_EE_RATE
    medicare_er_rate: float = tax.MEDICARE_ER_RATE
    additional_medicare_rate: float = tax.ADD_MEDICARE_RATE
    additional_medicare_threshold: float = tax.ADD_MEDICARE_THRESH
    ga_flat_rate: float = tax.GA_RATE
    ga_standard_deduction: float = tax.GA_STD_DEDUCTION
    ga_state: str = tax.GA_STATE
    pay_periods_per_year: int = tax.PAY_PERIODS_PER_YEAR
