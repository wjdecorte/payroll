"""
Frontend pages for expenses: vendors, expenses and receipt downloads.

Full-page routes (not in the OpenAPI schema):
  GET  /vendors                -> vendor list (``?inactive=1``, ``?q=`` name search, ``?page=``)
  GET  /vendors/new            -> new-vendor form                       (owner)
  POST /vendors/new            -> creates the vendor                    (owner)
  GET  /vendors/{id}/edit      -> edit form                             (owner)
  POST /vendors/{id}/edit      -> saves the vendor                      (owner)
  POST /vendors/{id}/active    -> (de)activates the vendor              (owner)
  GET  /expenses               -> expense list, 50 per page; filters ``date_from``, ``date_to``,
                                  ``vendor_id``, ``account_id``, ``status``, ``open_owner_paid``,
                                  ``page``
  GET  /expenses.csv           -> the same filters as a formula-neutralised CSV download
  GET  /expenses/new           -> new-expense form: split lines and receipts   (owner)
  POST /expenses/new           -> saves the expense, then stores its receipts  (owner)
  GET  /expenses/{id}          -> lines, receipts, ledger entry link, void form
  POST /expenses/{id}/void     -> voids the expense                     (owner)
  POST /expenses/{id}/receipts -> attaches more receipts                (owner)
  GET  /attachments/{id}       -> a receipt download (any signed-in role); ``?inline=1`` shows
                                  PNG and JPEG in the browser, every other type stays a download

Every route needs a signed-in user (router-level ``require_reader``); the routes that change
data also need the ``owner`` role, so viewers and accountants get the usual 403 and the
templates hide the controls. Form errors re-render the form with the typed values and status
422 (409 for a state conflict). Every value is parsed to its Python type here before it reaches
a service, so bad input is a 422 page and never a 500.

Receipts on a new expense: they are read and checked first (size, count, type, duplicates), so
a bad file refuses the whole form and nothing is saved. The expense is then created, and only
then are the receipts stored. If storing fails after that (disk full, permissions) the expense
stays saved and its page opens with a warning that the receipts need to be added again; the
expense is never left half-done, because the receipts are separate append-only rows.
"""

import csv
import hashlib
import io
import logging
import re
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.dependencies import get_session
from payroll.common.documents import PAYMENT_TYPES, RESERVED_ROLES
from payroll.exceptions import AppBaseError
from payroll.expenses.attachments import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    AttachmentService,
    detect_type,
    safe_filename,
)
from payroll.expenses.exceptions import (
    AttachmentNotFoundError,
    InvalidAttachmentError,
    InvalidExpenseError,
)
from payroll.expenses.expenses import LINE_TYPES, MAX_LINES, ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, Vendor
from payroll.expenses.vendors import VendorService
from payroll.frontend.journal_routers import parse_amount
from payroll.frontend.routers import _error_partial, templates
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account
from payroll.reports.export import csv_safe
from payroll.reports.periods import company_today

logger = logging.getLogger(f"{app_settings.logger_name}.frontend.expenses")

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

PAGE_SIZE = 50
VENDOR_PAGE_SIZE = 100
MAX_PAGE_NUMBER = 100_000
MAX_PICKER = 1000  # vendors offered in a drop-down
MAX_CSV_ROWS = 10_000
CSV_CHUNK = 200  # the service's largest page
MAX_ID = 2_147_483_647  # the largest value of an integer column
MAX_SEARCH = 200
INLINE_TYPES = frozenset({"image/png", "image/jpeg"})
OWNER_SOURCE = "owner"
STATUS_CHOICES = ("posted", "void")


# ---------------------------------------------------------------------------
# Parsing: everything is turned into its Python type before a service sees it
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _invalid(message: str) -> InvalidExpenseError:
    return InvalidExpenseError(message=message)


def _parse_id(raw: object, label: str) -> int | None:
    """A positive database id from text; blank -> None; anything else -> a 422."""
    text = _text(raw)
    if not text:
        return None
    if not (text.isascii() and text.isdigit() and len(text) <= 10):
        raise _invalid(f"{label} is not valid")
    value = int(text)
    if not 1 <= value <= MAX_ID:
        raise _invalid(f"{label} is not valid")
    return value


def _path_id(raw: str, label: str = "The id") -> int:
    value = _parse_id(raw, label)
    if value is None:
        raise _invalid(f"{label} is not valid")
    return value


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parse_date(raw: object, label: str) -> date | None:
    """A strict ``YYYY-MM-DD`` calendar date (never a datetime); blank -> None."""
    text = _text(raw)
    if not text:
        return None
    if not _ISO_DATE.fullmatch(text):
        raise _invalid(f"{label} must be a date like 2026-03-05")
    try:
        value = date.fromisoformat(text)
    except ValueError as exc:
        raise _invalid(f"{label} is not a valid date") from exc
    if not is_supported_date(value):
        raise _invalid(f"{label} must be within {supported_range_text()}")
    return value


def _parse_page(raw: object) -> int:
    """The 1-based page number: blank -> 1; zero, negative, huge or garbage -> a 422."""
    text = _text(raw)
    if not text:
        return 1
    if not (text.isascii() and text.isdigit() and len(text) <= 7):
        raise _invalid("The page number is not valid")
    page = int(text)
    if not 1 <= page <= MAX_PAGE_NUMBER:
        raise _invalid("The page number is not valid")
    return page


def _parse_search(raw: object) -> str:
    text = _text(raw)
    if len(text) > MAX_SEARCH or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in text):
        raise _invalid(f"Search text must be at most {MAX_SEARCH} printable characters")
    return text


def _flag(raw: object, label: str) -> bool:
    text = _text(raw)
    if text in ("", "0"):
        return False
    if text == "1":
        return True
    raise _invalid(f"{label} must be 1 or left out")


def _form_status(exc: AppBaseError) -> int:
    return exc.http_code if exc.http_code in (409, 422) else 422


def _bad_request_page(request: Request, exc: AppBaseError) -> HTMLResponse:
    return _error_partial(request, exc)


# ---------------------------------------------------------------------------
# Small look-ups
# ---------------------------------------------------------------------------


def _label(account: Account) -> str:
    return f"{account.number} {account.name}" if account.number else account.name


def _vendor_names(session: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = session.exec(select(Vendor).where(col(Vendor.id).in_(sorted(ids)))).all()
    return {row.id: row.name for row in rows}  # type: ignore[misc]


def _accounts_by_id(session: Session, ids: set[int]) -> dict[int, Account]:
    if not ids:
        return {}
    rows = session.exec(select(Account).where(col(Account.id).in_(sorted(ids)))).all()
    return {row.id: row for row in rows}  # type: ignore[misc]


def _pickers(session: Session, *, include_inactive: bool = False) -> dict[str, Any]:
    """Drop-down choices: vendors, expense-line accounts, payment accounts, all accounts."""
    accounts = AccountService(session).list(include_inactive=include_inactive)
    return {
        "vendors": VendorService(session).list(
            include_inactive=include_inactive, limit=MAX_PICKER, offset=0
        ),
        "line_accounts": [a for a in accounts if a.type in LINE_TYPES],
        "pay_accounts": [
            a for a in accounts if a.type in PAYMENT_TYPES and a.system_role not in RESERVED_ROLES
        ],
        "all_accounts": accounts,
        "label": _label,
    }


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


def _vendor_list(
    request: Request,
    session: Session,
    *,
    inactive: bool,
    query: str,
    page: int,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    rows = VendorService(session).list(
        include_inactive=inactive,
        query=query or None,
        limit=VENDOR_PAGE_SIZE + 1,
        offset=(page - 1) * VENDOR_PAGE_SIZE,
    )
    keep = {k: v for k, v in (("inactive", "1" if inactive else ""), ("q", query)) if v}
    return templates.TemplateResponse(
        request,
        "expenses/vendors.html",
        {
            "vendors": rows[:VENDOR_PAGE_SIZE],
            "inactive": inactive,
            "q": query,
            "page": page,
            "has_next": len(rows) > VENDOR_PAGE_SIZE,
            "page_qs": urlencode(keep),
            "error": error,
            "tab": "vendors",
        },
        status_code=status_code,
    )


def _vendor_form(
    request: Request,
    *,
    values: dict,
    vendor: Vendor | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "expenses/vendor_form.html",
        {"vendor": vendor, "values": values, "error": error, "tab": "vendors"},
        status_code=status_code,
    )


def _vendor_values(form) -> dict:
    return {"name": _text(form.get("name")), "note": _text(form.get("note"))}


@router.get("/vendors", response_class=HTMLResponse)
async def vendors_list(
    request: Request,
    inactive: str = "",
    q: str = "",
    page: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        show_inactive = _flag(inactive, "Inactive")
        query = _parse_search(q)
        number = _parse_page(page)
    except AppBaseError as exc:
        return _bad_request_page(request, exc)
    return _vendor_list(request, session, inactive=show_inactive, query=query, page=number)


@router.get("/vendors/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def vendor_new(request: Request) -> HTMLResponse:
    return _vendor_form(request, values={"name": "", "note": ""})


@router.post("/vendors/new", response_class=HTMLResponse)
async def vendor_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    values = _vendor_values(await request.form())
    try:
        VendorService(session).create(actor=actor, name=values["name"], note=values["note"])
    except AppBaseError as exc:
        return _vendor_form(
            request, values=values, error=exc.message, status_code=_form_status(exc)
        )
    return RedirectResponse("/vendors", status_code=303)


@router.get(
    "/vendors/{vendor_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_owner)],
)
async def vendor_edit(
    request: Request, vendor_id: str, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        vendor = VendorService(session).get(_path_id(vendor_id, "The vendor"))
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return _vendor_form(
        request, values={"name": vendor.name, "note": vendor.note or ""}, vendor=vendor
    )


@router.post("/vendors/{vendor_id}/edit", response_class=HTMLResponse)
async def vendor_update(
    request: Request,
    vendor_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    service = VendorService(session)
    try:
        number = _path_id(vendor_id, "The vendor")
        vendor = service.get(number)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = _vendor_values(await request.form())
    try:
        service.update(number, actor=actor, name=values["name"], note=values["note"])
    except AppBaseError as exc:
        return _vendor_form(
            request, values=values, vendor=vendor, error=exc.message, status_code=_form_status(exc)
        )
    return RedirectResponse("/vendors", status_code=303)


@router.post("/vendors/{vendor_id}/active", response_class=HTMLResponse)
async def vendor_set_active(
    request: Request,
    vendor_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    raw = form.get("active")
    try:
        if raw not in ("0", "1"):
            raise _invalid("Active must be 0 or 1")
        active = raw == "1"
        VendorService(session).set_active(
            _path_id(vendor_id, "The vendor"), actor=actor, active=active
        )
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return RedirectResponse("/vendors", status_code=303)


# ---------------------------------------------------------------------------
# Expense list and CSV
# ---------------------------------------------------------------------------


def _parse_filters(
    date_from: str, date_to: str, vendor_id: str, account_id: str, status: str, open_owner_paid: str
) -> dict[str, Any]:
    status_text = _text(status)
    if status_text and status_text not in STATUS_CHOICES:
        raise _invalid("Status must be posted or void")
    return {
        "date_from": _parse_date(date_from, "From date"),
        "date_to": _parse_date(date_to, "To date"),
        "vendor_id": _parse_id(vendor_id, "The vendor"),
        "account_id": _parse_id(account_id, "The account"),
        "status": status_text or None,
        "open_owner_paid": _flag(open_owner_paid, "Open owner-paid"),
    }


def _filter_query(filters: dict[str, Any]) -> str:
    """The filters back as a query string (used for paging and the CSV link)."""
    pairs = []
    for key in ("date_from", "date_to", "vendor_id", "account_id", "status"):
        if filters[key] is not None:
            pairs.append((key, str(filters[key])))
    if filters["open_owner_paid"]:
        pairs.append(("open_owner_paid", "1"))
    return urlencode(pairs)


def _paid_from(expense: Expense, accounts: dict[int, Account]) -> str:
    if expense.source == OWNER_SOURCE:
        return "Owner (personal card)"
    account = accounts.get(expense.paid_from_account_id or 0)
    return account.name if account else ""


@router.get("/expenses", response_class=HTMLResponse)
async def expenses_list(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    vendor_id: str = "",
    account_id: str = "",
    status: str = "",
    open_owner_paid: str = "",
    page: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        filters = _parse_filters(date_from, date_to, vendor_id, account_id, status, open_owner_paid)
        number = _parse_page(page)
        service = ExpenseService(session)
        total = service.count(**filters)
        rows = service.list(**filters, limit=PAGE_SIZE, offset=(number - 1) * PAGE_SIZE)
    except AppBaseError as exc:
        return _bad_request_page(request, exc)
    accounts = _accounts_by_id(
        session, {r.paid_from_account_id for r in rows if r.paid_from_account_id}
    )
    vendors = _vendor_names(session, {r.vendor_id for r in rows if r.vendor_id})
    query = _filter_query(filters)
    return templates.TemplateResponse(
        request,
        "expenses/list.html",
        {
            "expenses": rows,
            "vendor_names": vendors,
            "paid_from": {r.id: _paid_from(r, accounts) for r in rows},
            "filters": {
                "date_from": _text(date_from),
                "date_to": _text(date_to),
                "vendor_id": str(filters["vendor_id"] or ""),
                "account_id": str(filters["account_id"] or ""),
                "status": filters["status"] or "",
                "open_owner_paid": filters["open_owner_paid"],
            },
            "page": number,
            "has_next": number * PAGE_SIZE < total,
            "total": total,
            "page_size": PAGE_SIZE,
            "filter_qs": query,
            "page_qs": query + "&" if query else "",
            **_pickers(session, include_inactive=True),
            "tab": "expenses",
        },
    )


@router.get("/expenses.csv")
def expenses_csv(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    vendor_id: str = "",
    account_id: str = "",
    status: str = "",
    open_owner_paid: str = "",
    session: Session = Depends(get_session),
) -> Response:
    try:
        filters = _parse_filters(date_from, date_to, vendor_id, account_id, status, open_owner_paid)
        service = ExpenseService(session)
        total = service.count(**filters)
        rows: list[Expense] = []
        while len(rows) < min(total, MAX_CSV_ROWS):
            chunk = service.list(
                **filters,
                limit=min(CSV_CHUNK, MAX_CSV_ROWS - len(rows)),
                offset=len(rows),
            )
            if not chunk:
                break
            rows.extend(chunk)
    except AppBaseError as exc:
        return _bad_request_page(request, exc)
    accounts = _accounts_by_id(
        session, {r.paid_from_account_id for r in rows if r.paid_from_account_id}
    )
    vendors = _vendor_names(session, {r.vendor_id for r in rows if r.vendor_id})
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["Date", "Payee", "Memo", "Paid from", "Total", "Status", "Void date"])
    for row in rows:
        payee = vendors.get(row.vendor_id or 0, "") if row.vendor_id else row.payee_text or ""
        writer.writerow(
            [
                row.date.isoformat(),
                csv_safe(payee),
                csv_safe(row.memo or ""),
                csv_safe(_paid_from(row, accounts)),
                format(row.total, "f"),
                row.status,
                row.void_date.isoformat() if row.void_date else "",
            ]
        )
    if total > len(rows):
        writer.writerow(
            [f"Export limited to the first {MAX_CSV_ROWS:,} expenses; narrow the filters."]
        )
    return Response(
        buffer.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="expenses.csv"'},
    )


# ---------------------------------------------------------------------------
# New expense
# ---------------------------------------------------------------------------


def _blank_line() -> dict[str, str]:
    return {"account_id": "", "amount": "", "memo": ""}


def _expense_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "expenses/form.html",
        {
            "values": values,
            "error": error,
            "max_lines": MAX_LINES,
            **_pickers(session),
            "tab": "expenses",
        },
        status_code=status_code,
    )


def _expense_values(form) -> dict:
    """The typed values of the expense form, kept as text for the redisplay."""
    accounts = form.getlist("line_account_id")
    amounts = form.getlist("line_amount")
    memos = form.getlist("line_memo")
    lines = [
        {"account_id": _text(a), "amount": _text(m), "memo": _text(n)}
        for a, m, n in zip(accounts, amounts, memos, strict=False)
    ]
    ragged = len({len(accounts), len(amounts), len(memos)}) > 1
    return {
        "expense_date": _text(form.get("expense_date")),
        "vendor_id": _text(form.get("vendor_id")),
        "payee_text": _text(form.get("payee_text")),
        "paid_from": _text(form.get("paid_from")),
        "memo": _text(form.get("memo")),
        "lines": lines or [_blank_line()],
        "ragged": ragged,
        "line_count": max(len(accounts), len(amounts), len(memos)),
    }


def _parse_expense(values: dict) -> dict[str, Any]:
    """Typed arguments for ``ExpenseService.create`` from the form's text values."""
    if values["ragged"]:
        raise _invalid("The expense lines were not sent correctly; reload the form and try again")
    if values["line_count"] > MAX_LINES * 2:
        raise _invalid(f"An expense can have at most {MAX_LINES} lines")
    expense_date = _parse_date(values["expense_date"], "The expense date")
    if expense_date is None:
        raise _invalid("Enter the expense date")
    vendor_id = _parse_id(values["vendor_id"], "The vendor")
    paid_from = values["paid_from"]
    if paid_from == OWNER_SOURCE:
        source, paid_from_account_id = OWNER_SOURCE, None
    else:
        paid_from_account_id = _parse_id(paid_from, "The payment account")
        if paid_from_account_id is None:
            raise _invalid("Choose what the expense was paid from")
        source = "account"
    lines: list[ExpenseLineInput] = []
    for line in values["lines"]:
        if not (line["account_id"] or line["amount"] or line["memo"]):
            continue  # a row the user left empty
        account_id = _parse_id(line["account_id"], "The line account")
        if account_id is None:
            raise _invalid("Choose an account for every line")
        try:
            amount = parse_amount(line["amount"])
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
        lines.append(ExpenseLineInput(account_id, amount, line["memo"] or None))
    if len(lines) > MAX_LINES:
        raise _invalid(f"An expense can have at most {MAX_LINES} lines")
    return {
        "expense_date": expense_date,
        "lines": lines,
        "source": source,
        "paid_from_account_id": paid_from_account_id,
        "vendor_id": vendor_id,
        "payee_text": values["payee_text"] or None,
        "memo": values["memo"] or None,
    }


@router.get("/expenses/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def expense_new(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    values = {
        "expense_date": str(company_today()),
        "vendor_id": "",
        "payee_text": "",
        "paid_from": "",
        "memo": "",
        "lines": [_blank_line()],
    }
    return _expense_form(request, session, values=values)


# --- uploads -----------------------------------------------------------------


class _Upload:
    __slots__ = ("data", "filename", "sha256")

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.data = data
        self.sha256 = hashlib.sha256(data).hexdigest()


def _upload_error(message: str) -> InvalidAttachmentError:
    return InvalidAttachmentError(message=message)


async def _read_uploads(form) -> list[_Upload]:
    """The receipt files of a form, read with hard caps and checked before anything is saved.

    At most 10 files; each is read up to 10 MB + 1 byte (never the whole body), so an oversize
    file is refused without being buffered; the total is capped at 50 MB; empty file inputs
    (no file chosen) are ignored; type, emptiness and duplicates are checked here too.
    """
    parts = form.getlist("receipts")
    if len(parts) > MAX_FILES:
        raise _upload_error(f"Attach at most {MAX_FILES} receipts at a time")
    uploads: list[_Upload] = []
    total = 0
    seen: set[str] = set()
    for part in parts:
        if isinstance(part, str):
            if part.strip():
                raise _upload_error("Receipts must be sent as files")
            continue
        try:
            data = await part.read(MAX_FILE_BYTES + 1)
        finally:
            await part.close()
        name = safe_filename(part.filename or "")
        if not part.filename and not data:
            continue  # the file input was left empty
        if not data:
            raise _upload_error(f"{name}: the file is empty")
        if len(data) > MAX_FILE_BYTES:
            raise _upload_error(f"{name}: a receipt cannot be larger than 10 MB")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise _upload_error("An expense's receipts cannot total more than 50 MB")
        if detect_type(data) is None:
            raise _upload_error(f"{name}: only PDF, PNG, JPEG and HEIC files can be attached")
        upload = _Upload(part.filename or "", data)
        if upload.sha256 in seen:
            raise _upload_error(f"{name}: the same file was chosen twice")
        seen.add(upload.sha256)
        uploads.append(upload)
    return uploads


def _store_receipts(
    session: Session, actor: User, expense_id: int, uploads: list[_Upload]
) -> tuple[int, str | None, int]:
    """Store each upload in turn. Returns (stored count, problem text, status code)."""
    service = AttachmentService(session)
    stored = 0
    for upload in uploads:
        try:
            service.add(
                actor=actor,
                owner_type="expense",
                owner_id=expense_id,
                filename=upload.filename,
                data=upload.data,
            )
        except OSError:
            logger.exception("Could not store a receipt for expense id=%s", expense_id)
            return (
                stored,
                "The receipts could not be stored because of a problem with the server's "
                "storage. Try again later or tell your administrator.",
                503,
            )
        except AppBaseError as exc:
            return stored, f"{safe_filename(upload.filename)}: {exc.message}", exc.http_code
        stored += 1
    return stored, None, 200


@router.post("/expenses/new", response_class=HTMLResponse)
async def expense_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = _expense_values(form)
    try:
        uploads = await _read_uploads(form)
        arguments = _parse_expense(values)
        expense = ExpenseService(session).create(actor=actor, **arguments)
    except AppBaseError as exc:
        return _expense_form(
            request, session, values=values, error=exc.message, status_code=_form_status(exc)
        )
    stored, problem, status = _store_receipts(session, actor, expense.id, uploads)  # type: ignore[arg-type]
    if problem is not None:
        warning = (
            f"The expense was saved, but {len(uploads) - stored} of {len(uploads)} receipt(s) "
            f"were not attached. {problem} You can attach them again below."
        )
        return _expense_page(
            request,
            session,
            expense.id,
            warning=warning,
            status_code=_form_status_warning(status),  # type: ignore[arg-type]
        )
    return RedirectResponse(f"/expenses/{expense.id}", status_code=303)


def _form_status_warning(status: int) -> int:
    return status if status in (503, 409, 422) else 422


# ---------------------------------------------------------------------------
# Expense detail, void and receipts
# ---------------------------------------------------------------------------


def _expense_page(
    request: Request,
    session: Session,
    expense_id: int,
    *,
    error: str | None = None,
    warning: str | None = None,
    void_values: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    service = ExpenseService(session)
    try:
        expense = service.get(expense_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    lines = service.lines(expense_id)
    account_ids = {ln.account_id for ln in lines}
    if expense.paid_from_account_id:
        account_ids.add(expense.paid_from_account_id)
    accounts = _accounts_by_id(session, account_ids)
    vendor = session.get(Vendor, expense.vendor_id) if expense.vendor_id else None
    defaults = {"void_date": str(max(company_today(), expense.date)), "memo": ""}
    return templates.TemplateResponse(
        request,
        "expenses/detail.html",
        {
            "expense": expense,
            "lines": lines,
            "accounts": accounts,
            "vendor": vendor,
            "paid_from": _paid_from(expense, accounts),
            "attachments": AttachmentService(session).list("expense", expense_id),
            "inline_types": INLINE_TYPES,
            "void_values": void_values if void_values is not None else defaults,
            "error": error,
            "warning": warning,
            "max_files": MAX_FILES,
            "tab": "expenses",
        },
        status_code=status_code,
    )


@router.get("/expenses/{expense_id}", response_class=HTMLResponse)
async def expense_view(
    request: Request, expense_id: str, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        number = _path_id(expense_id, "The expense")
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return _expense_page(request, session, number)


@router.post("/expenses/{expense_id}/void", response_class=HTMLResponse)
async def expense_void(
    request: Request,
    expense_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {"void_date": _text(form.get("void_date")), "memo": _text(form.get("memo"))}
    try:
        number = _path_id(expense_id, "The expense")
    except AppBaseError as exc:
        return _error_partial(request, exc)
    try:
        void_date = _parse_date(values["void_date"], "The void date")
        if void_date is None:
            raise _invalid("Enter a void date")
        ExpenseService(session).void(
            number, actor=actor, void_date=void_date, memo=values["memo"] or None
        )
    except AppBaseError as exc:
        return _expense_page(
            request,
            session,
            number,
            error=exc.message,
            void_values=values,
            status_code=_form_status(exc) if exc.http_code != 404 else 404,
        )
    return RedirectResponse(f"/expenses/{number}", status_code=303)


@router.post("/expenses/{expense_id}/receipts", response_class=HTMLResponse)
async def expense_add_receipts(
    request: Request,
    expense_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    try:
        number = _path_id(expense_id, "The expense")
        ExpenseService(session).get(number)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    try:
        uploads = await _read_uploads(form)
        if not uploads:
            raise _upload_error("Choose at least one receipt file to attach")
    except AppBaseError as exc:
        return _expense_page(
            request, session, number, error=exc.message, status_code=_form_status(exc)
        )
    stored, problem, status = _store_receipts(session, actor, number, uploads)
    if problem is not None:
        note = f" {stored} receipt(s) were attached before the problem." if stored else ""
        return _expense_page(
            request,
            session,
            number,
            error=problem + note,
            status_code=_form_status_warning(status),
        )
    return RedirectResponse(f"/expenses/{number}", status_code=303)


# ---------------------------------------------------------------------------
# Receipt download
# ---------------------------------------------------------------------------


def _content_disposition(kind: str, filename: str) -> str:
    """``attachment``/``inline`` with an ASCII fallback name and an RFC 5987 UTF-8 name."""
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "receipt"
    encoded = quote(filename.encode("utf-8", "replace"), safe="")
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


@router.get("/attachments/{attachment_id}")
async def attachment_download(
    request: Request,
    attachment_id: str,
    inline: str = "",
    session: Session = Depends(get_session),
) -> Response:
    try:
        number = _path_id(attachment_id, "The receipt")
        row, path = AttachmentService(session).open(number)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    try:
        data = await run_in_threadpool(path.read_bytes)
    except OSError:
        logger.exception("Could not read attachment id=%s", number)
        return _error_partial(request, AttachmentNotFoundError())
    show_inline = _text(inline) == "1" and row.content_type in INLINE_TYPES
    return Response(
        data,
        media_type=row.content_type,
        headers={
            "Content-Disposition": _content_disposition(
                "inline" if show_inline else "attachment", row.filename
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
