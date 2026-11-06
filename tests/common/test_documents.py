"""Shared document helpers: commit/rollback, void guards, row locks."""

from datetime import date, datetime

import pytest

from payroll.common import documents
from payroll.common.documents import (
    MAX_MEMO,
    check_void_date,
    commit_or_rollback,
    lock_rows,
    utcnow_naive,
    void_memo,
)
from payroll.ledger.models import Account, AccountType
from payroll.receivables.exceptions import InvalidDepositError as _DocError


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_commit_or_rollback_commits_and_returns_result():
    session = _Session()
    assert commit_or_rollback(session, lambda: 42) == 42  # type: ignore[arg-type]
    assert (session.commits, session.rollbacks) == (1, 0)


@pytest.mark.parametrize("exc", [ValueError("boom"), KeyboardInterrupt()])
def test_commit_or_rollback_rolls_back_and_reraises_any_exception(exc):
    session = _Session()

    def work():
        raise exc

    with pytest.raises(type(exc)):
        commit_or_rollback(session, work)  # type: ignore[arg-type]
    assert (session.commits, session.rollbacks) == (0, 1)


def test_commit_or_rollback_rolls_back_when_commit_fails():
    session = _Session()

    def bad_commit() -> None:
        raise RuntimeError("commit failed")

    session.commit = bad_commit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        commit_or_rollback(session, lambda: 1)  # type: ignore[arg-type]
    assert session.rollbacks == 1


def test_check_void_date_refuses_earlier_date_and_non_date():
    with pytest.raises(_DocError) as before:
        check_void_date(date(2026, 1, 1), date(2026, 2, 1), "invoice", _DocError)
    assert "before the invoice date (2026-02-01)" in before.value.message
    with pytest.raises(_DocError):
        check_void_date("2026-03-01", date(2026, 2, 1), "invoice", _DocError)
    check_void_date(date(2026, 2, 1), date(2026, 2, 1), "invoice", _DocError)


def test_void_memo_prefers_callers_memo_and_truncates_default():
    assert void_memo("mine", "default") == "mine"
    assert void_memo(None, "x" * (MAX_MEMO + 50)) == "x" * MAX_MEMO
    assert void_memo("", "d") == "d"


def test_utcnow_naive_has_no_tzinfo():
    assert isinstance(utcnow_naive(), datetime)
    assert utcnow_naive().tzinfo is None


def test_lock_rows_returns_rows_in_id_order(session):
    a = Account(name="A", type=AccountType.ASSET)
    b = Account(name="B", type=AccountType.ASSET)
    session.add_all([a, b])
    session.flush()
    rows = lock_rows(session, Account, [b.id, a.id], _DocError)
    assert [r.id for r in rows] == sorted([a.id, b.id])


def test_lock_rows_raises_when_an_id_is_missing(session):
    a = Account(name="C", type=AccountType.ASSET)
    session.add(a)
    session.flush()
    with pytest.raises(_DocError):
        lock_rows(session, Account, [a.id, 10**9], _DocError)


def test_module_exposes_locked_role_account_and_conflict_safe():
    assert callable(documents.locked_role_account)
    assert callable(documents.conflict_safe)
