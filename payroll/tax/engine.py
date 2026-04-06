"""
Payroll calculation engine.

All functions are pure (no I/O, no database, no FastAPI) so they are
trivial to unit-test and reuse across different layers of the application.
"""

from payroll.tax import constants as tax
from payroll.tax.schemas import PayrollCalculation


def calc_federal_withholding(annual_wages: float) -> float:
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
    adjusted = max(0.0, annual_wages - tax.FED_STEP1G_SINGLE)
    if adjusted == 0.0:
        return 0.0

    for lower, upper, rate, base in tax.FEDERAL_BRACKETS:
        if adjusted < upper:
            return base + rate * (adjusted - lower)

    # Fallback: top bracket (adjusted >= last lower limit)
    last = tax.FEDERAL_BRACKETS[-1]
    return last[3] + last[2] * (adjusted - last[0])


def calc_payroll(
    gross_salary: float,
    health_insurance: float,
    hsa_contribution: float,
    health_in_income_tax: bool,
    hsa_in_income_tax: bool,
    ytd_fica_wages: float,
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
        PayrollCalculation dataclass with all computed amounts.
    """

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
    ss_taxable = max(0.0, min(fica_wages, tax.SS_WAGE_BASE - ytd_fica_wages))
    ss_ee = round(ss_taxable * tax.SS_EE_RATE, 2)
    ss_er = round(ss_taxable * tax.SS_ER_RATE, 2)
    ss_cap_hit = (ytd_fica_wages + fica_wages) >= tax.SS_WAGE_BASE

    # ------------------------------------------------------------------
    # Medicare
    # ------------------------------------------------------------------
    medicare_ee = round(fica_wages * tax.MEDICARE_EE_RATE, 2)
    medicare_er = round(fica_wages * tax.MEDICARE_ER_RATE, 2)

    # Additional 0.9% Medicare (employee only)
    ytd_after = ytd_fica_wages + fica_wages
    if ytd_after > tax.ADD_MEDICARE_THRESH:
        add_med_base = min(fica_wages, ytd_after - tax.ADD_MEDICARE_THRESH)
        additional_medicare = round(add_med_base * tax.ADD_MEDICARE_RATE, 2)
    else:
        additional_medicare = 0.0

    total_medicare_ee = round(medicare_ee + additional_medicare, 2)

    # ------------------------------------------------------------------
    # Federal income tax withholding
    # ------------------------------------------------------------------
    annual_income_tax_wages = income_tax_wages * pay_periods
    annual_fed_tax = calc_federal_withholding(annual_income_tax_wages)
    federal_withholding = round(annual_fed_tax / pay_periods, 2)

    # ------------------------------------------------------------------
    # Georgia state income tax withholding
    # ------------------------------------------------------------------
    annual_ga_income_tax_wages = ga_income_tax_wages * pay_periods
    ga_annual_taxable = max(0.0, annual_ga_income_tax_wages - tax.GA_STD_DEDUCTION)
    annual_ga_tax = ga_annual_taxable * tax.GA_RATE
    ga_withholding = round(annual_ga_tax / pay_periods, 2)

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------
    total_employee_deductions = round(
        federal_withholding + ga_withholding + ss_ee + total_medicare_ee, 2
    )
    net_pay = round(gross_salary - total_employee_deductions, 2)
    total_employer_taxes = round(ss_er + medicare_er, 2)
    total_employer_cost = round(
        gross_salary + health_insurance + hsa_contribution + total_employer_taxes, 2
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
        annual_fed_tax=round(annual_fed_tax, 2),
        annual_ga_tax=round(annual_ga_tax, 2),
        federal_withholding=federal_withholding,
        ga_withholding=ga_withholding,
        total_employee_deductions=total_employee_deductions,
        net_pay=net_pay,
        total_employer_taxes=total_employer_taxes,
        total_employer_cost=total_employer_cost,
        ytd_fica_after=round(ytd_fica_wages + fica_wages, 2),
    )
