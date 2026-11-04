"""Helpers shared by the receivables services."""

import functools
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from payroll.exceptions import AppBaseError


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
