"""The Due to Owner role freezes once any owner-paid expense or reimbursement exists."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from payroll.auth.models import User
from payroll.exceptions import AppBaseError
from payroll.expenses import expenses as expenses_module
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, ExpenseLine, Reimbursement
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import InvalidAccountError
from payroll.ledger.models import Account, AccountType, JournalLine, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.migrations import alembic_runner
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 10)
MESSAGE = (
    "Due to Owner cannot change after owner-paid expenses, reimbursements "
    "or payroll runs paid to it have been recorded"
)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, owner_role):
    session.commit()


@pytest.fixture
def spare(session, owner_user, chart):
    return AccountService(session).create(
        actor=owner_user, name="Other Owner Liability", type=AccountType.LIABILITY
    )


def _owner_expense(session, account_id: int) -> Expense:
    expense = Expense(date=DAY, payee_text="Cash buy", source="owner", total=D("10.00"))
    session.add(expense)
    session.flush()
    session.add(
        ExpenseLine(expense_id=expense.id, line_no=1, account_id=account_id, amount=D("10"))
    )
    session.flush()
    return expense


def _assert_frozen(session, owner_user, owner_role, spare):
    accounts = AccountService(session)
    for target in (spare.id, None):
        with pytest.raises(InvalidAccountError) as caught:
            accounts.bind_role(SystemRole.DUE_TO_OWNER, target, actor=owner_user)
        assert caught.value.message == MESSAGE
    assert accounts.account_by_role(SystemRole.DUE_TO_OWNER).id == owner_role.id
    accounts.bind_role(SystemRole.DUE_TO_OWNER, owner_role.id, actor=owner_user)  # no-op rebind
    accounts.bind_role(SystemRole.UNDEPOSITED_FUNDS, None, actor=owner_user)  # other roles ok


def test_without_activity_the_role_moves_unbinds_and_binds_first_time(
    session, owner_user, owner_role, spare
):
    accounts = AccountService(session)
    accounts.bind_role(SystemRole.DUE_TO_OWNER, spare.id, actor=owner_user)
    accounts.bind_role(SystemRole.DUE_TO_OWNER, None, actor=owner_user)
    assert accounts.account_by_role(SystemRole.DUE_TO_OWNER) is None
    accounts.bind_role(SystemRole.DUE_TO_OWNER, owner_role.id, actor=owner_user)
    assert accounts.account_by_role(SystemRole.DUE_TO_OWNER).id == owner_role.id


def test_an_account_sourced_expense_does_not_freeze(
    session, owner_user, owner_role, spare, expense_account, card
):
    expense = Expense(
        date=DAY, payee_text="Shop", source="account", paid_from_account_id=card.id, total=D("5")
    )
    session.add(expense)
    session.flush()
    session.add(
        ExpenseLine(expense_id=expense.id, line_no=1, account_id=expense_account.id, amount=D("5"))
    )
    session.flush()
    AccountService(session).bind_role(SystemRole.DUE_TO_OWNER, spare.id, actor=owner_user)


def test_an_owner_expense_freezes_the_role(session, owner_user, owner_role, spare, expense_account):
    _owner_expense(session, expense_account.id)
    _assert_frozen(session, owner_user, owner_role, spare)


def test_a_voided_owner_expense_still_freezes_the_role(
    session, owner_user, owner_role, spare, expense_account, chart
):
    expense = _owner_expense(session, expense_account.id)
    ledger = LedgerPostingService(session)
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id
    draft = EntryDraft(DAY, [LineDraft(cash, debit=D("1")), LineDraft(sales, credit=D("1"))])
    entry_id = ledger.post(draft, actor=owner_user).id
    void_entry_id = ledger.post(draft, actor=owner_user).id
    expense.entry_id = entry_id
    expense.status = "void"
    expense.void_entry_id = void_entry_id
    expense.void_date = DAY
    session.flush()
    _assert_frozen(session, owner_user, owner_role, spare)


def test_a_reimbursement_freezes_the_role(session, owner_user, owner_role, spare, chart):
    session.add(
        Reimbursement(date=DAY, paid_from_account_id=chart["Business Checking"].id, total=D("10"))
    )
    session.flush()
    _assert_frozen(session, owner_user, owner_role, spare)


# ---------------------------------------------------------------------------
# Real two-session races
# ---------------------------------------------------------------------------


@pytest.fixture
def race_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        due = Account(
            name="Due to Owner", type=AccountType.LIABILITY, system_role=SystemRole.DUE_TO_OWNER
        )
        spare = Account(name="Other Liability", type=AccountType.LIABILITY)
        exp = Account(name="Supplies", type=AccountType.EXPENSE)
        db.add_all([due, spare, exp])
        db.flush()
        ids = {"user": user.id, "due": due.id, "spare": spare.id, "exp": exp.id}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=5)
    yield engine, ids
    engine.dispose()


@pytest.fixture
def probe(monkeypatch):
    """Record which role account the real issuer locked; optionally stall after the lock."""
    state: dict = {"locked": None, "hold": 0.0}
    real_lock = expenses_module.locked_role_account
    real_post = ExpenseService._post_entry

    def recording_lock(session, role, error):
        account = real_lock(session, role, error)
        state["locked"] = account.id
        return account

    def slow_post(self, *args, **kwargs):
        time.sleep(state["hold"])  # the posting work happens while the role row is held
        return real_post(self, *args, **kwargs)

    monkeypatch.setattr(expenses_module, "locked_role_account", recording_lock)
    monkeypatch.setattr(ExpenseService, "_post_entry", slow_post)
    return state


def _issue_owner_expense(db: Session, user_id: int, exp_id: int) -> None:
    ExpenseService(db).create(
        actor=db.get(User, user_id),
        expense_date=DAY,
        lines=[ExpenseLineInput(exp_id, D("10.00"))],
        source="owner",
        paid_from_account_id=None,
        payee_text="Cash buy",
    )


def _race(engine, ids, *, rebind_delay=0.0, issue_delay=0.0):
    barrier = threading.Barrier(2)
    outcome: dict[str, str] = {}

    def issue():
        with Session(engine) as db:
            barrier.wait(timeout=30)
            time.sleep(issue_delay)
            try:
                _issue_owner_expense(db, ids["user"], ids["exp"])
                outcome["issue"] = "ok"
            except AppBaseError as exc:
                db.rollback()
                outcome["issue"] = type(exc).__name__

    def rebind():
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
            barrier.wait(timeout=30)
            time.sleep(rebind_delay)
            try:
                AccountService(db).bind_role(SystemRole.DUE_TO_OWNER, ids["spare"], actor=actor)
                outcome["rebind"] = "ok"
            except AppBaseError as exc:
                outcome["rebind"] = type(exc).__name__

    threads = [threading.Thread(target=issue), threading.Thread(target=rebind)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "a worker is stuck (deadlock?)"
    assert set(outcome) == {"issue", "rebind"}
    return outcome


def _bound_id(engine) -> int:
    with Session(engine) as db:
        return db.exec(
            select(Account.id).where(Account.system_role == SystemRole.DUE_TO_OWNER)
        ).one()


def _credited_accounts(engine) -> list[int]:
    """The accounts credited by owner expense entries (the Due to Owner side)."""
    with Session(engine) as db:
        return list(
            db.exec(
                select(JournalLine.account_id)
                .join(Expense, Expense.entry_id == JournalLine.entry_id)
                .where(Expense.source == "owner", JournalLine.credit > 0)
            ).all()
        )


def test_rebind_started_mid_issue_is_refused_not_stranding_the_expense(race_db, probe):
    """The rebind starts while the issuer holds the role row and has not yet posted."""
    engine, ids = race_db
    probe["hold"] = 1.5
    outcome = _race(engine, ids, rebind_delay=0.5)
    assert outcome == {"issue": "ok", "rebind": "InvalidAccountError"}
    assert probe["locked"] == ids["due"]
    assert _credited_accounts(engine) == [ids["due"]]  # the expense is on the role's account
    assert _bound_id(engine) == ids["due"]  # and the role never left it


def test_rebind_that_wins_first_sends_the_issue_to_the_new_role_account(race_db, probe):
    """A rebind that commits before the issuer locks the role: the issue follows the new binding."""
    engine, ids = race_db
    outcome = _race(engine, ids, issue_delay=1.0)
    assert outcome == {"issue": "ok", "rebind": "ok"}
    assert probe["locked"] == ids["spare"]  # the issuer saw the committed rebind
    assert _credited_accounts(engine) == [ids["spare"]]
    assert _bound_id(engine) == ids["spare"]
    with Session(engine) as db:  # nothing was credited to the account that lost the role
        assert AccountService(db).balances().get(ids["due"], D("0.00")) == D("0.00")
