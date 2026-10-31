"""
Payroll calculation engine.

All functions are pure (no I/O, no database, no FastAPI) so they are
trivial to unit-test and reuse across different layers of the application.

All money is exact ``Decimal``. ``round_money`` (half-up to cents) is applied at
exactly the points the original float engine rounded to two places.
"""

from decimal import Decimal

from payroll.common.money import round_money
from payroll.tax import constants as tax
from payroll.tax.schemas import PayrollCalculation

ZERO = Decimal("0")


def calc_federal_withholding(annual_wages: Decimal) -> Decimal:
    """
    Compute the annual federal income tax using IRS Pub 15-T Percentage Method
    for a single/MFS filer whose W-4 Step 2 box is NOT checked.

    Implements Worksheet 1A (automated payroll systems):
      Step 1g: Subtract FED_STEP1G_SINGLE ($8,600) from annualized wages before
               looking up in the Standard Withholding Rate Schedule table.

    Args:
        annual_wages: Annualized income-tax wages (gross monthly × 12, plus
                      any additions such as S-Corp health insurance premium).

    Returns:
        Annual federal income tax amount (divide by 12 for monthly withholding).
    """
    # Worksheet 1A Step 1g: apply the standard deduction offset for single filers
    adjusted = max(ZERO, annual_wages - tax.FED_STEP1G_SINGLE)
    if adjusted == ZERO:
        return ZERO

    # The top bracket has no upper bound (None), so the loop always returns.
    for lower, upper, rate, base in tax.FEDERAL_BRACKETS:
        if upper is None or adjusted < upper:
            return base + rate * (adjusted - lower)

    raise AssertionError("FEDERAL_BRACKETS must end with an unbounded (None) bracket")


def calc_payroll(
    gross_salary: Decimal,
    health_insurance: Decimal,
    hsa_contribution: Decimal,
    health_in_income_tax: bool,
    hsa_in_income_tax: bool,
    ytd_fica_wages: Decimal,
    pay_periods: int = tax.PAY_PERIODS_PER_YEAR,
) -> PayrollCalculation:
    """
    Calculate all payroll withholdings for an S-Corp owner-employee.

    S-Corp tax treatment applied here:
      - Health insurance premiums: included in W-2 Box 1 (income tax) but NOT
        subject to FICA for >2% shareholders (IRC §3121(a)(2)(B), §1372).
      - HSA employer contributions: treatment varies; toggle ``hsa_in_income_tax``
        per your CPA's guidance.

    Args:
        gross_salary:        Monthly cash wages (officer compensation).
        health_insurance:    Monthly S-Corp health insurance premium.
        hsa_contribution:    Monthly HSA employer contribution.
        health_in_income_tax: Add health ins to income-tax wage base (standard).
        hsa_in_income_tax:   Add HSA to income-tax wage base (CPA-dependent).
        ytd_fica_wages:      Year-to-date FICA wages *before* this payroll.
        pay_periods:         Pay periods per year (12 for monthly).

    Returns:
        PayrollCalculation dataclass with all computed amounts (``Decimal``).

    Raises:
        ValueError: If ``gross_salary`` is negative.
    """
    if gross_salary < 0:
        raise ValueError(f"gross_salary must not be negative (got {gross_salary})")

    # ------------------------------------------------------------------
    # Wage bases
    # ------------------------------------------------------------------
    fica_wages = gross_salary  # Health ins and HSA excluded from FICA

    # Federal income-tax wages (W-2 Box 1): includes health ins and optionally HSA
    income_tax_wages = gross_salary
    if health_in_income_tax:
        income_tax_wages += health_insurance
    if hsa_in_income_tax:
        income_tax_wages += hsa_contribution

    # Georgia income-tax wages: health ins follows federal treatment, but HSA
    # employer contributions are NOT added to the Georgia wage base.  Georgia
    # conforms to IRC as of 1/1/2025 and does not separately tax employer HSA
    # contributions the way an S-Corp may elect to include them in federal Box 1.
    ga_income_tax_wages = gross_salary
    if health_in_income_tax:
        ga_income_tax_wages += health_insurance

    # ------------------------------------------------------------------
    # Social Security
    # ------------------------------------------------------------------
    ss_taxable = max(ZERO, min(fica_wages, tax.SS_WAGE_BASE - ytd_fica_wages))
    ss_ee = round_money(ss_taxable * tax.SS_EE_RATE)
    ss_er = round_money(ss_taxable * tax.SS_ER_RATE)
    ss_cap_hit = (ytd_fica_wages + fica_wages) >= tax.SS_WAGE_BASE

    # ------------------------------------------------------------------
    # Medicare
    # ------------------------------------------------------------------
    medicare_ee = round_money(fica_wages * tax.MEDICARE_EE_RATE)
    medicare_er = round_money(fica_wages * tax.MEDICARE_ER_RATE)

    # Additional 0.9% Medicare (employee only)
    ytd_after = ytd_fica_wages + fica_wages
    if ytd_after > tax.ADD_MEDICARE_THRESH:
        add_med_base = min(fica_wages, ytd_after - tax.ADD_MEDICARE_THRESH)
        additional_medicare = round_money(add_med_base * tax.ADD_MEDICARE_RATE)
    else:
        additional_medicare = ZERO

    total_medicare_ee = round_money(medicare_ee + additional_medicare)

    # ------------------------------------------------------------------
    # Federal income tax withholding
    # ------------------------------------------------------------------
    annual_income_tax_wages = income_tax_wages * pay_periods
    annual_fed_tax = calc_federal_withholding(annual_income_tax_wages)
    federal_withholding = round_money(annual_fed_tax / pay_periods)

    # ------------------------------------------------------------------
    # Georgia state income tax withholding
    # ------------------------------------------------------------------
    annual_ga_income_tax_wages = ga_income_tax_wages * pay_periods
    ga_annual_taxable = max(ZERO, annual_ga_income_tax_wages - tax.GA_STD_DEDUCTION)
    annual_ga_tax = ga_annual_taxable * tax.GA_RATE
    ga_withholding = round_money(annual_ga_tax / pay_periods)

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------
    total_employee_deductions = round_money(
        federal_withholding + ga_withholding + ss_ee + total_medicare_ee
    )
    net_pay = round_money(gross_salary - total_employee_deductions)
    total_employer_taxes = round_money(ss_er + medicare_er)
    total_employer_cost = round_money(
        gross_salary + health_insurance + hsa_contribution + total_employer_taxes
    )

    return PayrollCalculation(
        fica_wages=fica_wages,
        income_tax_wages=income_tax_wages,
        annual_income_tax_wages=annual_income_tax_wages,
        ss_taxable_wages=ss_taxable,
        ss_ee=ss_ee,
        ss_er=ss_er,
        ss_cap_hit=ss_cap_hit,
        medicare_ee=medicare_ee,
        additional_medicare=additional_medicare,
        total_medicare_ee=total_medicare_ee,
        medicare_er=medicare_er,
        ga_annual_taxable=ga_annual_taxable,
        annual_fed_tax=round_money(annual_fed_tax),
        annual_ga_tax=round_money(annual_ga_tax),
        federal_withholding=federal_withholding,
        ga_withholding=ga_withholding,
        total_employee_deductions=total_employee_deductions,
        net_pay=net_pay,
        total_employer_taxes=total_employer_taxes,
        total_employer_cost=total_employer_cost,
        ytd_fica_after=round_money(ytd_fica_wages + fica_wages),
    )
