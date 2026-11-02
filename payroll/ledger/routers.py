import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.common.middleware import LogRoute
from payroll.ledger.accounts import AccountNode, AccountService
from payroll.ledger.models import Account, SystemRole
from payroll.ledger.schemas import (
    ZERO,
    AccountCreate,
    AccountNodeRead,
    AccountRead,
    AccountUpdate,
    SystemRoleBind,
)

logger = logging.getLogger(f"{app_settings.logger_name}.ledger.router")

# Every route needs a signed-in user with any role (read access); each mutating
# route also depends on ``require_owner``, and the services re-check the role.
router = APIRouter(
    prefix="/ledger",
    tags=["ledger"],
    route_class=LogRoute,
    dependencies=[Depends(require_reader)],
)


def _read(svc: AccountService, node: AccountNode, balances: dict[int, Decimal]) -> dict:
    return _fields(svc, node.account, node.full_name, balances) | {"depth": node.depth}


def _fields(
    svc: AccountService,
    account: Account,
    full_name: str,
    balances: dict[int, Decimal] | None = None,
) -> dict:
    debit_minus_credit = (balances if balances is not None else svc.balances()).get(
        account.id, ZERO
    )
    normal = svc.normal_balance(account)
    return {
        **account.model_dump(),
        "full_name": full_name,
        "normal_balance": normal,
        "balance": debit_minus_credit if normal == "debit" else -debit_minus_credit,
    }


def _full_name(svc: AccountService, account: Account) -> str:
    names = [account.name]
    node = account
    while node.parent_id is not None:
        node = svc.get(node.parent_id)
        names.append(node.name)
    return ":".join(reversed(names))


def _one(svc: AccountService, account: Account) -> AccountRead:
    return AccountRead.model_validate(_fields(svc, account, _full_name(svc, account)))


@router.get("/accounts", response_model=list[AccountRead], summary="List accounts")
async def list_accounts(
    include_inactive: bool = Query(default=False, alias="includeInactive"),
    session: Session = Depends(get_session),
) -> list[AccountRead]:
    svc = AccountService(session)
    balances = svc.balances()
    return [
        AccountRead.model_validate(_read(svc, n, balances))
        for n in svc.tree(include_inactive=include_inactive)
    ]


@router.get(
    "/accounts/tree", response_model=list[AccountNodeRead], summary="Chart of accounts tree"
)
async def account_tree(
    include_inactive: bool = Query(default=False, alias="includeInactive"),
    session: Session = Depends(get_session),
) -> list[AccountNodeRead]:
    svc = AccountService(session)
    balances = svc.balances()
    return [
        AccountNodeRead.model_validate(_read(svc, n, balances))
        for n in svc.tree(include_inactive=include_inactive)
    ]


@router.get("/accounts/{account_id}", response_model=AccountRead, summary="Get an account")
async def get_account(account_id: int, session: Session = Depends(get_session)) -> AccountRead:
    svc = AccountService(session)
    return _one(svc, svc.get(account_id))


@router.post(
    "/accounts",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def create_account(
    payload: AccountCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> AccountRead:
    svc = AccountService(session)
    return _one(svc, svc.create(actor=actor, **payload.model_dump()))


@router.patch("/accounts/{account_id}", response_model=AccountRead, summary="Edit an account")
async def update_account(
    account_id: int,
    payload: AccountUpdate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> AccountRead:
    svc = AccountService(session)
    changes = payload.model_dump(exclude_unset=True)
    return _one(svc, svc.update(account_id, actor=actor, **changes))


@router.post(
    "/accounts/{account_id}/deactivate", response_model=AccountRead, summary="Deactivate an account"
)
async def deactivate_account(
    account_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> AccountRead:
    svc = AccountService(session)
    return _one(svc, svc.set_active(account_id, False, actor=actor))


@router.post(
    "/accounts/{account_id}/activate", response_model=AccountRead, summary="Reactivate an account"
)
async def activate_account(
    account_id: int,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> AccountRead:
    svc = AccountService(session)
    return _one(svc, svc.set_active(account_id, True, actor=actor))


@router.get(
    "/system-roles",
    response_model=dict[SystemRole, AccountRead | None],
    summary="Accounts bound to each system role",
)
async def get_system_roles(
    session: Session = Depends(get_session),
) -> dict[SystemRole, AccountRead | None]:
    svc = AccountService(session)
    result: dict[SystemRole, AccountRead | None] = {}
    for role in SystemRole:
        account = svc.account_by_role(role)
        result[role] = _one(svc, account) if account else None
    return result


@router.put(
    "/system-roles/{role}",
    status_code=status.HTTP_200_OK,
    response_model=dict[SystemRole, AccountRead | None],
    summary="Bind (or unbind) a system role",
)
async def bind_system_role(
    role: SystemRole,
    payload: SystemRoleBind,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> dict[SystemRole, AccountRead | None]:
    AccountService(session).bind_role(role, payload.account_id, actor=actor)
    return await get_system_roles(session)
