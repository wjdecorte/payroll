"""Helpers shared by the document services (receivables, expenses)."""

import functools
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, col, select

from payroll.exceptions import AppBaseError
from payroll.ledger.models import Account, SystemRole


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # AppBaseModel stores naive UTC


MAX_MEMO = 500


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
    if not isinstance(void_date, date) or void_date < document_date:
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
