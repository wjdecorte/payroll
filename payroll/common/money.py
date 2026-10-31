"""
Exact money arithmetic helpers.

All money is ``decimal.Decimal`` (never ``float``) and is rounded half-up to
whole cents. See docs/DESIGN.md §3.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import Field

CENT = Decimal("0.01")

Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]


def round_money(value: Decimal | int | str) -> Decimal:
    """Round to cents, half-up (``2.345 -> 2.35``, ``-0.005 -> -0.01``).

    Floats are rejected: they cannot represent most cent values exactly, so a
    float reaching this function means an un-converted value slipped through.
    Convert with ``Decimal(str(x))`` at the boundary instead.
    """
    if isinstance(value, float):
        raise TypeError("round_money() does not accept float; use Decimal(str(x))")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
