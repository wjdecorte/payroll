"""Expenses: validation, reads, voids, audit."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.expenses.exceptions import ExpenseError, ExpenseNotFoundError, InvalidExpenseError
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, ExpenseLine, Reimbursement
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import JournalEntry
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, vendor, expense_account, card, owner_role):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def line(account, amount="10.00", memo=None):
    return ExpenseLineInput(account.id, D(amount), memo)


def make(session, owner_user, lines, **kw):
    kw.setdefault("expense_date", DAY)
    kw.setdefault("source", "account")
    kw.setdefault("payee_text", "Corner Shop")
    if kw["source"] == "account" and "paid_from_account_id" not in kw:
        raise AssertionError("pass paid_from_account_id")
    kw.setdefault("paid_from_account_id", None)
    return ExpenseService(session).create(actor=owner_user, lines=lines, **kw)


def entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def rejected(session, owner_user, lines, fragment=None, **kw):
    before = entry_count(session)
    with pytest.raises(InvalidExpenseError) as exc:
        make(session, owner_user, lines, **kw)
    if fragment:
        assert fragment in exc.value.message
    assert entry_count(session) == before
    assert session.exec(select(func.count()).select_from(Expense)).one() == 0
    return exc.value


# --------------------------------------------------------------- validation


def test_create_stores_lines_in_order_and_total(session, owner_user, expense_account, card):
    expense = make(
        session,
        owner_user,
        [line(expense_account, "1.10", "a"), line(expense_account, "2.20"), line(expense_account)],
        paid_from_account_id=card.id,
        memo="  weekly  ",
    )
    assert (expense.total, expense.status, expense.memo) == (D("13.30"), "posted", "weekly")
    assert expense.entry_id is not None
    rows = ExpenseService(session).lines(expense.id)
    assert [(r.line_no, r.amount, r.memo) for r in rows] == [
        (1, D("1.10"), "a"),
        (2, D("2.20"), None),
        (3, D("10.00"), None),
    ]


@pytest.mark.parametrize(
    "amount",
    [
        D("0.00"),
        D("-1.00"),
        D("1.001"),
        D("100000000.01"),
        D("NaN"),
        D("Infinity"),
        D("-Infinity"),
        D("1E+30"),
        D("-1E+30"),
        5,
        5.0,
        True,
    ],
)
def test_bad_line_amounts_refused(session, owner_user, expense_account, card, amount):
    bad = ExpenseLineInput(expense_account.id, amount)
    rejected(session, owner_user, [bad], paid_from_account_id=card.id)


def test_max_amount_allowed_and_total_capped(session, owner_user, expense_account, card):
    top = D("100000000.00")
    ok = make(
        session, owner_user, [line(expense_account, "100000000.00")], paid_from_account_id=card.id
    )
    assert ok.total == top
    session.rollback()
    with pytest.raises(InvalidExpenseError):
        make(
            session,
            owner_user,
            [line(expense_account, "100000000.00"), line(expense_account, "0.01")],
            paid_from_account_id=card.id,
        )


def test_line_count_bounds(session, owner_user, expense_account, card):
    rejected(session, owner_user, [], "at least one", paid_from_account_id=card.id)
    rejected(
        session,
        owner_user,
        [line(expense_account, "1.00")] * 101,
        "at most 100",
        paid_from_account_id=card.id,
    )
    hundred = make(
        session, owner_user, [line(expense_account, "1.00")] * 100, paid_from_account_id=card.id
    )
    assert hundred.total == D("100.00")


def test_line_account_rules(session, owner_user, chart, expense_account, card):
    revenue = chart["Services Revenue"]
    rejected(session, owner_user, [line(revenue)], "not an expense", paid_from_account_id=card.id)
    rejected(
        session, owner_user, [ExpenseLineInput(999_999, D("1.00"))], paid_from_account_id=card.id
    )
    AccountService(session).set_active(expense_account.id, False, actor=owner_user)
    session.commit()
    rejected(session, owner_user, [line(expense_account)], "inactive", paid_from_account_id=card.id)


def test_payment_account_rules(session, owner_user, chart, expense_account, card, owner_role):
    lines = [line(expense_account)]
    for name in ("Accounts Receivable", "Undeposited Funds"):
        rejected(session, owner_user, lines, "cannot pay", paid_from_account_id=chart[name].id)
    rejected(session, owner_user, lines, "cannot pay", paid_from_account_id=owner_role.id)
    rejected(
        session, owner_user, lines, "cannot pay", paid_from_account_id=chart["Services Revenue"].id
    )
    rejected(session, owner_user, lines, paid_from_account_id=None)
    rejected(session, owner_user, lines, paid_from_account_id=999_999)
    AccountService(session).set_active(card.id, False, actor=owner_user)
    session.commit()
    rejected(session, owner_user, lines, "inactive", paid_from_account_id=card.id)


def test_owner_source_takes_no_payment_account(session, owner_user, expense_account, card):
    rejected(
        session, owner_user, [line(expense_account)], source="owner", paid_from_account_id=card.id
    )
    rejected(
        session, owner_user, [line(expense_account)], source="bogus", paid_from_account_id=None
    )


def test_vendor_and_payee_exclusive(session, owner_user, expense_account, card, vendor):
    lines, card_id = [line(expense_account)], card.id
    rejected(
        session,
        owner_user,
        lines,
        vendor_id=vendor.id,
        payee_text="x",
        paid_from_account_id=card_id,
    )
    rejected(session, owner_user, lines, payee_text=None, paid_from_account_id=card_id)
    rejected(session, owner_user, lines, payee_text="   ", paid_from_account_id=card_id)
    rejected(
        session, owner_user, lines, vendor_id=999_999, payee_text=None, paid_from_account_id=card_id
    )
    rejected(session, owner_user, lines, payee_text="x" * 201, paid_from_account_id=card_id)
    rejected(session, owner_user, lines, payee_text="a\x00b", paid_from_account_id=card_id)
    by_vendor = make(
        session,
        owner_user,
        lines,
        vendor_id=vendor.id,
        payee_text=None,
        paid_from_account_id=card_id,
    )
    assert (by_vendor.vendor_id, by_vendor.payee_text) == (vendor.id, None)
    by_text = make(session, owner_user, lines, payee_text="  Cash  ", paid_from_account_id=card_id)
    assert (by_text.vendor_id, by_text.payee_text) == (None, "Cash")


def test_inactive_vendor_refused_for_new_expense_but_old_one_stays(
    session, owner_user, expense_account, card, vendor
):
    from payroll.expenses.vendors import VendorService

    old = make(
        session,
        owner_user,
        [line(expense_account)],
        vendor_id=vendor.id,
        payee_text=None,
        paid_from_account_id=card.id,
    )
    VendorService(session).set_active(vendor.id, actor=owner_user, active=False)
    with pytest.raises(InvalidExpenseError) as inactive:
        make(
            session,
            owner_user,
            [line(expense_account)],
            vendor_id=vendor.id,
            payee_text=None,
            paid_from_account_id=card.id,
        )
    assert "inactive" in inactive.value.message
    ExpenseService(session).void(old.id, actor=owner_user, void_date=LATER)  # still voidable


def test_memo_length_and_dates(session, owner_user, expense_account, card):
    lines, card_id = [line(expense_account)], card.id
    rejected(session, owner_user, lines, memo="m" * 501, paid_from_account_id=card_id)
    rejected(
        session, owner_user, lines, expense_date=date(1899, 12, 31), paid_from_account_id=card_id
    )
    rejected(session, owner_user, lines, expense_date="2026-03-01", paid_from_account_id=card_id)
    ok = make(session, owner_user, lines, memo="m" * 500, paid_from_account_id=card_id)
    assert len(ok.memo) == 500


def test_memo_and_line_memo_rules(session, owner_user, expense_account, card):
    card_id = card.id
    rejected(
        session, owner_user, [line(expense_account, memo="m" * 501)], paid_from_account_id=card_id
    )
    rejected(
        session, owner_user, [line(expense_account, memo="a\x00b")], paid_from_account_id=card_id
    )
    rejected(
        session, owner_user, [line(expense_account)], memo="a\x07b", paid_from_account_id=card_id
    )
    # a bad line memo on a later line is caught before anything is written
    rejected(
        session,
        owner_user,
        [line(expense_account), line(expense_account, memo="m" * 501)],
        paid_from_account_id=card_id,
    )
    ok = make(
        session, owner_user, [line(expense_account, memo="m" * 500)], paid_from_account_id=card_id
    )
    assert len(ExpenseService(session).lines(ok.id)[0].memo) == 500


def test_unbound_due_to_owner_is_a_422(session, owner_user, expense_account, owner_role):
    AccountService(session).bind_role(
        __import__("payroll.ledger.models", fromlist=["SystemRole"]).SystemRole.DUE_TO_OWNER,
        None,
        actor=owner_user,
    )
    session.commit()
    err = rejected(session, owner_user, [line(expense_account)], source="owner")
    assert err.message == "Bind Due to Owner in Settings > Ledger first"
    assert err.http_code == 422


def test_date_in_locked_period_refused(session, owner_user, expense_account, card):
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": date(2026, 3, 31)}
    )
    session.commit()
    before = entry_count(session)
    with pytest.raises(PeriodLockedError):
        make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    assert entry_count(session) == before
    assert session.exec(select(func.count()).select_from(Expense)).one() == 0
    assert session.exec(select(func.count()).select_from(ExpenseLine)).one() == 0


def test_only_the_owner_may_mutate(session, owner_user, expense_account, card):
    others = [
        make_user(session, email=f"{role}@example.com", role=role)
        for role in ("viewer", "accountant")
    ]
    session.commit()
    for other in others:
        with pytest.raises(PermissionDeniedError):
            make(session, other, [line(expense_account)], paid_from_account_id=card.id)
    expense = make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    for other in others:
        with pytest.raises(PermissionDeniedError):
            ExpenseService(session).void(expense.id, actor=other, void_date=LATER)
    assert session.exec(select(func.count()).select_from(Expense)).one() == 1
    session.refresh(expense)
    assert expense.status == "posted"


# --------------------------------------------------------------------- void


def test_void_reverses_with_readable_memo(session, owner_user, expense_account, card):
    expense = make(
        session, owner_user, [line(expense_account, "7.00")], paid_from_account_id=card.id
    )
    voided = ExpenseService(session).void(expense.id, actor=owner_user, void_date=LATER)
    assert (voided.status, voided.void_date) == ("void", LATER)
    reversal = session.get(JournalEntry, voided.void_entry_id)
    assert reversal.entry_date == LATER
    assert reversal.memo == "Void of expense 2026-03-01"
    assert reversal.source_type == "expense" and reversal.source_id == str(expense.id)
    balances = AccountService(session).balances()
    assert balances.get(expense_account.id, D("0.00")) == D("0.00")
    assert balances.get(card.id, D("0.00")) == D("0.00")


def test_void_uses_caller_memo(session, owner_user, expense_account, card):
    expense = make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    voided = ExpenseService(session).void(expense.id, actor=owner_user, void_date=DAY, memo="dup")
    assert session.get(JournalEntry, voided.void_entry_id).memo == "dup"


@pytest.mark.parametrize("memo", ["bad\x00memo", "tab\tmemo", "line\nbreak", "x" * 501])
def test_void_refuses_a_dirty_memo(session, owner_user, expense_account, card, memo):
    expense = make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    with pytest.raises(InvalidExpenseError):
        ExpenseService(session).void(expense.id, actor=owner_user, void_date=DAY, memo=memo)
    session.refresh(expense)
    assert expense.status == "posted"


def test_void_refusals(session, owner_user, expense_account, card, chart):
    service = ExpenseService(session)
    expense = make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    with pytest.raises(InvalidExpenseError) as early:
        service.void(expense.id, actor=owner_user, void_date=date(2026, 2, 28))
    assert "before the expense date" in early.value.message
    with pytest.raises(ExpenseNotFoundError):
        service.void(999_999, actor=owner_user, void_date=LATER)
    service.void(expense.id, actor=owner_user, void_date=LATER)
    before = entry_count(session)
    with pytest.raises(ExpenseError) as again:
        service.void(expense.id, actor=owner_user, void_date=LATER)
    assert "already void" in again.value.message
    assert entry_count(session) == before


def test_void_refused_while_reimbursed(session, owner_user, expense_account, chart):
    expense = make(session, owner_user, [line(expense_account)], source="owner")
    reimbursement = Reimbursement(
        date=LATER, paid_from_account_id=chart["Business Checking"].id, total=expense.total
    )
    session.add(reimbursement)
    session.flush()
    expense.reimbursement_id = reimbursement.id
    session.add(expense)
    session.commit()
    before = entry_count(session)
    with pytest.raises(ExpenseError) as exc:
        ExpenseService(session).void(expense.id, actor=owner_user, void_date=LATER)
    assert "void the reimbursement first" in exc.value.message
    assert entry_count(session) == before
    session.refresh(expense)
    assert expense.status == "posted"


def test_void_in_locked_period_refused(session, owner_user, expense_account, card):
    expense = make(session, owner_user, [line(expense_account)], paid_from_account_id=card.id)
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": date(2026, 3, 31)}
    )
    session.commit()
    with pytest.raises(PeriodLockedError):
        ExpenseService(session).void(expense.id, actor=owner_user, void_date=date(2026, 4, 2))
    session.refresh(expense)
    assert expense.status == "posted" and expense.void_entry_id is None


def test_audit_rows(session, owner_user, expense_account, card):
    expense = make(
        session, owner_user, [line(expense_account, "3.00")], paid_from_account_id=card.id
    )
    ExpenseService(session).void(expense.id, actor=owner_user, void_date=LATER)
    rows = {
        r.action: r
        for r in session.exec(select(AuditLog).where(AuditLog.entity == "expense")).all()
    }
    created, voided = rows["expense.create"], rows["expense.void"]
    assert created.entity_id == str(expense.id) == voided.entity_id
    assert created.user_id == owner_user.id
    assert created.after["total"] == "3.00" and created.after["entry_id"] == expense.entry_id
    assert voided.before["status"] == "posted" and voided.after["status"] == "void"


def test_failed_create_writes_no_audit(session, owner_user, expense_account):
    with pytest.raises(InvalidExpenseError):
        make(session, owner_user, [line(expense_account)], paid_from_account_id=None)
    assert session.exec(select(AuditLog).where(AuditLog.action == "expense.create")).all() == []


# -------------------------------------------------------------------- reads


def test_get_not_found(session):
    with pytest.raises(ExpenseNotFoundError) as exc:
        ExpenseService(session).get(999_999)
    assert exc.value.http_code == 404


def test_list_filters_paging_and_count(session, owner_user, expense_account, card, vendor, chart):
    other_expense = AccountService(session).create(
        actor=owner_user, name="Travel Test", type=expense_account.type
    )
    a = make(
        session,
        owner_user,
        [line(expense_account)],
        expense_date=date(2026, 3, 1),
        paid_from_account_id=card.id,
        vendor_id=vendor.id,
        payee_text=None,
    )
    b = make(
        session,
        owner_user,
        [line(other_expense)],
        expense_date=date(2026, 3, 10),
        paid_from_account_id=chart["Business Checking"].id,
    )
    c = make(
        session, owner_user, [line(expense_account)], expense_date=date(2026, 3, 10), source="owner"
    )
    d = make(
        session, owner_user, [line(expense_account)], expense_date=date(2026, 3, 20), source="owner"
    )
    ExpenseService(session).void(d.id, actor=owner_user, void_date=date(2026, 3, 21))
    svc = ExpenseService(session)

    def ids(**kw):
        return [e.id for e in svc.list(**kw)]

    assert ids() == [d.id, c.id, b.id, a.id]  # newest date first, then newest id
    assert ids(date_from=date(2026, 3, 10)) == [d.id, c.id, b.id]
    assert ids(date_to=date(2026, 3, 10)) == [c.id, b.id, a.id]
    assert ids(vendor_id=vendor.id) == [a.id]
    assert ids(account_id=other_expense.id) == [b.id]
    assert ids(account_id=card.id) == [a.id]  # paid-from also matches
    assert ids(status="void") == [d.id]
    assert ids(status="posted") == [c.id, b.id, a.id]
    assert ids(open_owner_paid=True) == [c.id]
    assert ids(limit=2) == [d.id, c.id]
    assert ids(limit=2, offset=2) == [b.id, a.id]
    assert svc.count() == 4
    assert svc.count(status="posted") == 3
    assert svc.count(open_owner_paid=True) == 1
    assert svc.count(vendor_id=vendor.id, date_to=date(2026, 3, 1)) == 1


@pytest.mark.parametrize(
    "kw", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": True}, {"status": "bogus"}]
)
def test_list_validates_paging_and_status(session, kw):
    with pytest.raises(InvalidExpenseError):
        ExpenseService(session).list(**kw)
