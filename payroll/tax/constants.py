"""
2026 Tax Year Constants
=======================
Review and update each January.

Sources:
  Federal brackets / withholding : IRS Publication 15-T (irs.gov/pub/irs-pdf/p15t.pdf)
  Social Security wage base      : SSA.gov (https://www.ssa.gov/oact/cola/cbb.html)
  Georgia income tax             : Georgia DOR (dor.georgia.gov)
"""

from decimal import Decimal

TAX_YEAR: int = 2026

# ---------------------------------------------------------------------------
# FICA
# ---------------------------------------------------------------------------

SS_EE_RATE: Decimal = Decimal("0.062")  # Employee Social Security rate
SS_ER_RATE: Decimal = Decimal("0.062")  # Employer Social Security rate
SS_WAGE_BASE: Decimal = Decimal("184_500.00")  # 2026 annual SS wage base (SSA.gov)

MEDICARE_EE_RATE: Decimal = Decimal("0.0145")  # Employee Medicare rate
MEDICARE_ER_RATE: Decimal = Decimal("0.0145")  # Employer Medicare rate

# Additional 0.9% Medicare applies to the EMPLOYEE only on annual wages above this threshold
ADD_MEDICARE_RATE: Decimal = Decimal("0.009")
ADD_MEDICARE_THRESH: Decimal = Decimal("200_000.00")

# ---------------------------------------------------------------------------
# Federal Income Tax — Percentage Method
# Single filer, 2020+ W-4, Standard Withholding
#
# Each tuple: (lower_annualized_wage, upper_annualized_wage, rate, base_tax_at_lower)
# The top bracket's upper bound is None (unbounded).
#
# These are the EXACT thresholds from IRS Pub 15-T (2026), Table for Automated
# Payroll Systems, STANDARD Withholding Rate Schedule, Single or MFS column
# (page 12 of Pub 15-T).
#
# IMPORTANT — Worksheet 1A Step 1g:
#   Before looking up the table, subtract FED_STEP1G_SINGLE ($8,600) from the
#   annualized wages for single/MFS employees whose W-4 Step 2 box is NOT
#   checked.  This offset is applied inside calc_federal_withholding().
#   The effective zero-rate band for single filers is therefore $0–$16,100
#   (= $7,500 table threshold + $8,600 worksheet offset).
#
# Source: IRS Pub 15-T (2026), published Dec 3, 2025
# ---------------------------------------------------------------------------

FEDERAL_BRACKETS: list[tuple[Decimal, Decimal | None, Decimal, Decimal]] = [
    #   lower                upper                rate              base_tax
    (Decimal("0"), Decimal("7_500"), Decimal("0.0000"), Decimal("0.00")),
    (Decimal("7_500"), Decimal("19_900"), Decimal("0.1000"), Decimal("0.00")),
    (Decimal("19_900"), Decimal("57_900"), Decimal("0.1200"), Decimal("1_240.00")),
    (Decimal("57_900"), Decimal("113_200"), Decimal("0.2200"), Decimal("5_800.00")),
    (Decimal("113_200"), Decimal("209_275"), Decimal("0.2400"), Decimal("17_966.00")),
    (Decimal("209_275"), Decimal("263_725"), Decimal("0.3200"), Decimal("41_024.00")),
    (Decimal("263_725"), Decimal("648_100"), Decimal("0.3500"), Decimal("58_448.00")),
    (Decimal("648_100"), None, Decimal("0.3700"), Decimal("192_979.25")),  # None = no upper limit
]

# ---------------------------------------------------------------------------
# Worksheet 1A — Step 1g adjustment (IRS Pub 15-T 2026)
#
# Subtract this amount from annualized wages BEFORE looking up the Standard
# Withholding table above, when the employee's W-4 Step 2 box is NOT checked.
#   Single / Married Filing Separately : $8,600
#   Married Filing Jointly             : $12,900
# ---------------------------------------------------------------------------

FED_STEP1G_SINGLE: Decimal = Decimal("8_600.00")  # Single / MFS, Step 2 box NOT checked

# ---------------------------------------------------------------------------
# Georgia Income Tax — 2026
# Flat rate per HB 1437; scheduled to decrease 0.10% per year through 2029.
# ---------------------------------------------------------------------------

GA_RATE: Decimal = Decimal("0.0519")  # 5.19% flat rate effective 2026
GA_STD_DEDUCTION: Decimal = Decimal("12_000.00")  # Annual standard deduction, single filer
GA_STATE: str = "Georgia"

# ---------------------------------------------------------------------------
# Pay period
# ---------------------------------------------------------------------------

PAY_PERIODS_PER_YEAR: int = 12  # Monthly payroll
