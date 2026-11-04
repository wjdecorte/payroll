"""Helpers shared by the receivables services."""

import functools
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from payroll.exceptions import AppBaseError
from payroll.ledger.models import Account, SystemRole


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # AppBaseModel stores naive UTC


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
