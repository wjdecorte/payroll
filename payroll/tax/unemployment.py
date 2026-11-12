"""
Pure FUTA and Georgia SUTA calculation.

Unemployment tax applies to each employee's wages up to an annual wage base.
A run's taxable wages are the part of its wages that falls under the base,
worked out from year-to-date wages before and after the run. See the M4 design
spec §5. This module is independent of the main tax engine and of any
database or run model.
"""

from dataclasses import dataclass
from decimal import Decimal

from payroll.common.money import round_money

ZERO = Decimal("0")


@dataclass(frozen=True)
class UnemploymentRates:
    """Rates and annual wage bases for one tax year."""

    futa_rate: Decimal
    futa_wage_base: Decimal
    suta_rate: Decimal
    suta_wage_base: Decimal


@dataclass(frozen=True)
class UnemploymentResult:
    """Taxable wages and tax for one run; all amounts are 2 dp."""

    futa_wages: Decimal
    futa: Decimal
    suta_wages: Decimal
    suta: Decimal


def _wages_under_base(before: Decimal, after: Decimal, base: Decimal) -> Decimal:
    """The part of a run's wages that falls under the annual wage base."""
    return max(ZERO, min(base, after) - min(base, before))


def calc_unemployment(
    wages_before: Decimal, wages_after: Decimal, rates: UnemploymentRates
) -> UnemploymentResult:
    """
    FUTA and SUTA for a run, from year-to-date wages before and after it.

    Raises ``ValueError`` if either figure is negative or ``wages_after`` is
    less than ``wages_before``.
    """
    if wages_before < 0 or wages_after < 0:
        raise ValueError("year-to-date wages cannot be negative")
    if wages_after < wages_before:
        raise ValueError("wages_after cannot be less than wages_before")

    futa_wages = round_money(_wages_under_base(wages_before, wages_after, rates.futa_wage_base))
    suta_wages = round_money(_wages_under_base(wages_before, wages_after, rates.suta_wage_base))
    return UnemploymentResult(
        futa_wages=futa_wages,
        futa=round_money(futa_wages * rates.futa_rate),
        suta_wages=suta_wages,
        suta=round_money(suta_wages * rates.suta_rate),
    )
