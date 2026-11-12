"""Chart of accounts: create, edit, deactivate and bind system roles."""

import functools
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.exceptions import AppBaseError
from payroll.ledger.exceptions import (
    AccountConflictError,
    AccountInUseError,
    AccountNotFoundError,
    InvalidAccountError,
)
from payroll.ledger.models import (
    NORMAL_DEBIT,
    Account,
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
    SystemRole,
)

logger = logging.getLogger(f"{app_settings.logger_name}.ledger.accounts")

# The account type each system role must be bound to.
ROLE_TYPES: dict[SystemRole, AccountType] = {
    SystemRole.ACCOUNTS_RECEIVABLE: AccountType.ASSET,
    SystemRole.UNDEPOSITED_FUNDS: AccountType.ASSET,
    SystemRole.DUE_TO_OWNER: AccountType.LIABILITY,
    SystemRole.RETAINED_EARNINGS: AccountType.EQUITY,
    SystemRole.OPENING_BALANCE_EQUITY: AccountType.EQUITY,
    SystemRole.PAYROLL_WAGES: AccountType.EXPENSE,
    SystemRole.PAYROLL_TAX_EXPENSE: AccountType.EXPENSE,
    SystemRole.PAYROLL_FEDERAL_PAYABLE: AccountType.LIABILITY,
    SystemRole.PAYROLL_GA_WH_PAYABLE: AccountType.LIABILITY,
    SystemRole.PAYROLL_FUTA_PAYABLE: AccountType.LIABILITY,
    SystemRole.PAYROLL_SUTA_PAYABLE: AccountType.LIABILITY,
    SystemRole.PAYROLL_HEALTH_PAYABLE: AccountType.LIABILITY,
    SystemRole.PAYROLL_HSA_PAYABLE: AccountType.LIABILITY,
}

# The accounts payroll runs and tax payments post to.
PAYROLL_ROLES: tuple[SystemRole, ...] = (
    SystemRole.PAYROLL_WAGES,
    SystemRole.PAYROLL_TAX_EXPENSE,
    SystemRole.PAYROLL_FEDERAL_PAYABLE,
    SystemRole.PAYROLL_GA_WH_PAYABLE,
    SystemRole.PAYROLL_FUTA_PAYABLE,
    SystemRole.PAYROLL_SUTA_PAYABLE,
    SystemRole.PAYROLL_HEALTH_PAYABLE,
    SystemRole.PAYROLL_HSA_PAYABLE,
)


# Roles whose rebind waits for in-flight postings and is refused once documents exist.
FROZEN_ROLES = (SystemRole.ACCOUNTS_RECEIVABLE, SystemRole.DUE_TO_OWNER, *PAYROLL_ROLES)


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # AppBaseModel stores naive UTC


_UNSET: Any = object()  # distinguishes "leave parent alone" from "make top-level"


def _conflict_safe[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Turn a unique-index violation from a racing writer into AccountConflictError.

    The service checks uniqueness first for friendly messages, but two concurrent
    requests can both pass those checks; the database indexes then reject the loser.
    The rollback also discards the audit row written in the same transaction.
    """

    @functools.wraps(method)
    def wrapper(self, *args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore[no-untyped-def]
        try:
            return method(self, *args, **kwargs)
        except IntegrityError as exc:
            self.session.rollback()
            raise AccountConflictError(
                message="Another change took that account number, name or system role"
            ) from exc

    return wrapper


@dataclass(frozen=True)
class NewAccount:
    """One account for :meth:`AccountService.create_batch`.

    The parent is either an existing account (``parent_id``) or an earlier entry of the
    same batch (``parent_index``, a position in the batch list).
    """

    name: str
    type: AccountType
    number: str | None = None
    detail_type: str | None = None
    parent_id: int | None = None
    parent_index: int | None = None


@dataclass
class AccountNode:
    account: Account
    depth: int
    full_name: str


class AccountService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, account_id: int) -> Account:
        account = self.session.get(Account, account_id)
        if account is None:
            raise AccountNotFoundError()
        return account

    def list(self, include_inactive: bool = False) -> list[Account]:
        stmt = select(Account).order_by(col(Account.number).asc().nulls_last(), Account.name)
        if not include_inactive:
            stmt = stmt.where(col(Account.is_active).is_(True))
        return list(self.session.exec(stmt).all())

    def tree(self, include_inactive: bool = False) -> list[AccountNode]:
        """Depth-first list: each parent followed by its children, siblings by number/name."""
        accounts = self.list(include_inactive=True)
        children: dict[int | None, list[Account]] = {}
        for acct in accounts:
            children.setdefault(acct.parent_id, []).append(acct)
        nodes: list[AccountNode] = []

        def walk(parent_id: int | None, depth: int, prefix: str) -> None:
            for acct in children.get(parent_id, []):
                full = f"{prefix}{acct.name}"
                if acct.is_active or include_inactive:
                    nodes.append(AccountNode(account=acct, depth=depth, full_name=full))
                walk(acct.id, depth + 1, f"{full}:")

        walk(None, 0, "")
        return nodes

    def account_by_role(self, role: SystemRole) -> Account | None:
        return self.session.exec(select(Account).where(Account.system_role == role)).first()

    @staticmethod
    def normal_balance(account: Account) -> Literal["debit", "credit"]:
        return "debit" if account.type in NORMAL_DEBIT else "credit"

    def balances(self) -> dict[int, Decimal]:
        """Debits minus credits per account id, over every journal line."""
        stmt = select(
            JournalLine.account_id, func.sum(JournalLine.debit - JournalLine.credit)
        ).group_by(JournalLine.account_id)
        return {acct_id: total for acct_id, total in self.session.exec(stmt).all()}

    # ------------------------------------------------------------------
    # Mutations (owner only; one commit per call)
    # ------------------------------------------------------------------

    @_conflict_safe
    def create(
        self,
        *,
        actor: User,
        name: str,
        type: AccountType,
        number: str | None = None,
        parent_id: int | None = None,
        detail_type: str | None = None,
    ) -> Account:
        ensure_role(actor, OWNER)
        account = self._insert(
            actor,
            NewAccount(
                name=name, type=type, number=number, parent_id=parent_id, detail_type=detail_type
            ),
        )
        self.session.commit()
        logger.info("Created account id=%s", account.id)
        return account

    @_conflict_safe
    def create_batch(self, *, actor: User, specs: Sequence[NewAccount]) -> list[Account]:
        """Create every account in ``specs`` in one transaction: all of them or none."""
        ensure_role(actor, OWNER)
        created: list[Account] = []
        try:
            for spec in specs:
                if spec.parent_index is not None:
                    if not 0 <= spec.parent_index < len(created):
                        raise InvalidAccountError(message="A parent must come before its children")
                    spec = replace(spec, parent_id=created[spec.parent_index].id)
                created.append(self._insert(actor, spec))
            record_audit(
                self.session,
                action="account.import",
                user_id=actor.id,
                entity="account",
                after={"created": len(created), "account_ids": [a.id for a in created]},
            )
            self.session.commit()
        except AppBaseError:
            self.session.rollback()
            raise
        logger.info("Created %s accounts in one batch", len(created))
        return created

    def _insert(self, actor: User, spec: NewAccount) -> Account:
        """Validate and add one account plus its audit row; the caller commits."""
        name = spec.name.strip()
        number = (spec.number or "").strip() or None
        if spec.parent_id is not None:
            self._check_parent(None, spec.parent_id, spec.type)
        self._check_unique(None, name, number, spec.parent_id)
        account = Account(
            name=name,
            number=number,
            parent_id=spec.parent_id,
            type=spec.type,
            detail_type=spec.detail_type,
        )
        self.session.add(account)
        self.session.flush()
        record_audit(
            self.session,
            action="account.create",
            user_id=actor.id,
            entity="account",
            entity_id=str(account.id),
            after=self._snapshot(account),
        )
        return account

    @_conflict_safe
    def update(
        self,
        account_id: int,
        *,
        actor: User,
        name: str | None = None,
        number: str | None = None,
        parent_id: int | None = _UNSET,
        detail_type: str | None = None,
        type: AccountType | None = None,
    ) -> Account:
        """Edit fields; ``None`` leaves a field alone, except ``parent_id=None`` which
        makes the account top-level (omit it, i.e. pass nothing, to keep the parent)."""
        ensure_role(actor, OWNER)
        account = self.get(account_id)
        before = self._snapshot(account)

        new_type = type if type is not None else account.type
        new_parent = account.parent_id if parent_id is _UNSET else parent_id
        new_name = name.strip() if name is not None else account.name
        new_number = ((number or "").strip() or None) if number is not None else account.number

        if new_type != account.type:
            self._check_type_change(account, new_type)
        if new_parent is not None:
            self._check_parent(account, new_parent, new_type)
        self._check_unique(account, new_name, new_number, new_parent)

        account.type = new_type
        account.parent_id = new_parent
        account.name = new_name
        account.number = new_number
        if detail_type is not None:
            account.detail_type = detail_type
        account.modify_date = utcnow_naive()
        self.session.add(account)
        self.session.flush()
        record_audit(
            self.session,
            action="account.update",
            user_id=actor.id,
            entity="account",
            entity_id=str(account.id),
            before=before,
            after=self._snapshot(account),
        )
        self.session.commit()
        return account

    def set_active(self, account_id: int, active: bool, *, actor: User) -> Account:
        ensure_role(actor, OWNER)
        account = self.get(account_id)
        before = self._snapshot(account)
        if not active and account.is_active:
            if account.system_role is not None:
                raise AccountInUseError(
                    message="Unbind the account's system role before deactivating it"
                )
            if self.balances().get(account.id, Decimal("0")) != 0:
                raise AccountInUseError(
                    message="An account with a non-zero balance cannot be deactivated"
                )
        account.is_active = active
        account.modify_date = utcnow_naive()
        self.session.add(account)
        self.session.flush()
        record_audit(
            self.session,
            action="account.activate" if active else "account.deactivate",
            user_id=actor.id,
            entity="account",
            entity_id=str(account.id),
            before=before,
            after=self._snapshot(account),
        )
        self.session.commit()
        return account

    @_conflict_safe
    def bind_role(self, role: SystemRole, account_id: int | None, *, actor: User) -> None:
        """Bind ``role`` to an account (moving it from any other); ``None`` unbinds."""
        ensure_role(actor, OWNER)
        current = self.account_by_role(role)
        if role in FROZEN_ROLES and current is not None:
            # Wait for in-flight postings that depend on the role (they hold this row FOR SHARE)
            # so the freeze check below sees every document.
            self.session.exec(
                select(Account.id).where(Account.id == current.id).with_for_update()
            ).one()
        target = self.get(account_id) if account_id is not None else None
        if target is not None:
            if target.type != ROLE_TYPES[role]:
                raise InvalidAccountError(
                    message=f"Role {role.value} needs an {ROLE_TYPES[role].value} account"
                )
            if not target.is_active:
                raise InvalidAccountError(message="A system role needs an active account")
            if target.system_role not in (None, role):
                raise AccountConflictError(message="That account already holds another system role")
        if current is not None and (target is None or target.id != current.id):
            if role == SystemRole.ACCOUNTS_RECEIVABLE and self._receivables_activity():
                raise InvalidAccountError(
                    message=(
                        "Accounts receivable cannot change after invoices, payments or refunds "
                        "have been recorded"
                    )
                )
            if role == SystemRole.DUE_TO_OWNER and self._owner_activity():
                raise InvalidAccountError(
                    message=(
                        "Due to Owner cannot change after owner-paid expenses or "
                        "reimbursements have been recorded"
                    )
                )
            if role in PAYROLL_ROLES and self._payroll_activity():
                raise InvalidAccountError(
                    message=(
                        f"Payroll role {role.value} cannot change after payroll runs have been "
                        "approved or tax payments recorded"
                    )
                )
        before = {"account_id": current.id if current else None}
        if current is not None and (target is None or current.id != target.id):
            current.system_role = None
            current.modify_date = utcnow_naive()
            self.session.add(current)
            self.session.flush()  # free the unique slot before rebinding
        if target is not None and target.system_role != role:
            target.system_role = role
            target.modify_date = utcnow_naive()
            self.session.add(target)
            self.session.flush()
        record_audit(
            self.session,
            action="account.bind_role",
            user_id=actor.id,
            entity="system_role",
            entity_id=role.value,
            before=before,
            after={"account_id": account_id},
        )
        self.session.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _receivables_activity(self) -> bool:
        """Any invoice, payment or refund entry exists (voided documents keep theirs)."""
        stmt = select(JournalEntry.id).where(
            col(JournalEntry.source_type).in_(
                [SourceType.INVOICE, SourceType.PAYMENT, SourceType.REFUND]
            )
        )
        return self.session.exec(stmt.limit(1)).first() is not None

    def _owner_activity(self) -> bool:
        """Any owner-paid expense (voided included), reimbursement, or owner-funded payroll run."""
        # Imported here: the expenses and payroll_run packages depend on the ledger, not the reverse.
        from payroll.expenses.models import Expense, Reimbursement
        from payroll.payroll_run.models import PayrollRun

        if (
            self.session.exec(select(Expense.id).where(Expense.source == "owner").limit(1)).first()
            is not None
        ):
            return True
        if self.session.exec(select(Reimbursement.id).limit(1)).first() is not None:
            return True
        # An owner-source run finds its net-pay account through the role, so the role must hold.
        run = select(PayrollRun.id).where(
            PayrollRun.net_pay_source == "owner",
            col(PayrollRun.status).in_(["approved", "void"]),
        )
        return self.session.exec(run.limit(1)).first() is not None

    def _payroll_activity(self) -> bool:
        """Any approved or void payroll run (drafts and imports excluded) or any tax payment."""
        # Imported here: the payroll_run package depends on the ledger, not the reverse.
        from payroll.payroll_run.ledger_models import TaxPayment
        from payroll.payroll_run.models import PayrollRun

        run = select(PayrollRun.id).where(col(PayrollRun.status).in_(["approved", "void"]))
        if self.session.exec(run.limit(1)).first() is not None:
            return True
        return self.session.exec(select(TaxPayment.id).limit(1)).first() is not None

    def _check_parent(self, account: Account | None, parent_id: int, new_type: AccountType) -> None:
        parent = self.get(parent_id)
        if parent.type != new_type:
            raise InvalidAccountError(message="A sub-account must have the same type as its parent")
        if account is not None:
            node: Account | None = parent
            while node is not None:
                if node.id == account.id:
                    raise InvalidAccountError(message="An account cannot be its own ancestor")
                node = self.session.get(Account, node.parent_id) if node.parent_id else None

    def _check_type_change(self, account: Account, new_type: AccountType) -> None:
        has_lines = self.session.exec(
            select(JournalLine.id).where(JournalLine.account_id == account.id).limit(1)
        ).first()
        if has_lines is not None:
            raise InvalidAccountError(message="The type of an account with activity cannot change")
        if account.system_role is not None and ROLE_TYPES[account.system_role] != new_type:
            raise InvalidAccountError(
                message="The account is bound to a system role of another type"
            )
        has_children = self.session.exec(
            select(Account.id).where(Account.parent_id == account.id).limit(1)
        ).first()
        if has_children is not None:
            raise InvalidAccountError(message="Change the type of the sub-accounts first")

    def _check_unique(
        self, account: Account | None, name: str, number: str | None, parent_id: int | None
    ) -> None:
        if not name:
            raise InvalidAccountError(message="An account needs a name")
        own_id = account.id if account is not None else None
        if number is not None:
            other = self.session.exec(select(Account).where(Account.number == number)).first()
            if other is not None and other.id != own_id:
                raise AccountConflictError(message=f"Account number {number} is already in use")
        stmt = select(Account).where(func.lower(Account.name) == name.lower())
        stmt = stmt.where(
            Account.parent_id.is_(None) if parent_id is None else Account.parent_id == parent_id  # type: ignore[union-attr]
        )
        other = self.session.exec(stmt).first()
        if other is not None and other.id != own_id:
            raise AccountConflictError(message=f"An account named '{name}' already exists here")

    @staticmethod
    def _snapshot(account: Account) -> dict[str, Any]:
        return {
            "number": account.number,
            "name": account.name,
            "parent_id": account.parent_id,
            "type": account.type,
            "detail_type": account.detail_type,
            "is_active": account.is_active,
            "system_role": account.system_role,
        }
