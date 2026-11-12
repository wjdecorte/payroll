"""Where a journal entry's source document lives, for the drill-down links.

The expense module's documents (expense, transfer, reimbursement) and the payroll documents
(payroll run, tax payment) are linked here; reimbursements have no page of their own, so they
go to the owner page, and a payroll run opens its panel on the payroll page (``/?run={id}``). A ledger
``source_id`` is free text, so one that is not a plain positive integer in the database
range (a legacy or hand-made value) yields no link rather than an error.
"""

MAX_ID = 2**31 - 1  # integer primary key range

DOCUMENT_PAGES = {
    "expense": "/expenses/{id}",
    "transfer": "/transfers/{id}",
    "payroll_run": "/?run={id}",
    "tax_payment": "/payroll/tax-payments/{id}",
}
OWNER_PAGE = "/owner"
LABELS = {
    "expense": "Expense",
    "transfer": "Transfer",
    "reimbursement": "Reimbursement",
    "payroll_run": "Payroll run",
    "tax_payment": "Tax payment",
}


def _kind(source_type: object) -> str:
    return str(getattr(source_type, "value", source_type))


def source_href(source_type: object, source_id: str | None) -> str | None:
    """The page for the document an entry came from, or ``None`` when there is none."""
    kind = _kind(source_type)
    if kind == "reimbursement":
        return OWNER_PAGE
    page = DOCUMENT_PAGES.get(kind)
    if page is None or not source_id:
        return None
    if len(source_id) > 10 or not (source_id.isascii() and source_id.isdigit()):
        return None
    number = int(source_id)
    if not 1 <= number <= MAX_ID:
        return None
    return page.format(id=number)


def document_label(source_type: object) -> str:
    return LABELS.get(_kind(source_type), "")
