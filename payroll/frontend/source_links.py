"""Where a journal entry's source document lives, for the drill-down links.

Only the expense module's documents are linked here (expense, transfer, reimbursement);
reimbursements have no page of their own, so they go to the owner page. A ledger
``source_id`` is free text, so one that is not a plain positive integer in the database
range (a legacy or hand-made value) yields no link rather than an error.
"""

MAX_ID = 2**31 - 1  # integer primary key range

DOCUMENT_PAGES = {"expense": "/expenses", "transfer": "/transfers"}
OWNER_PAGE = "/owner"
LABELS = {"expense": "Expense", "transfer": "Transfer", "reimbursement": "Reimbursement"}


def _kind(source_type: object) -> str:
    return str(getattr(source_type, "value", source_type))


def source_href(source_type: object, source_id: str | None) -> str | None:
    """The page for the document an entry came from, or ``None`` when there is none."""
    kind = _kind(source_type)
    if kind == "reimbursement":
        return OWNER_PAGE
    prefix = DOCUMENT_PAGES.get(kind)
    if prefix is None or not source_id:
        return None
    if len(source_id) > 10 or not (source_id.isascii() and source_id.isdigit()):
        return None
    number = int(source_id)
    if not 1 <= number <= MAX_ID:
        return None
    return f"{prefix}/{number}"


def document_label(source_type: object) -> str:
    return LABELS.get(_kind(source_type), "")
