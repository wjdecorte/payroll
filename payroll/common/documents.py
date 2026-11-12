"""Helpers shared by the document services (receivables, expenses)."""

import functools
import unicodedata
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, col, select

from payroll.exceptions import AppBaseError
from payroll.ledger.models import Account, AccountType, SystemRole


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # AppBaseModel stores naive UTC


MAX_MEMO = 500
MAX_DOCUMENT_AMOUNT = Decimal("100000000.00")
_CENT = Decimal("0.01")

# Account-shape rules for the account a document pays from or into: a bank, cash or card
# account that does not hold a receivables / owner-debt system role.
PAYMENT_TYPES = frozenset({AccountType.ASSET, AccountType.LIABILITY})
RESERVED_ROLES = frozenset(
    {SystemRole.ACCOUNTS_RECEIVABLE, SystemRole.UNDEPOSITED_FUNDS, SystemRole.DUE_TO_OWNER}
)


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def clean_text(value: object, label: str, limit: int, error_cls: type[AppBaseError]) -> str | None:
    """Stripped text, or None when blank; refuses non-text, control characters and long text."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise error_cls(message=f"{label} must be text")
    text = value.strip()
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise error_cls(message=f"{label} cannot contain control characters")
    if len(text) > limit:
        raise error_cls(message=f"{label} must be at most {limit} characters")
    return text or None


def checked_amount(value: object, label: str, error_cls: type[AppBaseError]) -> Decimal:
    """A positive, finite Decimal up to 100,000,000.00 with at most two decimal places.

    Checked in that order: finite, then range (``quantize`` raises on huge values), then
    decimal places. Returns the value quantized to cents.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise error_cls(message=f"{label} must be a number")
    if value <= 0 or value > MAX_DOCUMENT_AMOUNT:
        raise error_cls(message=f"{label} must be between 0.01 and 100,000,000.00")
    if value != value.quantize(_CENT):
        raise error_cls(message=f"{label} must have at most two decimal places")
    return value.quantize(_CENT)


def void_memo(memo: str | None, default: str) -> str:
    """The memo of a void's reversal entry: the caller's own, else a readable default.

    The ledger's own default quotes the journal entry number, which is easily mistaken
    for the document number; reversal rows are immutable, so the default names the
    document instead.
    """
    return memo or default[:MAX_MEMO]


def check_void_date(
    void_date: object, document_date: date, what: str, error_cls: type[AppBaseError]
) -> None:
    """Refuse a void dated before the document: the reversal would precede the original."""
    if (
        not isinstance(void_date, date)
        or isinstance(void_date, datetime)
        or void_date < document_date
    ):
        raise error_cls(
            message=f"The void date cannot be before the {what} date ({document_date.isoformat()})"
        )


def conflict_safe[**P, R](
    conflict_error: type[AppBaseError],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Turn a unique-index violation from a racing writer into ``conflict_error``.

    The service checks uniqueness first for friendly messages, but two concurrent
    requests can both pass those checks; the database index then rejects the loser.
    The rollback also discards the audit row written in the same transaction.
    """

    def decorate(method: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(method)
        def wrapper(self, *args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore[no-untyped-def]
            try:
                return method(self, *args, **kwargs)
            except IntegrityError as exc:
                self.session.rollback()
                raise conflict_error() from exc

        return wrapper

    return decorate


def commit_or_rollback[R](session: Session, work: Callable[[], R]) -> R:
    """Run ``work`` and commit; on ANY exception roll back at once (frees row locks)."""
    try:
        result = work()
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return result


def locked_role_account(
    session: Session, role: SystemRole, error: Callable[[], AppBaseError]
) -> Account:
    """The account bound to ``role``, held FOR SHARE so a rebind waits for this transaction.

    The role is confirmed after the lock: a rebind that committed while we waited is
    seen, and ``error()`` is raised if the account no longer holds the role.
    """
    account = session.exec(
        select(Account)
        .where(Account.system_role == role)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).first()
    if account is None or account.system_role != role:
        raise error()
    return account


def lock_rows[M: SQLModel](
    session: Session, model: type[M], ids: Sequence[int], error: Callable[[], AppBaseError]
) -> list[M]:
    """The rows of ``model`` with ``ids``, locked FOR UPDATE in id order.

    Locking in a fixed order keeps concurrent writers from deadlocking. ``error()`` is
    raised if any id is missing.
    """
    rows = list(
        session.exec(
            select(model)
            .where(col(model.id).in_(ids))  # type: ignore[attr-defined]
            .order_by(col(model.id))  # type: ignore[attr-defined]
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(rows) != len(set(ids)):
        raise error()
    return rows


def lock_accounts(session: Session, ids: set[int]) -> dict[int, Account]:
    """The accounts with ``ids`` (those that exist), locked FOR SHARE in id order."""
    rows = session.exec(
        select(Account)
        .where(col(Account.id).in_(sorted(ids)))
        .order_by(col(Account.id))
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).all()
    return {row.id: row for row in rows}  # type: ignore[misc]
