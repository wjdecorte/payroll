"""Frontend pages for tax payments and payroll settings (M4).

  GET  /payroll/tax-payments               -> tax payment list, 50 per page (``?page=``)
  GET  /payroll/tax-payments/new           -> new-payment form                  (owner)
  POST /payroll/tax-payments/new           -> saves the payment                 (owner)
  GET  /payroll/tax-payments/{id}          -> the payment, its entry links, void form
  POST /payroll/tax-payments/{id}/void     -> voids the payment                 (owner)
  GET  /settings/payroll                   -> the year's FUTA/SUTA rates, deposit rules
  POST /settings/payroll/year              -> saves the year's rates and bases  (owner)
  POST /settings/payroll/deposit-rules     -> saves the deposit due-date rules  (owner)

Every GET is open to any signed-in role; every POST is owner-only (and CSRF-checked app-wide).
All form text is parsed into its Python type here before a service sees it; a bad value is a
422 page that keeps what was typed, never a 500. Rates are typed as percentages (0.6 for 0.6%)
and converted to the stored fraction with exact ``Decimal`` arithmetic.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, col, select

from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.common.documents import PAYMENT_TYPES, RESERVED_ROLES
from payroll.exceptions import AppBaseError
from payroll.frontend.journal_routers import parse_amount
from payroll.frontend.parsing import _parse_date, _parse_id, _path_id, _text
from payroll.frontend.routers import _error_partial, templates
from payroll.ledger.accounts import PAYROLL_ROLES, AccountService
from payroll.ledger.models import Account
from payroll.payroll_run.exceptions import InvalidPayrollSettingsError, InvalidTaxPaymentError
from payroll.payroll_run.ledger_models import PayrollDepositRules
from payroll.payroll_run.settings_service import (
    AGENCIES,
    CADENCES,
    MAX_RATE,
    PayrollSettingsService,
)
from payroll.payroll_run.tax_payments import AGENCY_ROLES, TaxPaymentService
from payroll.reports.periods import company_today
from payroll.tax import constants as tax

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

PAGE_SIZE = 50
MAX_PAGE_NUMBER = 100_000
AGENCY_LABELS = {
    "irs_941": "IRS 941 (federal withholding and FICA)",
    "irs_futa": "IRS FUTA (federal unemployment)",
    "ga_dor": "Georgia DOR (state withholding)",
    "ga_dol": "Georgia DOL (state unemployment)",
}
DEPOSIT_LABELS = {
    "irs_941": "IRS 941",
    "ga_dor": "Georgia DOR",
    "futa": "IRS FUTA",
    "ga_dol": "Georgia DOL",
}
_PAYROLL_ROLE_VALUES = frozenset(role.value for role in PAYROLL_ROLES)
_PERCENT = re.compile(r"[0-9]+(\.[0-9]+)?")
_RATE_STEP = Decimal("0.000001")
_HUNDRED = Decimal(100)


def _form_status(exc: AppBaseError) -> int:
    return exc.http_code if exc.http_code in (409, 422) else 422


def _bad_payment(message: str) -> InvalidTaxPaymentError:
    return InvalidTaxPaymentError(message=message)


def _bad_settings(message: str) -> InvalidPayrollSettingsError:
    return InvalidPayrollSettingsError(message=message)


def _parse_page(raw: object) -> int:
    text = _text(raw)
    if not text:
        return 1
    if not (text.isascii() and text.isdigit() and len(text) <= 7):
        raise _bad_payment("The page number is not valid")
    page = int(text)
    if not 1 <= page <= MAX_PAGE_NUMBER:
        raise _bad_payment("The page number is not valid")
    return page


def _accounts_by_id(session: Session, ids: set[int]) -> dict[int, Account]:
    if not ids:
        return {}
    rows = session.exec(select(Account).where(col(Account.id).in_(ids))).all()
    return {row.id: row for row in rows}  # type: ignore[misc]


def _account_label(account: Account) -> str:
    return f"{account.number} {account.name}" if account.number else account.name


# ---------------------------------------------------------------------------
# Tax payments
# ---------------------------------------------------------------------------


def _bank_choices(session: Session) -> list[tuple[int, str]]:
    return [
        (a.id, _account_label(a))  # type: ignore[misc]
        for a in AccountService(session).list()
        if a.type in PAYMENT_TYPES
        and a.system_role not in RESERVED_ROLES
        and a.system_role not in _PAYROLL_ROLE_VALUES
    ]


def _payment_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "payroll/tax_payments/form.html",
        {
            "values": values,
            "error": error,
            "banks": _bank_choices(session),
            "agencies": AGENCY_LABELS,
        },
        status_code=status_code,
    )


def _payment_values(form) -> dict:
    return {
        "payment_date": _text(form.get("payment_date")),
        "agency": _text(form.get("agency")),
        "bank_account_id": _text(form.get("bank_account_id")),
        "amount": _text(form.get("amount")),
        "memo": _text(form.get("memo")),
    }


def _parse_payment(values: dict) -> dict[str, Any]:
    """Typed arguments for ``TaxPaymentService.create`` from the form's text values."""
    payment_date = _parse_date(values["payment_date"], "The payment date", InvalidTaxPaymentError)
    if payment_date is None:
        raise _bad_payment("Enter the payment date")
    agency = values["agency"]
    if agency not in AGENCY_ROLES:
        raise _bad_payment("Choose the agency being paid")
    bank_id = _parse_id(values["bank_account_id"], "The bank account", InvalidTaxPaymentError)
    if bank_id is None:
        raise _bad_payment("Choose the bank account the payment is made from")
    if not values["amount"]:
        raise _bad_payment("Enter the amount paid")
    try:
        amount = parse_amount(values["amount"])
    except ValueError as exc:
        raise _bad_payment(str(exc)) from exc
    return {
        "payment_date": payment_date,
        "agency": agency,
        "bank_account_id": bank_id,
        "amount": amount,
        "memo": values["memo"] or None,
    }


@router.get("/payroll/tax-payments", response_class=HTMLResponse)
async def tax_payments_list(
    request: Request, page: str = "", session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        number = _parse_page(page)
        rows = TaxPaymentService(session).list(limit=PAGE_SIZE + 1, offset=(number - 1) * PAGE_SIZE)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    shown = rows[:PAGE_SIZE]
    return templates.TemplateResponse(
        request,
        "payroll/tax_payments/list.html",
        {
            "payments": shown,
            "accounts": _accounts_by_id(session, {r.bank_account_id for r in shown}),
            "agencies": AGENCY_LABELS,
            "page": number,
            "has_next": len(rows) > PAGE_SIZE,
        },
    )


@router.get(
    "/payroll/tax-payments/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)]
)
async def tax_payment_new(
    request: Request, session: Session = Depends(get_session)
) -> HTMLResponse:
    values = {
        "payment_date": str(company_today()),
        "agency": "",
        "bank_account_id": "",
        "amount": "",
        "memo": "",
    }
    return _payment_form(request, session, values=values)


@router.post("/payroll/tax-payments/new", response_class=HTMLResponse)
async def tax_payment_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    values = _payment_values(await request.form())
    try:
        payment = TaxPaymentService(session).create(actor=actor, **_parse_payment(values))
    except AppBaseError as exc:
        return _payment_form(
            request, session, values=values, error=exc.message, status_code=_form_status(exc)
        )
    return RedirectResponse(f"/payroll/tax-payments/{payment.id}", status_code=303)


def _payment_page(
    request: Request,
    session: Session,
    payment_id: int,
    *,
    error: str | None = None,
    void_values: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    try:
        payment = TaxPaymentService(session).get(payment_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    accounts = _accounts_by_id(session, {payment.bank_account_id})
    defaults = {"void_date": str(max(company_today(), payment.payment_date)), "memo": ""}
    return templates.TemplateResponse(
        request,
        "payroll/tax_payments/detail.html",
        {
            "payment": payment,
            "accounts": accounts,
            "agencies": AGENCY_LABELS,
            "void_values": void_values if void_values is not None else defaults,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/payroll/tax-payments/{payment_id}", response_class=HTMLResponse)
async def tax_payment_view(
    request: Request, payment_id: str, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        number = _path_id(payment_id, "The tax payment", InvalidTaxPaymentError)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return _payment_page(request, session, number)


@router.post("/payroll/tax-payments/{payment_id}/void", response_class=HTMLResponse)
async def tax_payment_void(
    request: Request,
    payment_id: str,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {"void_date": _text(form.get("void_date")), "memo": _text(form.get("memo"))}
    try:
        number = _path_id(payment_id, "The tax payment", InvalidTaxPaymentError)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    try:
        void_date = _parse_date(values["void_date"], "The void date", InvalidTaxPaymentError)
        if void_date is None:
            raise _bad_payment("Enter a void date")
        TaxPaymentService(session).void(
            number, actor=actor, void_date=void_date, memo=values["memo"] or None
        )
    except AppBaseError as exc:
        return _payment_page(
            request,
            session,
            number,
            error=exc.message,
            void_values=values,
            status_code=_form_status(exc) if exc.http_code != 404 else 404,
        )
    return RedirectResponse(f"/payroll/tax-payments/{number}", status_code=303)


# ---------------------------------------------------------------------------
# Settings > Payroll
# ---------------------------------------------------------------------------


def _percent_text(rate: Decimal) -> str:
    """A stored fraction shown as a percentage: 0.006000 -> 0.6."""
    return format((rate * _HUNDRED).normalize(), "f")


def _parse_percent(raw: str, label: str) -> Decimal:
    """``0.6`` or ``2.64%`` -> the fraction 0.006 / 0.0264, exactly; finer than 6 places is refused."""
    text = raw.removesuffix("%").strip()
    if not text:
        raise _bad_settings(f"Enter the {label} as a percentage, like 0.6")
    if len(text) > 20 or not _PERCENT.fullmatch(text):
        raise _bad_settings(f"The {label} must be a percentage like 0.6")
    percent = Decimal(text)
    if percent > MAX_RATE * _HUNDRED:
        raise _bad_settings(f"The {label} must be between 0% and {MAX_RATE * _HUNDRED:f}%")
    try:
        fraction = percent.scaleb(-2)
        exact = fraction == fraction.quantize(_RATE_STEP)
    except InvalidOperation:  # pragma: no cover - the length and range checks keep it small
        exact = False
    if not exact:
        raise _bad_settings(f"The {label} can have at most four decimal places")
    return fraction.quantize(_RATE_STEP)


def _parse_year_form(values: dict) -> dict[str, Any]:
    tax_year = _parse_id(values["tax_year"], "The tax year", InvalidPayrollSettingsError)
    if tax_year is None:
        raise _bad_settings("Enter the tax year")
    futa_rate = _parse_percent(values["futa_rate"], "FUTA rate")
    suta_rate = _parse_percent(values["suta_rate"], "SUTA rate")
    bases = {}
    for key, label in (("futa_wage_base", "FUTA wage base"), ("suta_wage_base", "SUTA wage base")):
        if not values[key]:
            raise _bad_settings(f"Enter the {label}")
        try:
            bases[key] = parse_amount(values[key])
        except ValueError as exc:
            raise _bad_settings(f"The {label}: {exc}") from exc
    return {"tax_year": tax_year, "futa_rate": futa_rate, "suta_rate": suta_rate, **bases}


def _year_values(session: Session, tax_year: int | None, fallback: int) -> dict:
    row = PayrollSettingsService(session).get_year(tax_year or fallback)
    if row is None:
        return {
            "tax_year": str(tax_year or fallback),
            "futa_rate": "",
            "futa_wage_base": "",
            "suta_rate": "",
            "suta_wage_base": "",
        }
    return {
        "tax_year": str(row.tax_year),
        "futa_rate": _percent_text(row.futa_rate),
        "futa_wage_base": str(row.futa_wage_base),
        "suta_rate": _percent_text(row.suta_rate),
        "suta_wage_base": str(row.suta_wage_base),
    }


def _rules_values(session: Session) -> dict:
    row = session.get(PayrollDepositRules, 1) or PayrollDepositRules()
    values = {}
    for agency in AGENCIES:
        values[f"{agency}_cadence"] = getattr(row, f"{agency}_cadence")
        values[f"{agency}_due_day"] = str(getattr(row, f"{agency}_due_day"))
    return values


def _settings_page(
    request: Request,
    session: Session,
    *,
    tax_year: int | None = None,
    year_values: dict | None = None,
    year_error: str | None = None,
    rules_values: dict | None = None,
    rules_error: str | None = None,
    saved: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    fallback = tax.TAX_YEAR
    return templates.TemplateResponse(
        request,
        "settings/payroll.html",
        {
            "year_values": year_values
            if year_values is not None
            else _year_values(session, tax_year, fallback),
            "year_error": year_error,
            "rules": rules_values if rules_values is not None else _rules_values(session),
            "rules_error": rules_error,
            "deposit_labels": DEPOSIT_LABELS,
            "agencies": AGENCIES,
            "cadences": sorted(CADENCES),
            "saved": saved,
            "engine_year": tax.TAX_YEAR,
        },
        status_code=status_code,
    )


@router.get("/settings/payroll", response_class=HTMLResponse)
async def payroll_settings(
    request: Request, tax_year: str = "", session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        year = _parse_id(tax_year, "The tax year", InvalidPayrollSettingsError)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return _settings_page(request, session, tax_year=year)


@router.post("/settings/payroll/year", response_class=HTMLResponse)
async def payroll_settings_year(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {
        key: _text(form.get(key))
        for key in ("tax_year", "futa_rate", "futa_wage_base", "suta_rate", "suta_wage_base")
    }
    try:
        PayrollSettingsService(session).set_year(actor=actor, **_parse_year_form(values))
    except AppBaseError as exc:
        return _settings_page(
            request,
            session,
            year_values=values,
            year_error=exc.message,
            status_code=_form_status(exc),
        )
    return _settings_page(
        request, session, tax_year=int(values["tax_year"]), saved=f"Saved {values['tax_year']}."
    )


def _parse_rules(values: dict) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for agency in AGENCIES:
        label = DEPOSIT_LABELS[agency]
        cadence = values[f"{agency}_cadence"]
        if cadence not in CADENCES:
            raise _bad_settings(f"The {label} cadence must be monthly or quarterly")
        day = values[f"{agency}_due_day"]
        if not (day.isascii() and day.isdigit() and len(day) <= 2) or int(day) > 31:
            raise _bad_settings(
                f"The {label} due day must be a whole number from 1 to 31, or 0 for the last day"
            )
        fields[f"{agency}_cadence"] = cadence
        fields[f"{agency}_due_day"] = int(day)
    return fields


@router.post("/settings/payroll/deposit-rules", response_class=HTMLResponse)
async def payroll_settings_rules(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    values = {
        f"{agency}_{suffix}": _text(form.get(f"{agency}_{suffix}"))
        for agency in AGENCIES
        for suffix in ("cadence", "due_day")
    }
    try:
        PayrollSettingsService(session).set_deposit_rules(actor=actor, **_parse_rules(values))
    except AppBaseError as exc:
        return _settings_page(
            request,
            session,
            rules_values=values,
            rules_error=exc.message,
            status_code=_form_status(exc),
        )
    return _settings_page(request, session, saved="Deposit rules saved.")
