"""Cash basis: income is recognized when invoices are paid (the Task 9 row source).

Documents are built through the receivables services (``InvoiceService.issue``,
``PaymentService.record`` / ``void``) so the numbers below come from real
``payment_application`` / ``application_income`` rows. The base scenario is an invoice of
1,000.00 dated 2026-01-10 over two income accounts, Consulting (4100) 600.00 and
Training (4200) 400.00.
"""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.receivables.balances import invoice_states
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer, Item
from payroll.receivables.payments import ApplicationInput, PaymentService
from payroll.reports import queries
from payroll.reports.basis import BASIS_NOTE, Basis
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService

D = Decimal
ZERO = D("0.00")
CASH, ACCRUAL = Basis.CASH, Basis.ACCRUAL
INVOICE_DAY = date(2026, 1, 10)
YEAR = Period(date(2026, 1, 1), date(2026, 12, 31), "This year")
JAN = Period(date(2026, 1, 1), date(2026, 1, 31), "January")
FEB = Period(date(2026, 2, 1), date(2026, 2, 28), "February")
MAR = Period(date(2026, 3, 1), date(2026, 3, 31), "March")

AR = "Accounts Receivable"
UF = "Undeposited Funds"
A, B, C = "Consulting", "Training", "Workshops"


@pytest.fixture
def income(session, owner_user, chart) -> dict[str, Account]:
    service = AccountService(session)
    added = {
        name: service.create(actor=owner_user, name=name, type=AccountType.INCOME, number=number)
        for name, number in ((A, "4100"), (B, "4200"), (C, "4300"))
    }
    chart.update(added)
    return added


@pytest.fixture
def customer(session) -> Customer:
    row = Customer(name="Acme Corp")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def items(session, income) -> dict[str, Item]:
    rows = {
        name: Item(name=f"{name} item", unit_price=ZERO, income_account_id=account.id)
        for name, account in income.items()
    }
    session.add_all(rows.values())
    session.flush()
    return rows


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, income, customer, items):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


@pytest.fixture
def make(session, owner_user, chart, customer, items):
    class Make:
        def invoice(self, day=INVOICE_DAY, lines=((A, "600.00"), (B, "400.00"))):
            service = InvoiceService(session)
            draft = service.create_draft(
                actor=owner_user,
                customer_id=customer.id,
                invoice_date=day,
                lines=[
                    InvoiceLineInput(items[name].id, name, D("1"), D(price))
                    for name, price in lines
                ],
            )
            return service.issue(draft.id, actor=owner_user)

        def pay(self, day, amount, *applied):
            """Record a payment of ``amount`` on ``day``; ``applied`` = (invoice, amount) pairs."""
            return PaymentService(session).record(
                actor=owner_user,
                customer_id=customer.id,
                received_date=day,
                amount=D(amount),
                deposit_account_id=chart[UF].id,
                method="check",
                applications=[ApplicationInput(inv.id, D(value)) for inv, value in applied],
            )

        def void_payment(self, payment, day):
            return PaymentService(session).void(payment.id, actor=owner_user, void_date=day)

    return Make()


def _amounts(statement) -> dict[str, list[Decimal]]:
    return {r.label: r.amounts for r in statement.rows}


def _pnl(session, period, basis=CASH, **kwargs):
    return _amounts(ReportService(session).profit_and_loss(period, basis=basis, **kwargs))


def _bs(session, as_of, basis=CASH, **kwargs):
    return _amounts(ReportService(session).balance_sheet(as_of, basis=basis, **kwargs))


def _ok(statement) -> dict[str, bool]:
    return {i.name: i.ok for i in statement.invariants}


# ----------------------------------------------------------------------
# The base scenario: 1,000.00 invoiced 2026-01-10, 500.00 paid 2026-02-05
# ----------------------------------------------------------------------


@pytest.fixture
def partly_paid(make):
    invoice = make.invoice()
    payment = make.pay(date(2026, 2, 5), "500.00", (invoice, "500.00"))
    return invoice, payment


def test_accrual_pnl_recognizes_invoice_in_january(session, partly_paid):
    jan = _pnl(session, JAN, ACCRUAL)
    assert jan[A] == [D("600.00")]
    assert jan[B] == [D("400.00")]
    assert jan["Net Income"] == [D("1000.00")]
    feb = _pnl(session, FEB, ACCRUAL)
    assert A not in feb and B not in feb
    assert feb["Net Income"] == [ZERO]


def test_cash_pnl_january_income_is_zero_and_february_is_500_split_300_200(session, partly_paid):
    jan = _pnl(session, JAN)
    assert A not in jan and B not in jan
    assert jan["Net Income"] == [ZERO]
    feb = _pnl(session, FEB)
    assert feb[A] == [D("300.00")]
    assert feb[B] == [D("200.00")]
    assert feb["Total Income"] == [D("500.00")]
    assert feb["Net Income"] == [D("500.00")]


def test_cash_balance_sheet_has_no_receivable_after_full_payment(session, make, partly_paid):
    invoice, _ = partly_paid
    # Before any payment, accrual carries the receivable and cash carries nothing.
    assert _bs(session, JAN.end, ACCRUAL)[AR] == [D("1000.00")]
    assert AR not in _bs(session, JAN.end)
    make.pay(date(2026, 3, 5), "500.00", (invoice, "500.00"))
    cash = _bs(session, MAR.end)
    assert cash[AR] == [ZERO]  # activity (payment credits, application debits) nets to zero
    assert cash[UF] == [D("1000.00")]
    assert cash["Net Income"] == [D("1000.00")]
    assert cash["Total Assets"] == cash["Liabilities + Equity"] == [D("1000.00")]
    assert _bs(session, MAR.end, ACCRUAL)[AR] == [ZERO]


def test_accrual_balance_sheet_shows_open_receivable_500(session, partly_paid):
    accrual = _bs(session, FEB.end, ACCRUAL)
    assert accrual[AR] == [D("500.00")]
    assert accrual[UF] == [D("500.00")]
    assert accrual["Net Income"] == [D("1000.00")]
    cash = _bs(session, FEB.end)
    assert cash[AR] == [ZERO]
    assert cash["Net Income"] == [D("500.00")]


def test_cash_pnl_final_payment_completes_income_to_1000(session, make, partly_paid):
    invoice, _ = partly_paid
    make.pay(date(2026, 3, 5), "500.00", (invoice, "500.00"))
    march = _pnl(session, MAR)
    assert march[A] == [D("300.00")]
    assert march[B] == [D("200.00")]
    quarter = _pnl(session, Period(JAN.start, MAR.end, "Q1"))
    assert quarter[A] == [D("600.00")]
    assert quarter[B] == [D("400.00")]
    assert quarter["Net Income"] == [D("1000.00")]
    # Paid in full, cash equals accrual for the quarter.
    assert quarter == _pnl(session, Period(JAN.start, MAR.end, "Q1"), ACCRUAL)


def test_uneven_split_sums_to_payment(session, make):
    invoice = make.invoice(lines=((A, "33.33"), (B, "33.33"), (C, "33.34")))
    for day, amount in ((date(2026, 2, 1), "33.33"), (date(2026, 2, 2), "33.33")):
        make.pay(day, amount, (invoice, amount))
    first_two = _pnl(session, Period(date(2026, 2, 1), date(2026, 2, 2), "two"))
    assert [first_two[n] for n in (A, B, C)] == [[D("22.22")]] * 3
    make.pay(date(2026, 2, 3), "33.34", (invoice, "33.34"))
    cash = _pnl(session, YEAR)
    assert cash[A] == [D("33.33")]
    assert cash[B] == [D("33.33")]
    assert cash[C] == [D("33.34")]
    assert cash["Net Income"] == [D("100.00")]  # no cent lost
    assert _pnl(session, YEAR, ACCRUAL) == cash


def test_overpayment_is_not_income_on_cash_basis(session, make):
    invoice = make.invoice()
    make.pay(date(2026, 2, 5), "1200.00", (invoice, "1000.00"))
    assert _pnl(session, YEAR)["Net Income"] == [D("1000.00")]
    for basis in (CASH, ACCRUAL):
        sheet = _bs(session, YEAR.end, basis)
        assert sheet[AR] == [D("-200.00")]
        assert sheet[UF] == [D("1200.00")]
        assert sheet["Total Assets"] == sheet["Liabilities + Equity"] == [D("1000.00")]


def test_payment_before_invoice_date_recognized_on_invoice_date(session, make):
    invoice = make.invoice(day=date(2026, 1, 10))
    make.pay(date(2026, 1, 5), "1000.00", (invoice, "1000.00"))
    between = _bs(session, date(2026, 1, 7))
    assert between[AR] == [D("-1000.00")]  # money received, not yet income
    assert between[UF] == [D("1000.00")]
    assert "Net Income" not in between
    assert _pnl(session, Period(date(2026, 1, 1), date(2026, 1, 9), "early"))["Net Income"] == [
        ZERO
    ]
    on_invoice_day = _pnl(session, Period(date(2026, 1, 10), date(2026, 1, 10), "day"))
    assert on_invoice_day[A] == [D("600.00")]
    assert on_invoice_day["Net Income"] == [D("1000.00")]
    assert _bs(session, date(2026, 1, 10))[AR] == [ZERO]


def test_payment_voided_after_application_reverses_cash_income_on_void_date(
    session, make, partly_paid
):
    _, payment = partly_paid
    make.void_payment(payment, date(2026, 3, 10))
    before_void = Period(JAN.start, date(2026, 3, 9), "before")
    assert _pnl(session, before_void)["Net Income"] == [D("500.00")]
    assert _pnl(session, Period(JAN.start, MAR.end, "Q1"))["Net Income"] == [ZERO]
    march = _pnl(session, MAR)
    assert march[A] == [D("-300.00")]
    assert march[B] == [D("-200.00")]
    assert _bs(session, date(2026, 3, 9))[UF] == [D("500.00")]
    after = _bs(session, MAR.end)
    assert after[AR] == [ZERO] and after[UF] == [ZERO]
    assert "Net Income" not in after
    # Accrual is unaffected by the application: the invoice is income, the void reopens A/R.
    assert _pnl(session, before_void, ACCRUAL)["Net Income"] == [D("1000.00")]
    assert _pnl(session, Period(JAN.start, MAR.end, "Q1"), ACCRUAL)["Net Income"] == [D("1000.00")]
    assert _bs(session, MAR.end, ACCRUAL)[AR] == [D("1000.00")]


def test_payment_voided_before_recognition_never_reaches_cash_income(session, make):
    """A void dated before the application's recognition date cancels it on that date."""
    invoice = make.invoice(day=date(2026, 3, 1))
    payment = make.pay(date(2026, 1, 5), "1000.00", (invoice, "1000.00"))
    make.void_payment(payment, date(2026, 2, 1))
    for end in (date(2026, 2, 15), date(2026, 3, 1), YEAR.end):
        assert _pnl(session, Period(JAN.start, end, "to date"))["Net Income"] == [ZERO]
        sheet = _bs(session, end)
        assert sheet[AR] == [ZERO]
        assert sheet["Total Assets"] == sheet["Liabilities + Equity"] == [ZERO]


def test_invoice_voided_before_any_payment_has_no_cash_effect_and_zero_accrual_net(
    session, owner_user, make
):
    invoice = make.invoice()
    InvoiceService(session).void(invoice.id, actor=owner_user, void_date=date(2026, 1, 20))
    early = Period(JAN.start, date(2026, 1, 15), "early")
    assert _pnl(session, early, ACCRUAL)["Net Income"] == [D("1000.00")]
    assert _pnl(session, early)["Net Income"] == [ZERO]
    accrual_year = _pnl(session, YEAR, ACCRUAL)
    assert accrual_year[A] == [ZERO] and accrual_year["Net Income"] == [ZERO]
    cash_year = _pnl(session, YEAR)
    assert A not in cash_year and cash_year["Net Income"] == [ZERO]
    assert AR not in _bs(session, YEAR.end)
    assert _bs(session, YEAR.end, ACCRUAL)[AR] == [ZERO]


def test_voided_invoice_replacement_counts_once_on_accrual(session, owner_user, make, items):
    invoice = make.invoice()
    InvoiceService(session).replace(
        invoice.id,
        actor=owner_user,
        customer_id=invoice.customer_id,
        invoice_date=INVOICE_DAY,
        lines=[
            InvoiceLineInput(items[A].id, A, D("1"), D("800.00")),
            InvoiceLineInput(items[B].id, B, D("1"), D("400.00")),
        ],
    )
    accrual = _pnl(session, YEAR, ACCRUAL)
    assert accrual[A] == [D("800.00")]
    assert accrual[B] == [D("400.00")]
    assert accrual["Net Income"] == [D("1200.00")]
    assert _bs(session, YEAR.end, ACCRUAL)[AR] == [D("1200.00")]
    assert _pnl(session, YEAR)["Net Income"] == [ZERO]
    make.pay(date(2026, 2, 5), "1200.00", (invoice, "1200.00"))
    cash = _pnl(session, YEAR)
    assert cash[A] == [D("800.00")]
    assert cash[B] == [D("400.00")]
    assert cash["Net Income"] == [D("1200.00")]


def test_cash_balance_sheet_balances_with_prior_year_earnings(session, make):
    invoice = make.invoice(day=date(2025, 12, 15))
    make.pay(date(2026, 1, 20), "1000.00", (invoice, "1000.00"))
    service = ReportService(session)
    dec = service.balance_sheet(date(2025, 12, 31), basis=CASH)
    assert "Net Income" not in _amounts(dec) and AR not in _amounts(dec)
    assert _amounts(service.balance_sheet(date(2025, 12, 31), basis=ACCRUAL))[AR] == [D("1000.00")]
    march = service.balance_sheet(MAR.end, basis=CASH)
    assert _amounts(march)["Net Income"] == [D("1000.00")]
    assert "Prior Years Earnings" not in _amounts(march)
    accrual_march = _amounts(service.balance_sheet(MAR.end, basis=ACCRUAL))
    assert accrual_march["Prior Years Earnings"] == [D("1000.00")]
    assert "Net Income" not in accrual_march
    next_year = service.balance_sheet(date(2027, 6, 30), basis=CASH)
    assert _amounts(next_year)["Prior Years Earnings"] == [D("1000.00")]
    assert _amounts(next_year)["Total Assets"] == _amounts(next_year)["Liabilities + Equity"]
    for statement in (dec, march, next_year):
        assert _ok(statement) == {
            "assets_equal_liabilities_plus_equity": True,
            "trial_balance_balanced": True,
        }


# ----------------------------------------------------------------------
# A mixed scenario for the invariants
# ----------------------------------------------------------------------


@pytest.fixture
def mixed(session, owner_user, chart, make):
    """Invoices, partial / over / voided payments, a voided invoice and a manual sale."""
    inv1 = make.invoice()  # 1,000.00 on 2026-01-10
    inv2 = make.invoice(day=date(2026, 2, 1), lines=((A, "300.00"),))
    inv3 = make.invoice(day=date(2026, 3, 20), lines=((C, "200.00"),))
    make.pay(date(2026, 2, 5), "500.00", (inv1, "500.00"))
    make.pay(date(2026, 3, 3), "400.00", (inv2, "300.00"))  # 100.00 left unapplied
    InvoiceService(session).void(inv3.id, actor=owner_user, void_date=date(2026, 4, 2))
    voided = make.pay(date(2026, 4, 10), "250.00", (inv1, "250.00"))
    make.void_payment(voided, date(2026, 5, 15))
    make.pay(date(2026, 5, 1), "700.00", (inv1, "500.00"))  # 200.00 left unapplied
    inv4 = make.invoice(day=date(2026, 6, 10), lines=((B, "450.00"),))
    make.pay(date(2026, 6, 20), "100.00", (inv4, "100.00"))
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 4, 4),
            [
                LineDraft(chart["Business Checking"].id, debit=D("75.00")),
                LineDraft(chart["Services Revenue"].id, credit=D("75.00")),
            ],
            memo="Cash sale",
            source_type=SourceType.MANUAL,
        ),
        actor=owner_user,
    )
    session.commit()


def _open_receivables(session, as_of: date) -> Decimal:
    return sum((s.balance for s in invoice_states(session, as_of)), ZERO)


def test_cash_and_accrual_net_income_differ_exactly_by_open_receivable_change(session, mixed):
    edges = [date(2025, 12, 31)] + [
        date(2026, m, d) for m, d in ((1, 31), (2, 28), (3, 31), (4, 30), (5, 31), (6, 30))
    ]
    differences = []
    for prev_end, end in [*zip(edges, edges[1:], strict=False), (edges[0], edges[-1])]:
        start = date.fromordinal(prev_end.toordinal() + 1)
        window = Period(start, end, "")
        accrual = _pnl(session, window, ACCRUAL)["Net Income"][0]
        cash = _pnl(session, window)["Net Income"][0]
        change = _open_receivables(session, end) - _open_receivables(session, prev_end)
        assert accrual - cash == change, (start, end)
        differences.append(change)
    # The scenario is not trivial: the difference is non-zero in several windows.
    assert sum(1 for d in differences if d != 0) >= 4
    # Over the half year 1,750.00 stays invoiced (inv3 is void) and 1,400.00 of it is paid
    # by live payments (the voided 250.00 does not count): 350.00 is still open.
    assert differences[-1] == D("350.00")
    assert differences[:-1] == [
        D("1000.00"),  # January: inv1 issued, nothing paid
        D("-200.00"),  # February: inv2 +300, 500 paid on inv1
        D("-100.00"),  # March: inv2 paid -300; inv3 +200 issued
        D("-450.00"),  # April: inv3 void -200, 250 applied to inv1
        D("-250.00"),  # May: 500 paid on inv1, the 250 payment voided (+250)
        D("350.00"),  # June: inv4 +450, 100 paid
    ]


def test_cash_invariants_ok_and_basis_note_text(session, mixed):
    assert BASIS_NOTE == (
        "Cash basis recognizes income when an invoice is paid. Money received but not yet "
        "applied to an invoice stays in Accounts Receivable as a credit; accrual basis "
        "recognizes income when the invoice is issued."
    )
    service = ReportService(session)
    for as_of in (date(2026, 1, 9), date(2026, 3, 3), date(2026, 5, 14), date(2026, 5, 15)):
        for basis in (CASH, ACCRUAL):
            for statement in (
                service.balance_sheet(as_of, basis=basis, compare="previous_period"),
                service.profit_and_loss(Period(JAN.start, as_of, ""), basis=basis),
            ):
                assert statement.basis_note == BASIS_NOTE
                assert _ok(statement) == {
                    "assets_equal_liabilities_plus_equity": True,
                    "trial_balance_balanced": True,
                }, (as_of, basis, statement.invariants)
    debit, credit = queries.debit_credit_totals(session, basis=CASH, on_or_before=YEAR.end)
    assert debit == credit
    assert debit != queries.debit_credit_totals(session, basis=ACCRUAL, on_or_before=YEAR.end)[0]
    # Unapplied money (100.00 + 200.00) is the cash-basis A/R credit.
    assert _bs(session, YEAR.end)[AR] == [D("-300.00")]


def test_trial_balance_and_ledger_reports_ignore_basis(session, partly_paid):
    _, payment = partly_paid
    service = ReportService(session)
    tb = {r.name: (r.debit, r.credit) for r in service.trial_balance(FEB.end).rows}
    assert tb[AR] == (D("500.00"), ZERO)  # the ledger as posted (cash shows 0.00)
    assert tb[A] == (ZERO, D("600.00"))
    assert _bs(session, FEB.end)[AR] == [ZERO]
    ledger = {r.name: r for r in service.general_ledger(JAN.start, FEB.end).accounts}
    assert [ln.source_type for ln in ledger[A].lines] == ["invoice"]
    assert ledger[A].closing_balance == D("600.00")
    assert [(ln.source_type, ln.debit, ln.credit) for ln in ledger[AR].lines] == [
        ("invoice", D("1000.00"), ZERO),
        ("payment", ZERO, D("500.00")),
    ]
    register = service.account_register(ledger[AR].account_id, JAN.start, FEB.end)
    assert register.closing_balance == D("500.00")
    journal = service.journal(JAN.start, FEB.end)
    assert [(e.source_type, len(e.lines)) for e in journal] == [("invoice", 3), ("payment", 2)]
    assert payment.entry_id in {e.id for e in journal}


def test_comparison_columns_work_on_cash_basis(session, partly_paid):
    statement = ReportService(session).profit_and_loss(FEB, basis=CASH, compare="previous_period")
    net = next(r for r in statement.rows if r.label == "Net Income")
    assert net.amounts == [D("500.00"), ZERO]
    assert net.change == D("500.00") and net.pct_change is None
    consulting = next(r for r in statement.rows if r.label == A)
    assert consulting.amounts == [D("300.00"), ZERO]
    sheet = ReportService(session).balance_sheet(FEB.end, basis=CASH, compare="previous_period")
    rows = _amounts(sheet)
    assert rows["Net Income"] == [D("500.00"), ZERO]
    assert rows[UF] == [D("500.00"), ZERO]
    assert rows["Total Assets"] == rows["Liabilities + Equity"]
    accrual = _amounts(
        ReportService(session).profit_and_loss(FEB, basis=ACCRUAL, compare="previous_period")
    )
    assert accrual["Net Income"] == [ZERO, D("1000.00")]


def test_manual_journal_to_income_is_cash_recognized_on_its_date(session, owner_user, chart):
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 4, 4),
            [
                LineDraft(chart["Business Checking"].id, debit=D("250.00")),
                LineDraft(chart[A].id, credit=D("250.00")),
            ],
            memo="Cash sale",
            source_type=SourceType.MANUAL,
        ),
        actor=owner_user,
    )
    april = Period(date(2026, 4, 1), date(2026, 4, 30), "April")
    cash = _pnl(session, april)
    assert cash[A] == [D("250.00")]
    assert cash["Net Income"] == [D("250.00")]
    assert cash == _pnl(session, april, ACCRUAL)
    assert _pnl(session, MAR)["Net Income"] == [ZERO]
    assert _bs(session, april.end) == _bs(session, april.end, ACCRUAL)
    assert _bs(session, april.end)["Business Checking"] == [D("250.00")]


# ----------------------------------------------------------------------
# Detail rows (the cash-basis register builds on these)
# ----------------------------------------------------------------------


def test_cash_detail_rows_replace_invoice_lines_with_application_rows(
    session, chart, make, partly_paid
):
    invoice, payment = partly_paid
    make.void_payment(payment, date(2026, 3, 10))
    memo = f"Invoice #{invoice.number} paid"
    cash = queries.basis_lines(session, JAN.start, MAR.end, basis=CASH)
    assert {row.source_type for row in cash} == {"payment"}  # no invoice lines
    assert [
        (row.account.name, row.entry_date, row.line_id is None, row.debit, row.credit)
        for row in cash
    ] == [
        (AR, date(2026, 2, 5), False, ZERO, D("500.00")),  # the payment entry
        (AR, date(2026, 2, 5), True, D("500.00"), ZERO),  # the application
        (AR, date(2026, 3, 10), True, ZERO, D("500.00")),  # the application's mirror
        (AR, date(2026, 3, 10), False, D("500.00"), ZERO),  # the payment's reversal
        (UF, date(2026, 2, 5), False, D("500.00"), ZERO),
        (UF, date(2026, 3, 10), False, ZERO, D("500.00")),
        (A, date(2026, 2, 5), True, ZERO, D("300.00")),
        (A, date(2026, 3, 10), True, D("300.00"), ZERO),
        (B, date(2026, 2, 5), True, ZERO, D("200.00")),
        (B, date(2026, 3, 10), True, D("200.00"), ZERO),
    ]
    applied = [row for row in cash if row.line_id is None]
    assert {
        (row.entry_id, row.entry_no, row.source_type, row.line_no, row.memo) for row in applied
    } == {(payment.entry_id, cash[0].entry_no, "payment", None, memo)}
    assert cash[3].entry_no > cash[0].entry_no  # the reversal is a later payment entry
    assert {row.application_id for row in applied} == {cash[1].application_id}
    assert cash[1].application_id is not None and cash[0].application_id is None
    assert [row.is_reversal for row in applied] == [False, True] * 3
    assert all(row.is_void for row in applied)  # the payment entry is void now
    one = queries.basis_lines(session, JAN.start, MAR.end, basis=CASH, account_id=chart[A].id)
    assert [(r.debit, r.credit) for r in one] == [(ZERO, D("300.00")), (D("300.00"), ZERO)]
    feb = queries.basis_lines(session, FEB.start, FEB.end, basis=CASH)
    assert len(feb) == 5
    accrual = queries.basis_lines(session, JAN.start, MAR.end, basis=ACCRUAL)
    assert all(row.line_id is not None for row in accrual)
    assert [row.source_type for row in accrual].count("invoice") == 3
    assert [row.memo for row in accrual if row.source_type == "invoice"][0] is not None
