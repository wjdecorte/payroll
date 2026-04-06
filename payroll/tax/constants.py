"""
2026 Tax Year Constants
=======================
Review and update each January.

Sources:
  Federal brackets / withholding : IRS Publication 15-T (irs.gov/pub/irs-pdf/p15t.pdf)
  Social Security wage base      : SSA.gov (https://www.ssa.gov/oact/cola/cbb.html)
  Georgia income tax             : Georgia DOR (dor.georgia.gov)
"""

import math

TAX_YEAR: int = 2026

# ---------------------------------------------------------------------------
# FICA
# ---------------------------------------------------------------------------

SS_EE_RATE: float = 0.062  # Employee Social Security rate
SS_ER_RATE: float = 0.062  # Employer Social Security rate
SS_WAGE_BASE: float = 184_500.00  # 2026 annual SS wage base (SSA.gov)

MEDICARE_EE_RATE: float = 0.0145  # Employee Medicare rate
MEDICARE_ER_RATE: float = 0.0145  # Employer Medicare rate

# Additional 0.9% Medicare applies to the EMPLOYEE only on annual wages above this threshold
ADD_MEDICARE_RATE: float = 0.009
ADD_MEDICARE_THRESH: float = 200_000.00

# ---------------------------------------------------------------------------
# Federal Income Tax — Percentage Method
# Single filer, 2020+ W-4, Standard Withholding
#
# Each tuple: (lower_annualized_wage, upper_annualized_wage, rate, base_tax_at_lower)
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

FEDERAL_BRACKETS: list[tuple[float, float, float, float]] = [
    #   lower        upper       rate    base_tax
    (0, 7_500, 0.0000, 0.00),
    (7_500, 19_900, 0.1000, 0.00),
    (19_900, 57_900, 0.1200, 1_240.00),
    (57_900, 113_200, 0.2200, 5_800.00),
    (113_200, 209_275, 0.2400, 17_966.00),
    (209_275, 263_725, 0.3200, 41_024.00),
    (263_725, 648_100, 0.3500, 58_448.00),
    (648_100, math.inf, 0.3700, 192_979.25),
]

# ---------------------------------------------------------------------------
# Worksheet 1A — Step 1g adjustment (IRS Pub 15-T 2026)
#
# Subtract this amount from annualized wages BEFORE looking up the Standard
# Withholding table above, when the employee's W-4 Step 2 box is NOT checked.
#   Single / Married Filing Separately : $8,600
#   Married Filing Jointly             : $12,900
# ---------------------------------------------------------------------------

FED_STEP1G_SINGLE: float = 8_600.00  # Single / MFS, Step 2 box NOT checked

# ---------------------------------------------------------------------------
# Georgia Income Tax — 2026
# Flat rate per HB 1437; scheduled to decrease 0.10% per year through 2029.
# ---------------------------------------------------------------------------

GA_RATE: float = 0.0519  # 5.19% flat rate effective 2026
GA_STD_DEDUCTION: float = 12_000.00  # Annual standard deduction, single filer
GA_STATE: str = "Georgia"

# ---------------------------------------------------------------------------
# Pay period
# ---------------------------------------------------------------------------

PAY_PERIODS_PER_YEAR: int = 12  # Monthly payroll
