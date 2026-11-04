"""
Frontend pages for receivables: customers, items and receivables settings.

Full-page routes (not in the OpenAPI schema):
  GET  /customers                   -> customer list (``?inactive=1`` shows inactive)
  GET  /customers/new               -> new-customer form            (owner)
  POST /customers/new               -> creates the customer         (owner)
  GET  /customers/{id}/edit         -> edit form                    (owner)
  POST /customers/{id}/edit         -> saves the customer           (owner)
  POST /customers/{id}/active       -> (de)activates the customer   (owner)
  GET/POST /items ...               -> the same set for items
  GET  /invoices                    -> invoice list (``?status=`` and ``?customer_id=`` filters)
  GET  /invoices/new                -> new-invoice form             (owner)
  POST /invoices/new                -> saves a draft                (owner)
  GET  /invoices/{id}               -> read-only invoice with its lines
  GET  /invoices/{id}/edit          -> edit form, drafts and open invoices (owner)
  POST /invoices/{id}/edit          -> saves the draft or replaces the open invoice (owner)
  POST /invoices/{id}/discard       -> discards the draft           (owner)
  POST /invoices/{id}/issue         -> numbers and posts the draft  (owner)
  POST /invoices/{id}/void          -> voids an open invoice        (owner)
  GET  /payments                    -> payment list (``?customer_id=`` filter)
  GET  /payments/new                -> step 1: pick a customer; ``?customer_id=`` is step 2,
                                       the payment form with the customer's open invoices (owner)
  POST /payments/new                -> records the payment and its applications (owner)
  GET  /payments/{id}               -> payment, its applications, unapplied credit, void form
  POST /payments/{id}/apply         -> applies the unapplied credit to open invoices (owner)
  POST /payments/{id}/void          -> voids the payment            (owner)
  GET  /settings/receivables        -> next invoice number, company details
  POST /settings/receivables/number -> sets the next invoice number (owner)
  POST /settings/receivables/company -> saves the company details   (owner)

Every route needs a signed-in user (router-level ``require_reader``); the routes that
change data also need the ``owner`` role, so viewers and accountants get the usual 403
and the templates hide the controls. Form errors re-render the form with the typed
values and status 422.
"""

import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.exceptions import AppBaseError
from payroll.frontend.journal_routers import parse_amount
from payroll.frontend.routers import _error_partial, templates
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SystemRole
from payroll.receivables.customers import CustomerService
from payroll.receivables.exceptions import (
    InvalidCustomerError,
    InvalidInvoiceError,
    InvalidItemError,
    InvalidPaymentError,
    InvoiceNumberError,
    InvoiceStateError,
)
from payroll.receivables.invoices import STATUSES, InvoiceLineInput, InvoiceService, live_applied
from payroll.receivables.items import INCOME_TYPES, ItemService
from payroll.receivables.models import Invoice, Payment, PaymentApplication
from payroll.receivables.payments import (
    METHODS,
    ApplicationInput,
    PaymentService,
    allocate_oldest_first,
    unapplied,
)
from payroll.receivables.settings import ReceivablesSettingsService
from payroll.reports.periods import company_today
from payroll.reports.periods import parse_date as _parse_date

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _whole_number(raw: str, exc_type: type[AppBaseError], message: str) -> int:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 12):
        raise exc_type(message=message)
    return int(raw)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def _customer_list(
    request: Request,
    session: Session,
    *,
    inactive: bool,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "customers/list.html",
        {
            "customers": CustomerService(session).list(include_inactive=inactive),
            "inactive": inactive,
            "error": error,
            "tab": "customers",
        },
        status_code=status_code,
    )


def _customer_form(
    request: Request,
    *,
    values: dict,
    customer=None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "customers/form.html",
        {"customer": customer, "values": values, "error": error, "tab": "customers"},
        status_code=status_code,
    )


def _customer_values(form) -> dict:
    return {
        "name": _text(form.get("name")),
        "email": _text(form.get("email")),
        "phone": _text(form.get("phone")),
        "billing_address": _text(form.get("billing_address")),
        "terms_days": _text(form.get("terms_days")),
    }


def _parse_customer(values: dict) -> dict:
    return {
        "name": values["name"],
        "email": values["email"] or None,
        "phone": values["phone"] or None,
        "billing_address": values["billing_address"] or None,
        "terms_days": _whole_number(
            values["terms_days"], InvalidCustomerError, "Terms must be a whole number of days"
        ),
    }


@router.get("/customers", response_class=HTMLResponse)
async def customers_list(
    request: Request, inactive: int = 0, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _customer_list(request, session, inactive=bool(inactive))


@router.get("/customers/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def customer_new(request: Request) -> HTMLResponse:
    values = {"name": "", "email": "", "phone": "", "billing_address": "", "terms_days": "30"}
    return _customer_form(request, values=values)


@router.post("/customers/new", response_class=HTMLResponse)
async def customer_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    values = _customer_values(await request.form())
    try:
        CustomerService(session).create(actor=actor, **_parse_customer(values))
    except AppBaseError as exc:
        return _customer_form(request, values=values, error=exc.message, status_code=422)
    return RedirectResponse("/customers", status_code=303)


@router.get(
    "/customers/{customer_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_owner)],
)
async def customer_edit(
    request: Request, customer_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        customer = CustomerService(session).get(customer_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = {
        "name": customer.name,
        "email": customer.email or "",
        "phone": customer.phone or "",
        "billing_address": customer.billing_address or "",
        "terms_days": str(customer.terms_days),
    }
    return _customer_form(request, values=values, customer=customer)


@router.post("/customers/{customer_id}/edit", response_class=HTMLResponse)
async def customer_update(
    request: Request,
    customer_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    service = CustomerService(session)
    try:
        customer = service.get(customer_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = _customer_values(await request.form())
    try:
        service.update(customer_id, actor=actor, **_parse_customer(values))
    except AppBaseError as exc:
        return _customer_form(
            request, values=values, customer=customer, error=exc.message, status_code=422
        )
    return RedirectResponse("/customers", status_code=303)


@router.post("/customers/{customer_id}/active", response_class=HTMLResponse)
async def customer_set_active(
    request: Request,
    customer_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    active = _text((await request.form()).get("active")) == "1"
    try:
        CustomerService(session).set_active(customer_id, active, actor=actor)
    except AppBaseError as exc:
        return _customer_list(
            request, session, inactive=not active, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse("/customers", status_code=303)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _item_list(
    request: Request,
    session: Session,
    *,
    inactive: bool,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    accounts = {a.id: a for a in AccountService(session).list(include_inactive=True)}
    return templates.TemplateResponse(
        request,
        "items/list.html",
        {
            "items": ItemService(session).list(include_inactive=inactive),
            "accounts": accounts,
            "inactive": inactive,
            "error": error,
            "tab": "items",
        },
        status_code=status_code,
    )


def _item_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    item=None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    accounts = [a for a in AccountService(session).list() if a.type in INCOME_TYPES]
    return templates.TemplateResponse(
        request,
        "items/form.html",
        {
            "item": item,
            "values": values,
            "error": error,
            "accounts": accounts,
            "tab": "items",
        },
        status_code=status_code,
    )


def _item_values(form) -> dict:
    return {
        "name": _text(form.get("name")),
        "description": _text(form.get("description")),
        "unit_price": _text(form.get("unit_price")),
        "income_account_id": _text(form.get("income_account_id")),
    }


def _parse_item(values: dict) -> dict:
    try:
        price = parse_amount(values["unit_price"])
    except ValueError as exc:
        raise InvalidItemError(message=str(exc)) from exc
    return {
        "name": values["name"],
        "description": values["description"] or None,
        "unit_price": Decimal(price),
        "income_account_id": _whole_number(
            values["income_account_id"], InvalidItemError, "Choose an income account"
        ),
    }


@router.get("/items", response_class=HTMLResponse)
async def items_list(
    request: Request, inactive: int = 0, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _item_list(request, session, inactive=bool(inactive))


@router.get("/items/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def item_new(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    values = {"name": "", "description": "", "unit_price": "0.00", "income_account_id": ""}
    return _item_form(request, session, values=values)


@router.post("/items/new", response_class=HTMLResponse)
async def item_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    values = _item_values(await request.form())
    try:
        ItemService(session).create(actor=actor, **_parse_item(values))
    except AppBaseError as exc:
        return _item_form(request, session, values=values, error=exc.message, status_code=422)
    return RedirectResponse("/items", status_code=303)


@router.get(
    "/items/{item_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_owner)]
)
async def item_edit(
    request: Request, item_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        item = ItemService(session).get(item_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = {
        "name": item.name,
        "description": item.description or "",
        "unit_price": f"{item.unit_price:.2f}",
        "income_account_id": str(item.income_account_id),
    }
    return _item_form(request, session, values=values, item=item)


@router.post("/items/{item_id}/edit", response_class=HTMLResponse)
async def item_update(
    request: Request,
    item_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    service = ItemService(session)
    try:
        item = service.get(item_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = _item_values(await request.form())
    try:
        service.update(item_id, actor=actor, **_parse_item(values))
    except AppBaseError as exc:
        return _item_form(
            request, session, values=values, item=item, error=exc.message, status_code=422
        )
    return RedirectResponse("/items", status_code=303)


@router.post("/items/{item_id}/active", response_class=HTMLResponse)
async def item_set_active(
    request: Request,
    item_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    active = _text((await request.form()).get("active")) == "1"
    try:
        ItemService(session).set_active(item_id, active, actor=actor)
    except AppBaseError as exc:
        return _item_list(
            request, session, inactive=not active, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse("/items", status_code=303)


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

BLANK_LINES = 3
MAX_FORM_LINES = 200
MAX_ID = 2**31 - 1  # integer primary key range
_QTY = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _id_param(raw: str, label: str) -> int | None:
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 10 and int(raw) <= MAX_ID):
        raise InvalidInvoiceError(message=f"{label} is not valid")
    return int(raw)


def _qty_text(qty: Decimal) -> str:
    text = f"{qty:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _blank_line() -> dict:
    return {"item": "", "description": "", "qty": "", "unit_price": "", "error": None}


def _invoice_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    lines: list[dict],
    invoice=None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "invoices/form.html",
        {
            "invoice": invoice,
            "values": values,
            "lines": lines,
            "blank": _blank_line(),
            "customers": CustomerService(session).list(),
            "items": ItemService(session).list(),
            "error": error,
            "tab": "invoices",
        },
        status_code=status_code,
    )


def _invoice_values(form) -> dict:
    return {
        "customer_id": _text(form.get("customer_id")),
        "invoice_date": _text(form.get("invoice_date")),
        "terms_days": _text(form.get("terms_days")),
        "memo": _text(form.get("memo")),
    }


def _lines_from_form(form) -> list[dict]:
    columns = {
        key: [v if isinstance(v, str) else "" for v in form.getlist(f"line_{key}")]
        for key in ("item", "description", "qty", "unit_price")
    }
    count = max((len(v) for v in columns.values()), default=0)
    lines = []
    for i in range(min(count, MAX_FORM_LINES + 1)):
        line = {key: (vals[i].strip() if i < len(vals) else "") for key, vals in columns.items()}
        line["error"] = None
        lines.append(line)
    return lines


def _build_invoice(values: dict, lines: list[dict]) -> dict:
    """Parse the typed values; set ``line['error']`` and raise on the first problem."""
    if len(lines) > MAX_FORM_LINES:
        raise InvalidInvoiceError(message=f"An invoice can have at most {MAX_FORM_LINES} lines")
    customer_id = _id_param(values["customer_id"], "Customer")
    if customer_id is None:
        raise InvalidInvoiceError(message="Choose a customer")
    invoice_date = _parse_date(values["invoice_date"], "Invoice date")
    if invoice_date is None:
        raise InvalidInvoiceError(message="Enter an invoice date")
    terms_days = (
        _whole_number(
            values["terms_days"], InvalidInvoiceError, "Terms must be a whole number of days"
        )
        if values["terms_days"]
        else None
    )
    inputs: list[InvoiceLineInput] = []
    failed = False
    for number, line in enumerate(lines, start=1):
        if not any(line[key] for key in ("item", "qty", "unit_price")) and not line["description"]:
            continue  # a blank row
        problem = None
        item_id = qty = price = None
        try:
            item_id = _id_param(line["item"], "Item")
            if item_id is None:
                problem = "Choose an item"
            elif not (_QTY.match(line["qty"]) and len(line["qty"]) <= 30):
                problem = "Enter a quantity like 2 or 1.5"
            elif not line["unit_price"]:
                problem = "Enter a unit price"
            else:
                qty = Decimal(line["qty"])
                price = parse_amount(line["unit_price"])
        except (ValueError, InvalidOperation, InvalidInvoiceError) as exc:
            problem = exc.message if isinstance(exc, AppBaseError) else str(exc)
        if problem is not None:
            line["error"] = f"Line {number}: {problem}"
            failed = True
        else:
            inputs.append(InvoiceLineInput(item_id, line["description"], qty, price))
    if failed:
        raise InvalidInvoiceError(message="Fix the highlighted lines")
    return {
        "customer_id": customer_id,
        "invoice_date": invoice_date,
        "terms_days": terms_days,
        "memo": values["memo"] or None,
        "lines": inputs,
    }


def _fill_blank_lines(lines: list[dict]) -> list[dict]:
    while len(lines) < BLANK_LINES:
        lines.append(_blank_line())
    return lines


@router.get("/invoices", response_class=HTMLResponse)
async def invoices_list(
    request: Request,
    status: str = "",
    customer_id: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    try:
        if status and status not in STATUSES:
            raise InvalidInvoiceError(message="Unknown invoice status")
        customer = _id_param(customer_id.strip(), "Customer")
    except AppBaseError as exc:
        return _error_partial(request, exc)
    customers = CustomerService(session).list(include_inactive=True)
    return templates.TemplateResponse(
        request,
        "invoices/list.html",
        {
            "invoices": InvoiceService(session).list(status=status or None, customer_id=customer),
            "customers": {c.id: c for c in customers},
            "status": status,
            "customer_id": customer,
            "statuses": STATUSES,
            "tab": "invoices",
        },
    )


@router.get("/invoices/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def invoice_new(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    values = {
        "customer_id": "",
        "invoice_date": str(company_today()),
        "terms_days": "",
        "memo": "",
    }
    return _invoice_form(request, session, values=values, lines=_fill_blank_lines([]))


@router.post("/invoices/new", response_class=HTMLResponse)
async def invoice_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = _invoice_values(form)
    lines = _lines_from_form(form)
    try:
        invoice = InvoiceService(session).create_draft(actor=actor, **_build_invoice(values, lines))
    except AppBaseError as exc:
        return _invoice_form(
            request,
            session,
            values=values,
            lines=_fill_blank_lines(lines),
            error=exc.message,
            status_code=exc.http_code,
        )
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


def _invoice_applications(session: Session, invoice_id: int) -> list[tuple]:
    """(application, payment) pairs for the live (posted) payments applied to an invoice."""
    stmt = (
        select(PaymentApplication, Payment)
        .join(Payment, col(Payment.id) == col(PaymentApplication.payment_id))
        .where(PaymentApplication.invoice_id == invoice_id, Payment.status == "posted")
        .order_by(col(PaymentApplication.id))
    )
    return list(session.exec(stmt).all())


def _invoice_page(
    request: Request,
    session: Session,
    invoice_id: int,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    service = InvoiceService(session)
    try:
        invoice = service.get(invoice_id)
        customer = CustomerService(session).get(invoice.customer_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    applied = live_applied(session, invoice_id)
    return templates.TemplateResponse(
        request,
        "invoices/view.html",
        {
            "invoice": invoice,
            "customer": customer,
            "lines": service.lines(invoice_id),
            "has_payments": invoice.status == "open" and live_applied(session, invoice_id) > 0,
            "applied": applied,
            "balance": invoice.total - applied,
            "applications": _invoice_applications(session, invoice_id),
            "today": str(company_today()),
            "error": error,
            "tab": "invoices",
        },
        status_code=status_code,
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_view(
    request: Request, invoice_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _invoice_page(request, session, invoice_id)


@router.get(
    "/invoices/{invoice_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_owner)],
)
async def invoice_edit(
    request: Request, invoice_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    service = InvoiceService(session)
    try:
        invoice = service.get(invoice_id)
        if invoice.status not in ("draft", "open"):
            raise InvoiceStateError(message="Only a draft or open invoice can be edited")
        if invoice.status == "open" and live_applied(session, invoice_id) > 0:
            raise InvoiceStateError(message="An invoice with payments applied cannot be edited")
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = {
        "customer_id": str(invoice.customer_id),
        "invoice_date": str(invoice.invoice_date),
        "terms_days": str(invoice.terms_days),
        "memo": invoice.memo or "",
    }
    lines = [
        {
            "item": str(line.item_id or ""),
            "description": line.description or "",
            "qty": _qty_text(line.qty),
            "unit_price": f"{line.unit_price:.2f}",
            "error": None,
        }
        for line in service.lines(invoice_id)
    ]
    return _invoice_form(
        request, session, values=values, lines=_fill_blank_lines(lines), invoice=invoice
    )


@router.post("/invoices/{invoice_id}/edit", response_class=HTMLResponse)
async def invoice_update(
    request: Request,
    invoice_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    service = InvoiceService(session)
    try:
        invoice = service.get(invoice_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    form = await request.form()
    values = _invoice_values(form)
    lines = _lines_from_form(form)
    try:
        built = _build_invoice(values, lines)
        if invoice.status == "open":
            service.replace(invoice_id, actor=actor, **built)
        else:
            service.update_draft(invoice_id, actor=actor, **built)
    except AppBaseError as exc:
        return _invoice_form(
            request,
            session,
            values=values,
            lines=_fill_blank_lines(lines),
            invoice=invoice,
            error=exc.message,
            status_code=exc.http_code,
        )
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/discard", response_class=HTMLResponse)
async def invoice_discard(
    request: Request,
    invoice_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    try:
        InvoiceService(session).discard_draft(invoice_id, actor=actor)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return RedirectResponse("/invoices", status_code=303)


@router.post("/invoices/{invoice_id}/issue", response_class=HTMLResponse)
async def invoice_issue(
    request: Request,
    invoice_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    try:
        InvoiceService(session).issue(invoice_id, actor=actor)
    except AppBaseError as exc:
        return _invoice_page(
            request, session, invoice_id, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/void", response_class=HTMLResponse)
async def invoice_void(
    request: Request,
    invoice_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    try:
        void_date = _parse_date(_text(form.get("void_date")), "Void date")
        if void_date is None:
            raise InvalidInvoiceError(message="Enter a void date")
        InvoiceService(session).void(
            invoice_id, actor=actor, void_date=void_date, memo=_text(form.get("memo")) or None
        )
    except AppBaseError as exc:
        return _invoice_page(
            request, session, invoice_id, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def _open_invoices(session: Session, customer_id: int) -> list[dict]:
    """The customer's open invoices with live balances, oldest due date first."""
    stmt = (
        select(Invoice)
        .where(Invoice.customer_id == customer_id, Invoice.status == "open")
        .order_by(col(Invoice.due_date), col(Invoice.number), col(Invoice.id))
    )
    rows = []
    for invoice in session.exec(stmt).all():
        balance = invoice.total - live_applied(session, invoice.id)  # type: ignore[arg-type]
        if balance > 0:
            rows.append({"invoice": invoice, "balance": balance})
    return rows


def _deposit_accounts(session: Session) -> tuple[list[Account], int | None]:
    """Active asset accounts to deposit to (not A/R) and the Undeposited Funds default."""
    accounts = [
        a
        for a in AccountService(session).list()
        if a.type == AccountType.ASSET and a.system_role != SystemRole.ACCOUNTS_RECEIVABLE
    ]
    default = next((a.id for a in accounts if a.system_role == SystemRole.UNDEPOSITED_FUNDS), None)
    return accounts, default


def _payment_error(message: str) -> InvalidPaymentError:
    return InvalidPaymentError(message=message)


def _amounts_from_form(form) -> dict[str, str]:
    """The typed per-invoice amounts, keyed by invoice id (``apply_<id>`` fields)."""
    typed = {}
    for key in form.keys():
        if key.startswith("apply_") and key[6:].isascii() and key[6:].isdigit():
            raw = form.get(key)
            if isinstance(raw, str) and raw.strip():
                typed[key[6:]] = raw.strip()
    return typed


def _typed_inputs(typed: dict[str, str]) -> list[ApplicationInput]:
    out = []
    for raw_id, raw_amount in typed.items():
        if len(raw_id) > 10 or int(raw_id) > MAX_ID:
            raise _payment_error("Invoice is not valid")
        try:
            out.append(ApplicationInput(int(raw_id), parse_amount(raw_amount)))
        except ValueError as exc:
            raise _payment_error(str(exc)) from exc
    return out


def _payment_form(
    request: Request,
    session: Session,
    *,
    customer,
    values: dict,
    typed: dict[str, str],
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    accounts, _ = _deposit_accounts(session)
    return templates.TemplateResponse(
        request,
        "payments/form.html",
        {
            "customer": customer,
            "values": values,
            "typed": typed,
            "methods": METHODS,
            "accounts": accounts,
            "open_invoices": _open_invoices(session, customer.id),
            "error": error,
            "tab": "payments",
        },
        status_code=status_code,
    )


@router.get("/payments", response_class=HTMLResponse)
async def payments_list(
    request: Request, customer_id: str = "", session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        customer = _id_param(customer_id.strip(), "Customer")
    except AppBaseError as exc:
        return _error_partial(request, exc)
    customers = CustomerService(session).list(include_inactive=True)
    payments = PaymentService(session).list(customer_id=customer)
    return templates.TemplateResponse(
        request,
        "payments/list.html",
        {
            "payments": payments,
            "unapplied": {p.id: unapplied(session, p.id) for p in payments},  # type: ignore[arg-type]
            "customers": {c.id: c for c in customers},
            "customer_id": customer,
            "tab": "payments",
        },
    )


@router.get("/payments/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def payment_new(
    request: Request, customer_id: str = "", session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        chosen = _id_param(customer_id.strip(), "Customer")
        customer = CustomerService(session).get(chosen) if chosen is not None else None
    except AppBaseError as exc:
        return _error_partial(request, exc)
    if customer is None:
        return templates.TemplateResponse(
            request,
            "payments/form.html",
            {"customer": None, "customers": CustomerService(session).list(), "tab": "payments"},
        )
    _, default = _deposit_accounts(session)
    values = {
        "received_date": str(company_today()),
        "amount": "",
        "method": "check",
        "reference": "",
        "memo": "",
        "deposit_account_id": str(default or ""),
        "oldest_first": "1",
    }
    return _payment_form(request, session, customer=customer, values=values, typed={})


@router.post("/payments/new", response_class=HTMLResponse)
async def payment_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {
        "customer_id": _text(form.get("customer_id")),
        "received_date": _text(form.get("received_date")),
        "amount": _text(form.get("amount")),
        "method": _text(form.get("method")),
        "reference": _text(form.get("reference")),
        "memo": _text(form.get("memo")),
        "deposit_account_id": _text(form.get("deposit_account_id")),
        "oldest_first": "1" if form.get("oldest_first") else "",
    }
    typed = _amounts_from_form(form)
    try:
        customer_id = _id_param(values["customer_id"], "Customer")
        customer = CustomerService(session).get(customer_id) if customer_id is not None else None
    except AppBaseError as exc:
        return _error_partial(request, exc)
    if customer is None:
        return _error_partial(request, _payment_error("Choose a customer"))
    try:
        received = _parse_date(values["received_date"], "Received date")
        if received is None:
            raise _payment_error("Enter the date received")
        try:
            amount = parse_amount(values["amount"])
        except ValueError as exc:
            raise _payment_error(str(exc)) from exc
        account_id = _id_param(values["deposit_account_id"], "Deposit account")
        if account_id is None:
            raise _payment_error("Choose where the money was deposited")
        inputs = _typed_inputs(typed)
        if not inputs and values["oldest_first"]:
            inputs = allocate_oldest_first(session, customer.id, amount)  # type: ignore[arg-type]
        payment = PaymentService(session).record(
            actor=actor,
            customer_id=customer.id,  # type: ignore[arg-type]
            received_date=received,
            amount=amount,
            deposit_account_id=account_id,
            method=values["method"],
            reference=values["reference"] or None,
            memo=values["memo"] or None,
            applications=inputs,
        )
    except AppBaseError as exc:
        return _payment_form(
            request,
            session,
            customer=customer,
            values=values,
            typed=typed,
            error=exc.message,
            status_code=exc.http_code,
        )
    return RedirectResponse(f"/payments/{payment.id}", status_code=303)


def _payment_page(
    request: Request,
    session: Session,
    payment_id: int,
    *,
    error: str | None = None,
    typed: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    service = PaymentService(session)
    try:
        payment = service.get(payment_id)
        customer = CustomerService(session).get(payment.customer_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    invoices = {
        i.id: i
        for i in session.exec(
            select(Invoice).where(
                col(Invoice.id).in_([a.invoice_id for a in service.applications(payment_id)])
            )
        ).all()
    }
    deposit = session.get(Account, payment.deposit_account_id)
    left = unapplied(session, payment_id)
    return templates.TemplateResponse(
        request,
        "payments/view.html",
        {
            "payment": payment,
            "customer": customer,
            "deposit": deposit,
            "applications": service.applications(payment_id),
            "invoices": invoices,
            "unapplied": left,
            "open_invoices": _open_invoices(session, customer.id)  # type: ignore[arg-type]
            if left > 0
            else [],
            "typed": typed or {},
            "today": str(company_today()),
            "error": error,
            "tab": "payments",
        },
        status_code=status_code,
    )


@router.get("/payments/{payment_id}", response_class=HTMLResponse)
async def payment_view(
    request: Request, payment_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _payment_page(request, session, payment_id)


@router.post("/payments/{payment_id}/apply", response_class=HTMLResponse)
async def payment_apply(
    request: Request,
    payment_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    typed = _amounts_from_form(form)
    try:
        service = PaymentService(session)
        payment = service.get(payment_id)
        inputs = _typed_inputs(typed)
        if not inputs and form.get("oldest_first"):
            inputs = allocate_oldest_first(
                session,
                payment.customer_id,
                unapplied(session, payment_id),
                exclude={a.invoice_id for a in service.applications(payment_id)},
            )
        service.apply(payment_id, actor=actor, applications=inputs)
    except AppBaseError as exc:
        return _payment_page(
            request, session, payment_id, error=exc.message, typed=typed, status_code=exc.http_code
        )
    return RedirectResponse(f"/payments/{payment_id}", status_code=303)


@router.post("/payments/{payment_id}/void", response_class=HTMLResponse)
async def payment_void(
    request: Request,
    payment_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    try:
        void_date = _parse_date(_text(form.get("void_date")), "Void date")
        if void_date is None:
            raise _payment_error("Enter a void date")
        PaymentService(session).void(
            payment_id, actor=actor, void_date=void_date, memo=_text(form.get("memo")) or None
        )
    except AppBaseError as exc:
        return _payment_page(
            request, session, payment_id, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse(f"/payments/{payment_id}", status_code=303)


# ---------------------------------------------------------------------------
# Receivables settings
# ---------------------------------------------------------------------------


def _settings_page(
    request: Request,
    session: Session,
    *,
    number_value: str | None = None,
    company_values: dict | None = None,
    number_error: str | None = None,
    company_error: str | None = None,
    saved: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    row = ReceivablesSettingsService(session).get()
    return templates.TemplateResponse(
        request,
        "settings/receivables.html",
        {
            "number_value": number_value if number_value is not None else row.next_invoice_no,
            "company": company_values
            if company_values is not None
            else {
                "company_name": row.company_name or "",
                "company_address": row.company_address or "",
                "remit_to": row.remit_to or "",
                "invoice_footer": row.invoice_footer or "",
            },
            "number_error": number_error,
            "company_error": company_error,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/settings/receivables", response_class=HTMLResponse)
async def receivables_settings(request: Request, session: Session = Depends(get_session)):
    return _settings_page(request, session)


@router.post("/settings/receivables/number", response_class=HTMLResponse)
async def receivables_number_save(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    raw = _text((await request.form()).get("next_invoice_no"))
    try:
        number = _whole_number(raw, InvoiceNumberError, "Enter a whole number, 1 or higher")
        ReceivablesSettingsService(session).set_next_invoice_number(number, actor=actor)
    except AppBaseError as exc:
        return _settings_page(
            request, session, number_value=raw, number_error=exc.message, status_code=422
        )
    return _settings_page(request, session, saved="Next invoice number saved.")


@router.post("/settings/receivables/company", response_class=HTMLResponse)
async def receivables_company_save(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {
        key: _text(form.get(key))
        for key in ("company_name", "company_address", "remit_to", "invoice_footer")
    }
    try:
        ReceivablesSettingsService(session).update_company(actor=actor, **values)
    except AppBaseError as exc:
        return _settings_page(
            request, session, company_values=values, company_error=exc.message, status_code=422
        )
    return _settings_page(request, session, saved="Company details saved.")
