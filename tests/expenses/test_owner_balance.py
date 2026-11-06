"""The owner balance: Due to Owner against the open owner-paid expenses."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.owner import OwnerSummary, owner_summary
from payroll.expenses.reimbursements import ReimbursementService
from payroll.ledger.services import EntryDraft, JournalService, LineDraft
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
D1 = date(2026, 3, 1)
D2 = date(2026, 3, 10)
D3 = date(2026, 3, 20)
D4 = date(2026, 3, 25)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, expense_account, card, owner_role):
    session.commit()


def owner_expense(session, owner_user, account, amount, day):
    return ExpenseService(session).create(
        actor=owner_user,
        expense_date=day,
        lines=[ExpenseLineInput(account.id, D(amount))],
        source="owner",
        paid_from_account_id=None,
        payee_text="Cash buy",
    )


def reimburse(session, owner_user, chart, expenses, day):
    return ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=day,
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[e.id for e in expenses],
    )


def test_empty_books_match_at_zero(session):
    summary = owner_summary(session)
    assert summary == OwnerSummary(D("0.00"), D("0.00"), [], True)


def test_matches_on_clean_data_through_the_real_services(
    session, owner_user, chart, expense_account
):
    a = owner_expense(session, owner_user, expense_account, "10.00", D1)
    b = owner_expense(session, owner_user, expense_account, "25.50", D2)
    c = owner_expense(session, owner_user, expense_account, "4.00", D3)
    summary = owner_summary(session)
    assert (summary.owed_to_me, summary.open_expense_total, summary.matches) == (
        D("39.50"),
        D("39.50"),
        True,
    )
    assert [e.id for e in summary.open_expenses] == [a.id, b.id, c.id]

    reim = reimburse(session, owner_user, chart, [a, b], D3)
    summary = owner_summary(session)
    assert (summary.owed_to_me, summary.open_expense_total, summary.matches) == (
        D("4.00"),
        D("4.00"),
        True,
    )
    assert [e.id for e in summary.open_expenses] == [c.id]

    ExpenseService(session).void(c.id, actor=owner_user, void_date=D4)
    summary = owner_summary(session)
    assert (summary.owed_to_me, summary.open_expense_total, summary.matches) == (
        D("0.00"),
        D("0.00"),
        True,
    )

    ReimbursementService(session).void(reim.id, actor=owner_user, void_date=D4)
    summary = owner_summary(session)
    assert (summary.owed_to_me, summary.open_expense_total, summary.matches) == (
        D("35.50"),
        D("35.50"),
        True,
    )
    assert [e.id for e in summary.open_expenses] == [a.id, b.id]


def test_a_manual_credit_to_due_to_owner_breaks_the_match_and_reports_both(
    session, owner_user, chart, expense_account, owner_role
):
    owner_expense(session, owner_user, expense_account, "10.00", D1)
    JournalService(session).create_manual(
        EntryDraft(
            D2,
            [
                LineDraft(chart["Business Checking"].id, debit=D("7.00")),
                LineDraft(owner_role.id, credit=D("7.00")),
            ],
            memo="Owner put in cash",
        ),
        actor=owner_user,
    )
    summary = owner_summary(session)
    assert summary.owed_to_me == D("17.00")
    assert summary.open_expense_total == D("10.00")
    assert summary.matches is False


def test_as_of_excludes_later_expenses_and_later_reimbursements(
    session, owner_user, chart, expense_account
):
    a = owner_expense(session, owner_user, expense_account, "10.00", D1)
    b = owner_expense(session, owner_user, expense_account, "20.00", D3)
    reimburse(session, owner_user, chart, [a], D3)

    early = owner_summary(session, D2)  # b does not exist yet; a not reimbursed yet
    assert (early.owed_to_me, early.open_expense_total, early.matches) == (
        D("10.00"),
        D("10.00"),
        True,
    )
    assert [e.id for e in early.open_expenses] == [a.id]

    on_day = owner_summary(session, D3)
    assert (on_day.owed_to_me, on_day.open_expense_total, on_day.matches) == (
        D("20.00"),
        D("20.00"),
        True,
    )
    assert [e.id for e in on_day.open_expenses] == [b.id]
    assert owner_summary(session, date(2026, 2, 1)) == OwnerSummary(D("0.00"), D("0.00"), [], True)


def test_as_of_handles_later_voids_of_expenses_and_reimbursements(
    session, owner_user, chart, expense_account
):
    a = owner_expense(session, owner_user, expense_account, "10.00", D1)
    b = owner_expense(session, owner_user, expense_account, "20.00", D1)
    ExpenseService(session).void(b.id, actor=owner_user, void_date=D3)
    reim = reimburse(session, owner_user, chart, [a], D2)
    ReimbursementService(session).void(reim.id, actor=owner_user, void_date=D4)

    # between creation and any void: both expenses open, a not yet reimbursed
    s = owner_summary(session, D1)
    assert (s.owed_to_me, s.open_expense_total, s.matches) == (D("30.00"), D("30.00"), True)
    # a reimbursed (D2), b still open (void is D3)
    s = owner_summary(session, D2)
    assert (s.owed_to_me, s.open_expense_total, s.matches) == (D("20.00"), D("20.00"), True)
    assert [e.id for e in s.open_expenses] == [b.id]
    # b voided, a still reimbursed (reimbursement void is D4)
    s = owner_summary(session, D3)
    assert (s.owed_to_me, s.open_expense_total, s.matches) == (D("0.00"), D("0.00"), True)
    # reimbursement voided: a open again
    s = owner_summary(session, D4)
    assert (s.owed_to_me, s.open_expense_total, s.matches) == (D("10.00"), D("10.00"), True)
    assert [e.id for e in s.open_expenses] == [a.id]


def test_unbound_role_reports_zero_owed_and_still_lists_open_expenses(
    session, owner_user, expense_account, owner_role
):
    a = owner_expense(session, owner_user, expense_account, "10.00", D1)
    session.execute(
        text("UPDATE account SET system_role = NULL WHERE id = :i"), {"i": owner_role.id}
    )
    session.commit()
    summary = owner_summary(session)
    assert summary.owed_to_me == D("0.00")
    assert summary.open_expense_total == D("10.00")
    assert [e.id for e in summary.open_expenses] == [a.id]
    assert summary.matches is False
