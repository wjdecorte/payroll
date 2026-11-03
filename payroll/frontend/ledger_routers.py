"""
Frontend pages for the ledger: chart of accounts, account register, ledger settings.

Full-page routes (not in the OpenAPI schema):
  GET  /accounts                    -> chart of accounts (``?inactive=1`` shows inactive)
  GET  /accounts/new                -> new-account form            (owner)
  POST /accounts/new                -> creates the account         (owner)
  GET  /accounts/{id}/edit          -> edit form                   (owner)
  POST /accounts/{id}/edit          -> saves the account           (owner)
  POST /accounts/{id}/active        -> (de)activates the account   (owner)
  GET  /accounts/{id}/register      -> account register for a date range
  GET  /settings/ledger             -> system roles and period lock
  POST /settings/ledger/roles       -> binds the system roles      (owner)
  POST /settings/ledger/lock        -> moves the lock date         (owner, needs password)

Every route needs a signed-in user (router-level ``require_reader``); the routes
that change data also need the ``owner`` role, so viewers and accountants get the
usual 403 and the templates hide the controls. Form errors re-render the form with
the typed values and status 422; a wrong lock password re-renders with 403. The
password is read from the form and passed straight to the service: never echoed.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.exceptions import AppBaseError
from payroll.frontend.routers import _error_partial, templates
from payroll.ledger.accounts import ROLE_TYPES, AccountService
from payroll.ledger.exceptions import InvalidAccountError, LockPasswordError
from payroll.ledger.models import Account, AccountType, SystemRole
from payroll.ledger.period_lock import PeriodLockService
from payroll.reports.periods import company_today
from payroll.reports.periods import parse_date as _parse_date
from payroll.reports.statements import ReportService, fiscal_year_start

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_reader)])

MAX_INDENT = 5
ROLE_LABELS: dict[SystemRole, str] = {
    SystemRole.ACCOUNTS_RECEIVABLE: "Accounts receivable",
    SystemRole.UNDEPOSITED_FUNDS: "Undeposited funds",
    SystemRole.DUE_TO_OWNER: "Due to owner",
    SystemRole.RETAINED_EARNINGS: "Retained earnings",
    SystemRole.OPENING_BALANCE_EQUITY: "Opening balance equity",
}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------


def _account_list(
    request: Request,
    session: Session,
    *,
    inactive: bool,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    today = company_today()
    trial = ReportService(session).trial_balance(today, include_zero=True)
    nets = {row.account_id: row.debit - row.credit for row in trial.rows}
    rows = []
    for node in AccountService(session).tree(include_inactive=inactive):
        account = node.account
        net = nets.get(account.id, 0)
        rows.append(
            {
                "account": account,
                "indent": min(node.depth, MAX_INDENT),
                "balance": net if AccountService.normal_balance(account) == "debit" else -net,
            }
        )
    return templates.TemplateResponse(
        request,
        "accounts/list.html",
        {
            "rows": rows,
            "inactive": inactive,
            "as_of": today,
            "error": error,
            "role_labels": ROLE_LABELS,
        },
        status_code=status_code,
    )


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_list(
    request: Request, inactive: int = 0, session: Session = Depends(get_session)
) -> HTMLResponse:
    return _account_list(request, session, inactive=bool(inactive))


def _parent_choices(session: Session, exclude: Account | None) -> list[dict]:
    blocked: set[int] = set()
    if exclude is not None:
        # An account cannot sit under itself or its own descendants.
        blocked.add(exclude.id)
        for node in AccountService(session).tree(include_inactive=True):
            if node.account.parent_id in blocked:
                blocked.add(node.account.id)
    return [
        {"id": n.account.id, "label": _label(n.account, n.full_name), "type": n.account.type}
        for n in AccountService(session).tree(include_inactive=False)
        if n.account.id not in blocked
    ]


def _label(account: Account, full_name: str) -> str:
    return f"{account.number} {full_name}" if account.number else full_name


def _account_form(
    request: Request,
    session: Session,
    *,
    values: dict,
    account: Account | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "accounts/form.html",
        {
            "account": account,
            "values": values,
            "error": error,
            "types": list(AccountType),
            "parents": _parent_choices(session, account),
        },
        status_code=status_code,
    )


def _values_from_form(form) -> dict:
    return {
        "name": _text(form.get("name")),
        "number": _text(form.get("number")),
        "type": _text(form.get("type")),
        "parent_id": _text(form.get("parent_id")),
        "detail_type": _text(form.get("detail_type")),
    }


def _parse_account_values(values: dict) -> dict:
    try:
        account_type = AccountType(values["type"])
    except ValueError as exc:
        raise InvalidAccountError(message="Choose an account type") from exc
    try:
        parent_id = int(values["parent_id"]) if values["parent_id"] else None
    except ValueError as exc:
        raise InvalidAccountError(message="Choose a valid parent account") from exc
    return {
        "name": values["name"],
        "number": values["number"] or None,
        "type": account_type,
        "parent_id": parent_id,
        "detail_type": values["detail_type"] or None,
    }


@router.get("/accounts/new", response_class=HTMLResponse, dependencies=[Depends(require_owner)])
async def account_new(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    values = {"name": "", "number": "", "type": "", "parent_id": "", "detail_type": ""}
    return _account_form(request, session, values=values)


@router.post("/accounts/new", response_class=HTMLResponse)
async def account_create(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    values = _values_from_form(await request.form())
    try:
        AccountService(session).create(actor=actor, **_parse_account_values(values))
    except AppBaseError as exc:
        return _account_form(request, session, values=values, error=exc.message, status_code=422)
    return RedirectResponse("/accounts", status_code=303)


@router.get(
    "/accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_owner)],
)
async def account_edit(
    request: Request, account_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    try:
        account = AccountService(session).get(account_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = {
        "name": account.name,
        "number": account.number or "",
        "type": AccountType(account.type).value,
        "parent_id": str(account.parent_id or ""),
        "detail_type": account.detail_type or "",
    }
    return _account_form(request, session, values=values, account=account)


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def account_update(
    request: Request,
    account_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    service = AccountService(session)
    try:
        account = service.get(account_id)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    values = _values_from_form(await request.form())
    try:
        fields = _parse_account_values(values)
        service.update(account_id, actor=actor, **fields)
    except AppBaseError as exc:
        return _account_form(
            request, session, values=values, account=account, error=exc.message, status_code=422
        )
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{account_id}/active", response_class=HTMLResponse)
async def account_set_active(
    request: Request,
    account_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    active = _text(form.get("active")) == "1"
    try:
        AccountService(session).set_active(account_id, active, actor=actor)
    except AppBaseError as exc:
        return _account_list(
            request, session, inactive=not active, error=exc.message, status_code=exc.http_code
        )
    return RedirectResponse("/accounts", status_code=303)


@router.get("/accounts/{account_id}/register", response_class=HTMLResponse)
async def account_register(
    request: Request,
    account_id: int,
    start: str = "",
    end: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    today = company_today()
    try:
        start_date = _parse_date(start, "Start") or fiscal_year_start(today)
        end_date = _parse_date(end, "End") or today
        register = ReportService(session).account_register(account_id, start_date, end_date)
    except AppBaseError as exc:
        return _error_partial(request, exc)
    return templates.TemplateResponse(
        request,
        "accounts/register.html",
        {"register": register, "start": start_date, "end": end_date},
    )


# ---------------------------------------------------------------------------
# Ledger settings
# ---------------------------------------------------------------------------


def _settings_page(
    request: Request,
    session: Session,
    *,
    error: str | None = None,
    role_error: str | None = None,
    saved: str | None = None,
    lock_value: str | None = None,
    selected: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    service = AccountService(session)
    active = service.tree(include_inactive=False)
    roles = []
    for role, account_type in ROLE_TYPES.items():
        bound = service.account_by_role(role)
        roles.append(
            {
                "value": role.value,
                "label": ROLE_LABELS[role],
                "type": account_type.value,
                "selected": (selected or {}).get(role.value, str(bound.id) if bound else ""),
                "options": [
                    {"id": n.account.id, "label": _label(n.account, n.full_name)}
                    for n in active
                    if n.account.type == account_type
                ],
            }
        )
    lock_date = PeriodLockService(session).get()
    return templates.TemplateResponse(
        request,
        "settings/ledger.html",
        {
            "roles": roles,
            "lock_date": lock_date,
            "lock_value": lock_value if lock_value is not None else (lock_date or ""),
            "error": error,
            "role_error": role_error,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/settings/ledger", response_class=HTMLResponse)
async def ledger_settings(request: Request, session: Session = Depends(get_session)):
    return _settings_page(request, session)


@router.post("/settings/ledger/roles", response_class=HTMLResponse)
async def ledger_roles_save(
    request: Request,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    form = await request.form()
    selected = {role.value: _text(form.get(f"role_{role.value}")) for role in ROLE_TYPES}
    service = AccountService(session)
    saved_roles: list[str] = []
    failed = None
    try:
        for failed in ROLE_TYPES:
            raw = selected[failed.value]
            try:
                account_id = int(raw) if raw else None
            except ValueError as exc:
                raise InvalidAccountError(message="Choose a valid account") from exc
            current = service.account_by_role(failed)
            if (current.id if current else None) != account_id:
                # bind_role commits each binding, so earlier roles stay saved on a failure.
                service.bind_role(failed, account_id, actor=actor)
                saved_roles.append(ROLE_LABELS[failed])
    except AppBaseError as exc:
        session.rollback()
        notice = f"Saved before the error: {', '.join(saved_roles)}." if saved_roles else None
        return _settings_page(
            request,
            session,
            role_error=f"{ROLE_LABELS[failed]}: {exc.message}",
            saved=notice,
            status_code=422,
        )
    return _settings_page(request, session, saved="System roles saved.")


@router.post("/settings/ledger/lock", response_class=HTMLResponse)
def ledger_lock_save(
    request: Request,
    lock_date: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
):
    # A plain ``def``: the Argon2 check runs in the threadpool, not on the event loop.
    raw_date = _text(lock_date)
    try:
        parsed = _parse_date(raw_date, "Lock date")
        PeriodLockService(session).set(parsed, password=password, actor=actor)
    except LockPasswordError as exc:
        return _settings_page(
            request, session, error=exc.message, lock_value=raw_date, status_code=exc.http_code
        )
    except AppBaseError as exc:
        return _settings_page(
            request, session, error=exc.message, lock_value=raw_date, status_code=422
        )
    return _settings_page(request, session, saved="Lock date saved.")
