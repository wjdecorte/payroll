import logging
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.models import User
from payroll.auth.permissions import require_owner, require_reader
from payroll.common.dependencies import get_session
from payroll.common.middleware import LogRoute
from payroll.ledger.accounts import AccountNode, AccountService
from payroll.ledger.models import Account, JournalEntry, SourceType, SystemRole
from payroll.ledger.schemas import (
    ZERO,
    AccountCreate,
    AccountNodeRead,
    AccountRead,
    AccountUpdate,
    JournalEntryAction,
    JournalEntryCreate,
    JournalEntryRead,
    SystemRoleBind,
)
from payroll.ledger.services import EntryDraft, JournalService, LineDraft

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


# ----------------------------------------------------------------------
# Journal entries
# ----------------------------------------------------------------------


def _entries_read(svc: JournalService, entries: list[JournalEntry]) -> list[JournalEntryRead]:
    ids = [e.id for e in entries]
    lines = svc.lines(ids)
    reversed_by = svc.reversed_by(ids)
    result = []
    for entry in entries:
        rows = lines.get(entry.id, [])
        result.append(
            JournalEntryRead.model_validate(
                {
                    **entry.model_dump(),
                    "reversed_by_entry_id": reversed_by.get(entry.id),
                    "total_debit": sum((ln.debit for ln, _ in rows), ZERO),
                    "total_credit": sum((ln.credit for ln, _ in rows), ZERO),
                    "lines": [
                        {
                            **line.model_dump(),
                            "account_number": account.number,
                            "account_name": account.name,
                        }
                        for line, account in rows
                    ],
                }
            )
        )
    return result


def _entry_read(svc: JournalService, entry: JournalEntry) -> JournalEntryRead:
    return _entries_read(svc, [entry])[0]


@router.get(
    "/journal-entries", response_model=list[JournalEntryRead], summary="List journal entries"
)
async def list_journal_entries(
    start: date | None = None,
    end: date | None = None,
    source_type: SourceType | None = Query(default=None, alias="sourceType"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[JournalEntryRead]:
    svc = JournalService(session)
    entries = svc.list(start=start, end=end, source_type=source_type, limit=limit, offset=offset)
    return _entries_read(svc, entries)


@router.get(
    "/journal-entries/{entry_id}", response_model=JournalEntryRead, summary="Get a journal entry"
)
async def get_journal_entry(
    entry_id: int, session: Session = Depends(get_session)
) -> JournalEntryRead:
    svc = JournalService(session)
    return _entry_read(svc, svc.get(entry_id))


@router.post(
    "/journal-entries",
    response_model=JournalEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Post a manual journal entry",
)
async def create_journal_entry(
    payload: JournalEntryCreate,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> JournalEntryRead:
    svc = JournalService(session)
    draft = EntryDraft(
        entry_date=payload.entry_date,
        memo=payload.memo,
        lines=[
            LineDraft(ln.account_id, debit=ln.debit, credit=ln.credit, memo=ln.memo)
            for ln in payload.lines
        ],
    )
    return _entry_read(svc, svc.create_manual(draft, actor=actor))


@router.post(
    "/journal-entries/{entry_id}/reverse",
    response_model=JournalEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Reverse a manual journal entry (returns the reversing entry)",
)
async def reverse_journal_entry(
    entry_id: int,
    payload: JournalEntryAction,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> JournalEntryRead:
    svc = JournalService(session)
    reversal = svc.reverse_manual(
        entry_id, entry_date=payload.entry_date, actor=actor, memo=payload.memo
    )
    return _entry_read(svc, reversal)


@router.post(
    "/journal-entries/{entry_id}/void",
    response_model=JournalEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Void a manual journal entry (returns the reversing entry)",
)
async def void_journal_entry(
    entry_id: int,
    payload: JournalEntryAction,
    session: Session = Depends(get_session),
    actor: User = Depends(require_owner),
) -> JournalEntryRead:
    svc = JournalService(session)
    reversal = svc.void_manual(
        entry_id, entry_date=payload.entry_date, actor=actor, memo=payload.memo
    )
    return _entry_read(svc, reversal)
