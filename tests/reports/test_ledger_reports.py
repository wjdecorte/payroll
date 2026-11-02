from datetime import date
from decimal import Decimal

import pytest

from payroll.ledger.exceptions import AccountNotFoundError
from payroll.reports.periods import InvalidPeriodError
from payroll.reports.statements import ReportService

D = Decimal
ZERO = D("0.00")
YEAR_END = date(2026, 12, 31)


def _tb(report):
    return {r.name: (r.debit, r.credit) for r in report.rows}


def test_trial_balance_columns_and_totals_literal(session, books):
    report = ReportService(session).trial_balance(YEAR_END)
    assert _tb(report) == {
        "Business Checking": (D("14999.25"), ZERO),
        "Opening Balance Equity": (ZERO, D("10000.00")),
        "Services Revenue": (ZERO, D("6500.00")),
        "Software and Subscriptions": (D("1200.50"), ZERO),
        "Office Expenses": (D("300.25"), ZERO),
    }
    assert [r.number for r in report.rows] == ["1000", "3900", "4000", "6210", "6220"]
    assert report.total_debit == D("16500.00")
    assert report.total_credit == D("16500.00")
    assert report.balanced is True


def test_trial_balance_include_zero_lists_every_account(session, books):
    report = ReportService(session).trial_balance(YEAR_END, include_zero=True)
    names = {r.name for r in report.rows}
    assert {"Travel", "Accounts Receivable", "Wages"} <= names
    assert _tb(report)["Travel"] == (ZERO, ZERO)
    assert report.total_debit == D("16500.00")


def test_trial_balance_empty_ledger_is_balanced_zero(session, chart):
    report = ReportService(session).trial_balance(YEAR_END)
    assert report.rows == []
    assert report.total_debit == ZERO
    assert report.total_credit == ZERO
    assert report.balanced is True


def test_trial_balance_as_of_before_first_entry_is_empty(session, books):
    report = ReportService(session).trial_balance(date(2026, 1, 1))
    assert report.rows == []
    assert report.balanced is True


def test_trial_balance_as_of_is_inclusive_and_partial(session, books):
    report = ReportService(session).trial_balance(date(2026, 1, 15))
    assert _tb(report) == {
        "Business Checking": (D("14000.00"), ZERO),
        "Opening Balance Equity": (ZERO, D("10000.00")),
        "Services Revenue": (ZERO, D("4000.00")),
    }


def test_register_running_balance_and_opening_row(session, books):
    chk = books.chart["Business Checking"]
    register = ReportService(session).account_register(chk.id, date(2026, 2, 1), date(2026, 2, 28))
    assert register.normal_balance == "debit"
    assert register.opening_balance == D("12799.50")
    assert [(ln.entry_date, ln.debit, ln.credit, ln.balance) for ln in register.lines] == [
        (date(2026, 2, 10), D("2500.00"), ZERO, D("15299.50")),
        (date(2026, 2, 15), ZERO, D("300.25"), D("14999.25")),
        (date(2026, 2, 20), ZERO, D("99.99"), D("14899.26")),
        (date(2026, 2, 21), D("99.99"), ZERO, D("14999.25")),
    ]
    first = register.lines[0]
    assert (first.entry_id, first.entry_no, first.source_type) == (
        books.sale_feb.id,
        2,
        "manual",
    )
    assert first.memo == "February sale"
    assert register.total_debit == D("2599.99")
    assert register.total_credit == D("400.24")
    assert register.closing_balance == D("14999.25")


def test_register_credit_normal_account_uses_credit_sign(session, books):
    rev = books.chart["Services Revenue"]
    register = ReportService(session).account_register(rev.id, date(2026, 2, 1), date(2026, 2, 28))
    assert register.normal_balance == "credit"
    assert register.opening_balance == D("4000.00")
    assert [ln.balance for ln in register.lines] == [D("6500.00")]


def test_register_with_no_activity_still_shows_opening_and_closing(session, books):
    rev = books.chart["Services Revenue"]
    register = ReportService(session).account_register(rev.id, date(2026, 6, 1), date(2026, 6, 30))
    assert register.lines == []
    assert register.opening_balance == register.closing_balance == D("6500.00")


def test_register_validates_period_and_account(session, books):
    svc = ReportService(session)
    chk = books.chart["Business Checking"]
    with pytest.raises(InvalidPeriodError):
        svc.account_register(chk.id, date(2026, 3, 1), date(2026, 2, 1))
    with pytest.raises(AccountNotFoundError):
        svc.account_register(999_999, date(2026, 1, 1), date(2026, 2, 1))


def test_contra_balance_keeps_its_sign(session, books):
    books.post(date(2026, 3, 1), "Business Checking", "Bank Charges", "25.00", "Fee refund")
    bank = books.chart["Bank Charges"]
    svc = ReportService(session)
    register = svc.account_register(bank.id, date(2026, 3, 1), date(2026, 3, 31))
    assert register.closing_balance == D("-25.00")
    assert register.lines[0].balance == D("-25.00")
    assert _tb(svc.trial_balance(YEAR_END))["Bank Charges"] == (ZERO, D("25.00"))


def test_general_ledger_skips_accounts_without_activity(session, books):
    ledger = ReportService(session).general_ledger(date(2026, 2, 1), date(2026, 2, 28))
    assert [a.name for a in ledger.accounts] == [
        "Business Checking",  # activity and an opening balance
        "Opening Balance Equity",  # opening balance only
        "Services Revenue",
        "Software and Subscriptions",  # opening balance only
        "Office Expenses",
        "Travel",
    ]
    by_name = {a.name: a for a in ledger.accounts}
    assert by_name["Opening Balance Equity"].lines == []
    assert by_name["Opening Balance Equity"].opening_balance == D("10000.00")
    assert by_name["Software and Subscriptions"].closing_balance == D("1200.50")
    assert by_name["Travel"].opening_balance == ZERO
    assert len(by_name["Travel"].lines) == 2

    january = ReportService(session).general_ledger(date(2026, 1, 1), date(2026, 1, 1))
    assert january.accounts == []


def test_general_ledger_single_account_filter(session, books):
    office = books.chart["Office Expenses"]
    ledger = ReportService(session).general_ledger(date(2026, 1, 1), YEAR_END, account_id=office.id)
    (register,) = ledger.accounts
    assert register.name == "Office Expenses"
    assert register.closing_balance == D("300.25")
    with pytest.raises(AccountNotFoundError):
        ReportService(session).general_ledger(date(2026, 1, 1), YEAR_END, account_id=999_999)


def test_journal_orders_and_lists_all_lines(session, books):
    entries = ReportService(session).journal(date(2026, 1, 1), YEAR_END)
    assert [(e.entry_date.isoformat(), e.source_type, e.entry_no) for e in entries] == [
        ("2026-01-02", "opening_balance", 1),
        ("2026-01-15", "manual", 1),
        ("2026-01-20", "manual", 3),
        ("2026-02-10", "manual", 2),
        ("2026-02-15", "manual", 4),
        ("2026-02-20", "manual", 5),
        ("2026-02-21", "manual", 6),
    ]
    software = entries[2]
    assert [(ln.line_no, ln.account_name, ln.debit, ln.credit) for ln in software.lines] == [
        (1, "Software and Subscriptions", D("1200.50"), ZERO),
        (2, "Business Checking", ZERO, D("1200.50")),
    ]
    assert software.total_debit == software.total_credit == D("1200.50")
    assert software.lines[0].account_number == "6210"
    assert sum(len(e.lines) for e in entries) == 14
    # Date window is inclusive on both ends.
    window = ReportService(session).journal(date(2026, 1, 15), date(2026, 1, 20))
    assert [e.id for e in window] == [books.sale_jan.id, books.software.id]


def test_journal_validates_period(session, books):
    with pytest.raises(InvalidPeriodError):
        ReportService(session).journal(date(2026, 2, 1), date(2026, 1, 1))


def test_void_and_reversal_both_appear_and_net_zero(session, books):
    original = books.post(date(2026, 3, 5), "Meals", "Business Checking", "80.00", "Lunch")
    reversal = books.posting.void(original.id, entry_date=date(2026, 3, 6), actor=books.owner)
    svc = ReportService(session)

    entries = {e.id: e for e in svc.journal(date(2026, 3, 1), date(2026, 3, 31))}
    assert set(entries) == {original.id, reversal.id}
    assert (entries[original.id].is_void, entries[original.id].is_reversal) == (True, False)
    assert (entries[reversal.id].is_void, entries[reversal.id].is_reversal) == (False, True)
    assert entries[reversal.id].reverses_entry_id == original.id

    meals = books.chart["Meals"]
    register = svc.account_register(meals.id, date(2026, 3, 1), date(2026, 3, 31))
    assert [ln.balance for ln in register.lines] == [D("80.00"), ZERO]
    assert [(ln.is_void, ln.is_reversal) for ln in register.lines] == [
        (True, False),
        (False, True),
    ]
    assert register.closing_balance == ZERO
    assert "Meals" not in _tb(svc.trial_balance(YEAR_END))
