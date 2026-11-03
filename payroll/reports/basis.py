"""
Accounting basis for the financial statements.

Cash is the default view (owner's decision); accrual is the plain ledger sum.
:func:`recognized_lines` is the single hook that decides which ledger lines a
basis recognizes at their entry date. :func:`payroll.reports.queries.line_selection`
applies it, so every report query honours the basis and M2 (invoice-to-payment
attribution for cash basis) changes only this function.
"""

from enum import StrEnum

from sqlalchemy.sql.elements import ColumnElement


class Basis(StrEnum):
    CASH = "cash"
    ACCRUAL = "accrual"


DEFAULT_BASIS = Basis.CASH

BASIS_NOTE = (
    "Cash and accrual agree until invoices are entered; "
    "the difference starts with Receivables (M2)."
)


def recognized_lines(basis: Basis) -> ColumnElement[bool] | None:
    """The SQL predicate restricting the lines ``basis`` recognizes, or ``None`` for all.

    ``None`` for both bases in M1: no document-backed (invoice/expense) sources
    exist yet, so every line is recognized at its entry date either way.
    """
    return None
