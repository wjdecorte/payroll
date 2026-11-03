"""
Frontend pages for the general journal.

Full-page routes (not in the OpenAPI schema):
  GET  /journal                 -> entries (``start``, ``end``, ``source``, ``page`` filters)
  GET  /journal/new             -> manual entry editor              (owner)
  POST /journal                 -> posts the entry, 303 to it       (owner)
  GET  /journal/{id}            -> one entry: lines, status, reversal links
  POST /journal/{id}/reverse    -> posts a reversing entry          (owner)
  POST /journal/{id}/void       -> voids the entry                  (owner)

Same rules as the account pages: router-level ``require_reader``, owner-only
mutations, form errors re-render with every entered row and the service's status
code (422 for validation, 409 for a locked period or a double reversal). The
server is authoritative; ``journal.js`` only adds rows and a live balance check.
Amounts are parsed strictly into ``Decimal`` here, before the service sees them.
"""

import re
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.exceptions import AppBaseError
from payroll.frontend.ledger_routers import _label, _parse_date, _text
from payroll.frontend.routers import _error_partial, templates
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import InvalidEntryError
from payroll.ledger.models import JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, JournalService, LineDraft
from payroll.reports.periods import company_today

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

JOURNAL_PAGE_SIZE = 50
BLANK_ROWS = 2
MAX_ROWS = 200
MAX_PAGE = 100000
MAX_ACCOUNT_ID = 2**31 - 1  # integer primary key range
_AMOUNT = re.compile(r"^\$?\s*([0-9]{1,3}(,[0-9]{3})+|[0-9]+)(\.[0-9]{1,2})?$")
ZERO = Decimal("0")


def _source_label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def parse_amount(raw: str) -> Decimal:
    """``1234.56``, ``1,234.56`` or ``$10.00`` -> Decimal; anything else raises ValueError."""
    text = raw.strip()
    if not text:
        return ZERO
    if len(text) > 40 or not _AMOUNT.match(text):
        raise ValueError("Enter an amount like 1234.56 (at most 2 decimal places)")
    return Decimal(text.lstrip("$").strip().replace(",", ""))


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/journal", response_class=HTMLResponse)
async def journal_list(
    request: Request,
    start: str = "",
    end: str = "",
    source: str = "",
    page: int = 1,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    page = min(max(page, 1), MAX_PAGE)
    service = JournalService(session)
    try:
        start_date = _parse_date(start, "Start")
        end_date = _parse_date(end, "End")
        try:
            source_type = SourceType(source) if source else None
        except ValueError as exc:
            raise InvalidEntryError(message="Unknown source type") from exc
    except AppBaseError as exc:
        return _error_partial(request, exc)
    found = service.list(
        start_date,
        end_date,
        source_type,
        limit=JOURNAL_PAGE_SIZE + 1,
        offset=(page - 1) * JOURNAL_PAGE_SIZE,
    )
    entries = found[:JOURNAL_PAGE_SIZE]
    ids = [e.id for e in entries]
    lines = service.lines(ids)
    reversed_by = service.reversed_by(ids)
    rows = [
        {
            "entry": e,
            "total": sum((line.debit for line, _ in lines.get(e.id, [])), ZERO),
            "reversed_by": reversed_by.get(e.id),
        }
        for e in entries
    ]
    query = "&".join(
        f"{key}={value}"
        for key, value in (("start", start), ("end", end), ("source", source))
        if value
    )
    return templates.TemplateResponse(
        request,
        "journal/list.html",
        {
            "rows": rows,
            "start": start,
            "end": end,
            "source": source,
            "sources": [(s.value, _source_label(s.value)) for s in SourceType],
            "page": page,
            "has_next": len(found) > JOURNAL_PAGE_SIZE,
            "query": query + "&" if query else "",
        },
    )


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------


def _blank_row() -> dict:
    return {"account": "", "debit": "", "credit": "", "memo": "", "error": None}


def _journal_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    rows: list[dict],
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    accounts = [
        {"id": str(n.account.id), "label": _label(n.account, n.full_name)}
        for n in AccountService(session).tree(include_inactive=False)
    ]
    return templates.TemplateResponse(
        request,
        "journal/form.html",
        {
            "values": values,
            "rows": rows,
            "accounts": accounts,
            "error": error,
            "blank": _blank_row(),
        },
        status_code=status_code,
    )


@router.get("/journal/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def journal_new(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    values = {"entry_date": str(company_today()), "memo": ""}
    return _journal_form(
        request, session, values=values, rows=[_blank_row() for _ in range(BLANK_ROWS)]
    )


def _rows_from_form(form) -> list[dict]:
    columns = {
        key: [v if isinstance(v, str) else "" for v in form.getlist(f"line_{key}")]
        for key in ("account", "debit", "credit", "memo")
    }
    count = max((len(v) for v in columns.values()), default=0)
    rows = []
    for i in range(count):
        row = {key: (vals[i].strip() if i < len(vals) else "") for key, vals in columns.items()}
        row["error"] = None
        rows.append(row)
    return rows


def _build_draft(values: dict, rows: list[dict]) -> EntryDraft:
    """Validate the typed values; set ``row['error']`` and raise on the first problem."""
    entry_date = _parse_date(values["entry_date"], "Date")
    if entry_date is None:
        raise InvalidEntryError(message="Enter an entry date")
    lines: list[LineDraft] = []
    failed = False
    for number, row in enumerate(rows, start=1):
        problem = None
        debit = credit = ZERO
        try:
            debit, credit = parse_amount(row["debit"]), parse_amount(row["credit"])
        except ValueError as exc:
            problem = str(exc)
        if problem is None and not row["account"] and not debit and not credit:
            continue  # no account and no amount (blank or zero; memo ignored): not a line
        account_id = None
        if problem is None:
            account = row["account"]
            if (
                len(account) > 10
                or not (account.isascii() and account.isdigit())
                or int(account) > MAX_ACCOUNT_ID
            ):
                problem = "Choose an account"
            elif debit and credit:
                problem = "Enter either a debit or a credit, not both"
            elif not debit and not credit:
                problem = "Enter a debit or a credit amount"
            else:
                account_id = int(row["account"])
        if problem is not None:
            row["error"] = f"Line {number}: {problem}"
            failed = True
        elif account_id is not None:
            lines.append(LineDraft(account_id, debit, credit, row["memo"] or None))
    if failed:
        raise InvalidEntryError(message="Fix the highlighted lines")
    return EntryDraft(entry_date=entry_date, memo=values["memo"] or None, lines=lines)


@router.post("/journal", response_class=HTMLResponse)
async def journal_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {"entry_date": _text(form.get("entry_date")), "memo": _text(form.get("memo"))}
    rows = _rows_from_form(form)
    try:
        if len(rows) > MAX_ROWS:
            rows = rows[:MAX_ROWS]
            raise InvalidEntryError(message=f"An entry can have at most {MAX_ROWS} lines")
        draft = _build_draft(values, rows)
        entry = JournalService(session).create_manual(draft, actor=actor)
    except AppBaseError as exc:
        while len(rows) < BLANK_ROWS:
            rows.append(_blank_row())
        return _journal_form(
            request,
            session,
            values=values,
            rows=rows,
            error=exc.message,
            status_code=exc.http_code,
        )
    return RedirectResponse(f"/journal/{entry.id}", status_code=303)


# ---------------------------------------------------------------------------
# One entry
# ---------------------------------------------------------------------------


def _entry_page(
    request: Request,
    session: Session,
    entry: JournalEntry,
    *,
    error: str | None = None,
    action_date: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    service = JournalService(session)
    lines = service.lines([entry.id]).get(entry.id, [])
    reversed_by_id = service.reversed_by([entry.id]).get(entry.id)
    links = {}
    for key, entry_id in (("reverses", entry.reverses_entry_id), ("reversed_by", reversed_by_id)):
        if entry_id is not None:
            links[key] = service.get(entry_id)
    manual = entry.source_type == SourceType.MANUAL
    return templates.TemplateResponse(
        request,
        "journal/view.html",
        {
            "entry": entry,
            "lines": lines,
            "total_debit": sum((line.debit for line, _ in lines), ZERO),
            "total_credit": sum((line.credit for line, _ in lines), ZERO),
            "links": links,
            "source_label": _source_label(SourceType(entry.source_type).value),
            "can_change": manual
            and entry.status == "posted"
            and reversed_by_id is None
            and not (links.get("reverses") and links["reverses"].status == "void"),
            "action_date": action_date or str(company_today()),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/journal/{entry_id}", response_class=HTMLResponse)
async def journal_view(
    request: Request, entry_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        entry = JournalService(session).get(entry_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return _entry_page(request, session, entry)


async def _change(request: Request, entry_id: int, session: Session, actor: User, action: str):
    service = JournalService(session)
    try:
        entry = service.get(entry_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    form = await request.form()
    raw_date = _text(form.get("entry_date"))
    try:
        entry_date = _parse_date(raw_date, "Date")
        if entry_date is None:
            raise InvalidEntryError(message="Enter a date for the reversing entry")
        run = service.reverse_manual if action == "reverse" else service.void_manual
        created = run(
            entry_id, entry_date=entry_date, actor=actor, memo=_text(form.get("memo")) or None
        )
    except AppBaseError as exc:
        session.refresh(entry)
        return _entry_page(
            request,
            session,
            entry,
            error=exc.message,
            action_date=raw_date,
            status_code=exc.http_code,
        )
    return RedirectResponse(f"/journal/{created.id}", status_code=303)


@router.post("/journal/{entry_id}/reverse", response_class=HTMLResponse)
async def journal_reverse(
    request: Request,
    entry_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    return await _change(request, entry_id, session, actor, "reverse")


@router.post("/journal/{entry_id}/void", response_class=HTMLResponse)
async def journal_void(
    request: Request,
    entry_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    return await _change(request, entry_id, session, actor, "void")
