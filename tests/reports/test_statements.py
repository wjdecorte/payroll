from datetime import date
from decimal import Decimal

import pytest

from payroll.ledger.models import SourceType
from payroll.ledger.services import EntryDraft, LineDraft
from payroll.reports import queries
from payroll.reports.basis import BASIS_NOTE, Basis
from payroll.reports.periods import InvalidPeriodError, Period
from payroll.reports.statements import ReportService, pct_change

D = Decimal
ZERO = D("0.00")
YEAR = Period(date(2026, 1, 1), date(2026, 12, 31), "This year")
FEB = Period(date(2026, 2, 1), date(2026, 2, 28), "Last month")

PNL_STRUCTURE = [
    ("section", "Income"),
    ("subtotal", "Total Income"),
    ("section", "Cost of Goods Sold"),
    ("subtotal", "Total Cost of Goods Sold"),
    ("total", "Gross Profit"),
    ("section", "Expenses"),
    ("subtotal", "Total Expenses"),
    ("total", "Net Operating Income"),
    ("section", "Other Income"),
    ("subtotal", "Total Other Income"),
    ("section", "Other Expenses"),
    ("subtotal", "Total Other Expenses"),
    ("total", "Net Income"),
]


def _amounts(statement, kind: str | None = None) -> dict[str, list[Decimal]]:
    return {r.label: r.amounts for r in statement.rows if kind is None or r.kind == kind}


def _row(statement, label: str):
    return next(r for r in statement.rows if r.label == label)


def _invariants(statement) -> dict[str, bool]:
    return {i.name: i.ok for i in statement.invariants}


# ----------------------------------------------------------------------
# Profit & Loss
# ----------------------------------------------------------------------


def test_pnl_net_income_equals_income_minus_expenses(session, books):
    pnl = ReportService(session).profit_and_loss(YEAR)
    assert pnl.title == "Profit and Loss"
    assert [(c.label, c.start, c.end) for c in pnl.columns] == [
        ("2026-01-01 to 2026-12-31", date(2026, 1, 1), date(2026, 12, 31))
    ]
    assert [(r.kind, r.label, r.depth, r.amounts) for r in pnl.rows] == [
        ("section", "Income", 0, []),
        ("account", "Services Revenue", 1, [D("6500.00")]),
        ("subtotal", "Total Income", 0, [D("6500.00")]),
        ("total", "Gross Profit", 0, [D("6500.00")]),
        ("section", "Expenses", 0, []),
        ("account", "Software and Subscriptions", 1, [D("1200.50")]),
        ("account", "Office Expenses", 1, [D("300.25")]),
        ("account", "Travel", 1, [ZERO]),
        ("subtotal", "Total Expenses", 0, [D("1500.75")]),
        ("total", "Net Operating Income", 0, [D("4999.25")]),
        ("total", "Net Income", 0, [D("4999.25")]),
    ]
    assert _row(pnl, "Services Revenue").account_id == books.chart["Services Revenue"].id
    assert _row(pnl, "Net Income").account_id is None
    assert all(r.change is None and r.pct_change is None for r in pnl.rows)
    assert _invariants(pnl) == {
        "assets_equal_liabilities_plus_equity": True,
        "trial_balance_balanced": True,
    }


def test_pnl_sections_and_gross_profit_with_cogs(session, books, extra_accounts):
    chk = "Business Checking"
    books.post(date(2026, 3, 1), "Materials", chk, "1000.00")
    books.post(date(2026, 3, 2), chk, "Interest Income", "50.00")
    books.post(date(2026, 3, 3), "Penalties", chk, "25.00")
    pnl = ReportService(session).profit_and_loss(YEAR)
    assert [(r.kind, r.label) for r in pnl.rows if r.kind != "account"] == PNL_STRUCTURE
    totals = {label: amounts[0] for label, amounts in _amounts(pnl).items() if amounts}
    assert totals["Total Income"] == D("6500.00")
    assert totals["Materials"] == D("1000.00")
    assert totals["Total Cost of Goods Sold"] == D("1000.00")
    assert totals["Gross Profit"] == D("5500.00")
    assert totals["Total Expenses"] == D("1500.75")
    assert totals["Net Operating Income"] == D("3999.25")
    assert totals["Total Other Income"] == D("50.00")
    assert totals["Total Other Expenses"] == D("25.00")
    assert totals["Net Income"] == D("4024.25")


def test_pnl_parent_rolls_up_children(session, books):
    chk = "Business Checking"
    books.post(date(2026, 3, 31), "Payroll Expenses:Wages", chk, "1000.00")
    books.post(date(2026, 3, 31), "Payroll Expenses:Taxes", chk, "76.50")
    books.post(date(2026, 3, 31), "Payroll Expenses", chk, "10.00")
    pnl = ReportService(session).profit_and_loss(YEAR)
    rows = [(r.label, r.depth, r.amounts) for r in pnl.rows]
    start = rows.index(("Payroll Expenses", 1, [D("1086.50")]))
    assert rows[start + 1 : start + 3] == [
        ("Taxes", 2, [D("76.50")]),  # unnumbered siblings sort by name
        ("Wages", 2, [D("1000.00")]),
    ]
    # Parents are counted once: the section total does not double the children.
    assert _amounts(pnl)["Total Expenses"] == [D("2587.25")]
    assert _amounts(pnl)["Net Income"] == [D("3912.75")]


def test_pnl_hides_zero_accounts(session, books):
    year = ReportService(session).profit_and_loss(YEAR)
    labels = {r.label for r in year.rows}
    assert "Wages" not in labels
    assert "Payroll Expenses" not in labels
    assert "Travel" in labels  # zero, but it had activity in the period
    march = ReportService(session).profit_and_loss(
        Period(date(2026, 3, 1), date(2026, 3, 31), "March")
    )
    assert [(r.kind, r.label, r.amounts) for r in march.rows] == [
        ("total", "Gross Profit", [ZERO]),
        ("total", "Net Operating Income", [ZERO]),
        ("total", "Net Income", [ZERO]),
    ]


# ----------------------------------------------------------------------
# Balance Sheet
# ----------------------------------------------------------------------


def test_balance_sheet_balances_with_current_year_earnings(session, books):
    bs = ReportService(session).balance_sheet(date(2026, 12, 31))
    assert bs.title == "Balance Sheet"
    assert [(c.label, c.start, c.end) for c in bs.columns] == [
        ("As of 2026-12-31", date(2026, 1, 1), date(2026, 12, 31))
    ]
    assert [(r.kind, r.label, r.depth, r.amounts) for r in bs.rows] == [
        ("section", "Assets", 0, []),
        ("account", "Business Checking", 1, [D("14999.25")]),
        ("total", "Total Assets", 0, [D("14999.25")]),
        ("section", "Liabilities", 0, []),
        ("subtotal", "Total Liabilities", 0, [ZERO]),
        ("section", "Equity", 0, []),
        ("account", "Opening Balance Equity", 1, [D("10000.00")]),
        ("account", "Net Income", 1, [D("4999.25")]),
        ("subtotal", "Total Equity", 0, [D("14999.25")]),
        ("total", "Liabilities + Equity", 0, [D("14999.25")]),
    ]
    assert _row(bs, "Net Income").account_id is None
    assert all(i.ok for i in bs.invariants)


def test_balance_sheet_year_two_includes_unclosed_prior_year_earnings(session, prior_year):
    bs = ReportService(session).balance_sheet(date(2026, 6, 30))
    amounts = _amounts(bs)
    assert amounts["Business Checking"] == [D("17499.25")]
    assert amounts["Prior Years Earnings"] == [D("2500.00")]
    assert amounts["Net Income"] == [D("4999.25")]
    assert amounts["Total Equity"] == [D("17499.25")]
    assert amounts["Liabilities + Equity"] == amounts["Total Assets"] == [D("17499.25")]
    assert _row(bs, "Prior Years Earnings").account_id is None
    assert all(i.ok for i in bs.invariants)


def _close_2025(books) -> None:
    """Post the 2025 year-end close of ``prior_year``: net income 2,500.00 to Retained Earnings."""
    chart = books.chart
    books.posting.post(
        EntryDraft(
            entry_date=date(2025, 12, 31),
            memo="Close 2025",
            source_type=SourceType.YEAR_END_CLOSE,
            lines=[
                LineDraft(chart["Services Revenue"].id, debit=D("3000.00")),
                LineDraft(chart["Office Expenses"].id, credit=D("500.00")),
                LineDraft(chart["Retained Earnings"].id, credit=D("2500.00")),
            ],
        ),
        actor=books.owner,
    )


def test_balance_sheet_balances_after_year_end_close_entry(session, prior_year):
    _close_2025(prior_year)
    bs = ReportService(session).balance_sheet(date(2026, 6, 30))
    amounts = _amounts(bs)
    assert "Prior Years Earnings" not in amounts  # nets to zero once closed
    assert amounts["Retained Earnings"] == [D("2500.00")]
    assert amounts["Net Income"] == [D("4999.25")]
    assert amounts["Total Equity"] == [D("17499.25")]
    assert amounts["Total Assets"] == amounts["Liabilities + Equity"] == [D("17499.25")]
    assert all(i.ok for i in bs.invariants)


def test_contra_asset_shows_negative(session, books, extra_accounts):
    books.post(date(2026, 3, 1), "Equipment", "Business Checking", "5000.00")
    books.post(date(2026, 12, 31), "Other Expenses", "Accumulated Depreciation", "1000.00")
    bs = ReportService(session).balance_sheet(date(2026, 12, 31))
    amounts = _amounts(bs)
    assert amounts["Equipment"] == [D("5000.00")]
    assert amounts["Accumulated Depreciation"] == [D("-1000.00")]
    assert amounts["Business Checking"] == [D("9999.25")]
    assert amounts["Total Assets"] == [D("13999.25")]
    assert amounts["Net Income"] == [D("3999.25")]
    assert amounts["Liabilities + Equity"] == [D("13999.25")]
    assert all(i.ok for i in bs.invariants)


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------


def test_compare_previous_period_and_year_columns_and_pct(session, books):
    service = ReportService(session)
    pnl = service.profit_and_loss(FEB, compare="previous_period")
    assert [(c.label, c.start, c.end) for c in pnl.columns] == [
        ("2026-02-01 to 2026-02-28", date(2026, 2, 1), date(2026, 2, 28)),
        ("2026-01-01 to 2026-01-31", date(2026, 1, 1), date(2026, 1, 31)),
    ]
    rows = {r.label: (r.amounts, r.change, r.pct_change) for r in pnl.rows if r.amounts}
    assert rows["Services Revenue"] == ([D("2500.00"), D("4000.00")], D("-1500.00"), D("-37.5"))
    assert rows["Software and Subscriptions"] == (
        [ZERO, D("1200.50")],
        D("-1200.50"),
        D("-100.0"),
    )
    assert rows["Office Expenses"] == ([D("300.25"), ZERO], D("300.25"), None)
    assert rows["Travel"] == ([ZERO, ZERO], ZERO, None)
    # -599.75 / 2,799.50 = -21.42...% -> -21.4
    assert rows["Net Income"] == ([D("2199.75"), D("2799.50")], D("-599.75"), D("-21.4"))
    assert _row(pnl, "Income").change is None

    prior = service.profit_and_loss(FEB, compare="previous_year")
    assert prior.columns[1].label == "2025-02-01 to 2025-02-28"
    net = _row(prior, "Net Income")
    assert (net.amounts, net.change, net.pct_change) == ([D("2199.75"), ZERO], D("2199.75"), None)

    bs = service.balance_sheet(date(2026, 2, 28), compare="previous_period")
    assert [c.label for c in bs.columns] == ["As of 2026-02-28", "As of 2026-01-31"]
    chk = _row(bs, "Business Checking")
    assert (chk.amounts, chk.change, chk.pct_change) == (
        [D("14999.25"), D("12799.50")],
        D("2199.75"),
        D("17.2"),  # 2,199.75 / 12,799.50 = 17.186...%
    )
    bs_year = service.balance_sheet(date(2026, 2, 28), compare="previous_year")
    assert [c.label for c in bs_year.columns] == ["As of 2026-02-28", "As of 2025-02-28"]
    assert _row(bs_year, "Business Checking").pct_change is None
    assert all(i.ok for i in bs_year.invariants)


def test_pct_change_rounds_half_up_to_one_decimal():
    assert pct_change(D("1.00"), D("16.00")) == D("6.3")  # 6.25 -> 6.3 (not banker's 6.2)
    assert pct_change(D("-1.00"), D("16.00")) == D("-6.3")
    assert pct_change(D("1.00"), D("-16.00")) == D("6.3")  # divides by |compare|
    assert pct_change(D("5.00"), ZERO) is None
    assert isinstance(pct_change(D("1.00"), D("3.00")), Decimal)


def test_unknown_compare_is_rejected(session, books):
    with pytest.raises(InvalidPeriodError):
        ReportService(session).profit_and_loss(FEB, compare="next_year")
    with pytest.raises(InvalidPeriodError):
        ReportService(session).balance_sheet(date(2026, 2, 28), compare="next_year")


def test_balance_sheet_previous_period_mid_month_and_leap_day(session, books):
    service = ReportService(session)
    assert service.balance_sheet(date(2026, 3, 15), compare="previous_period").columns[1].end == (
        date(2026, 2, 15)
    )
    assert service.balance_sheet(date(2028, 3, 31), compare="previous_period").columns[1].end == (
        date(2028, 2, 29)
    )
    assert service.balance_sheet(date(2028, 2, 29), compare="previous_year").columns[1].end == (
        date(2027, 2, 28)
    )


# ----------------------------------------------------------------------
# Empty ledger, basis, invariants
# ----------------------------------------------------------------------


def test_empty_ledger_statements_have_zero_totals_and_ok_invariants(session, chart):
    service = ReportService(session)
    pnl = service.profit_and_loss(YEAR, compare="previous_year")
    assert [(r.label, r.amounts, r.change, r.pct_change) for r in pnl.rows] == [
        ("Gross Profit", [ZERO, ZERO], ZERO, None),
        ("Net Operating Income", [ZERO, ZERO], ZERO, None),
        ("Net Income", [ZERO, ZERO], ZERO, None),
    ]
    bs = service.balance_sheet(date(2026, 12, 31))
    assert [(r.label, r.amounts) for r in bs.rows] == [
        ("Assets", []),
        ("Total Assets", [ZERO]),
        ("Liabilities", []),
        ("Total Liabilities", [ZERO]),
        ("Equity", []),
        ("Total Equity", [ZERO]),
        ("Liabilities + Equity", [ZERO]),
    ]
    for statement in (pnl, bs):
        assert _invariants(statement) == {
            "assets_equal_liabilities_plus_equity": True,
            "trial_balance_balanced": True,
        }


def test_basis_default_is_cash_and_cash_equals_accrual_without_invoices(session, books):
    service = ReportService(session)
    cash = service.profit_and_loss(YEAR)
    accrual = service.profit_and_loss(YEAR, basis=Basis.ACCRUAL)
    assert cash.basis == Basis.CASH
    assert accrual.basis == Basis.ACCRUAL
    assert cash.rows == accrual.rows
    assert cash.basis_note == accrual.basis_note == BASIS_NOTE
    assert BASIS_NOTE.startswith("Cash basis recognizes income when an invoice is paid.")
    bs_cash = service.balance_sheet(date(2026, 12, 31))
    bs_accrual = service.balance_sheet(date(2026, 12, 31), basis=Basis.ACCRUAL)
    assert bs_cash.basis == Basis.CASH
    assert bs_cash.rows == bs_accrual.rows


def test_cash_basis_narrows_statements_but_not_ledger_reports(session, books):
    """Invoice-sourced lines leave the cash statements; ledger-as-is reports keep them."""
    service = ReportService(session)
    chk = books.chart["Business Checking"].id
    ar = books.chart["Accounts Receivable"].id
    end = date(2026, 12, 31)
    books.post(
        date(2026, 1, 25),
        "Accounts Receivable",
        "Services Revenue",
        "700.00",
        "Billed",
        SourceType.INVOICE,
    )
    assert _amounts(service.profit_and_loss(YEAR))["Net Income"] == [D("4999.25")]
    assert _amounts(service.profit_and_loss(YEAR, basis=Basis.ACCRUAL))["Net Income"] == [
        D("5699.25")
    ]
    cash_bs = service.balance_sheet(end)
    accrual_bs = service.balance_sheet(end, basis=Basis.ACCRUAL)
    assert "Accounts Receivable" not in _amounts(cash_bs)
    assert _amounts(accrual_bs)["Accounts Receivable"] == [D("700.00")]
    assert _amounts(cash_bs)["Business Checking"] == [D("14999.25")]
    tb = {r.account_id: (r.debit, r.credit) for r in service.trial_balance(end).rows}
    assert tb[ar] == (D("700.00"), ZERO)
    assert service.account_register(ar, YEAR.start, YEAR.end).closing_balance == D("700.00")
    assert service.account_register(chk, YEAR.start, YEAR.end).closing_balance == D("14999.25")
    ledger_accounts = {r.account_id for r in service.general_ledger(YEAR.start, YEAR.end).accounts}
    assert ar in ledger_accounts
    assert "invoice" in {e.source_type for e in service.journal(YEAR.start, YEAR.end)}
    # The trial-balance invariant reads the ledger as posted on both bases.
    assert cash_bs.invariants[1] == accrual_bs.invariants[1]
    assert cash_bs.invariants[1].name == "trial_balance_balanced"
    assert cash_bs.invariants[1].detail == "As of 2026-12-31: debits 18900.73 = credits 18900.73"
    assert [i.ok for i in cash_bs.invariants] == [True, True]


def test_invariant_failure_is_reported_not_raised(session, books, monkeypatch):
    real_account_totals = queries.account_totals

    def drop_equity(session_, **window):
        return [t for t in real_account_totals(session_, **window) if t.account.type != "equity"]

    monkeypatch.setattr(queries, "account_totals", drop_equity)
    monkeypatch.setattr(queries, "debit_credit_totals", lambda *a, **k: (D("100.00"), ZERO))
    bs = ReportService(session).balance_sheet(date(2026, 12, 31))
    assert _amounts(bs)["Total Assets"] == [D("14999.25")]
    assert _amounts(bs)["Liabilities + Equity"] == [D("4999.25")]
    failed = {i.name: i for i in bs.invariants}
    assert failed["assets_equal_liabilities_plus_equity"].ok is False
    assert "10000.00" in failed["assets_equal_liabilities_plus_equity"].detail
    assert failed["trial_balance_balanced"].ok is False
    assert "2026-12-31" in failed["trial_balance_balanced"].detail
    pnl = ReportService(session).profit_and_loss(YEAR)
    assert _invariants(pnl)["trial_balance_balanced"] is False


# ----------------------------------------------------------------------
# Year-end close
# ----------------------------------------------------------------------


def test_pnl_for_a_closed_year_ignores_the_close_entry(session, prior_year):
    _close_2025(prior_year)
    service = ReportService(session)
    pnl = service.profit_and_loss(Period(date(2025, 1, 1), date(2025, 12, 31), "Last year"))
    amounts = _amounts(pnl)
    assert amounts["Services Revenue"] == [D("3000.00")]
    assert amounts["Office Expenses"] == [D("500.00")]
    assert amounts["Net Income"] == [D("2500.00")]
    assert all(i.ok for i in pnl.invariants)
    bs = service.balance_sheet(date(2025, 12, 31))
    bs_amounts = _amounts(bs)
    assert bs_amounts["Retained Earnings"] == [D("2500.00")]
    assert "Net Income" not in bs_amounts  # closed into Retained Earnings on the same day
    # Business Checking 3,000.00 - 500.00 (the opening balance is dated 2026)
    assert bs_amounts["Total Assets"] == bs_amounts["Liabilities + Equity"] == [D("2500.00")]
    assert all(i.ok for i in bs.invariants)


def test_pnl_compare_previous_year_shows_the_closed_years_figures(session, prior_year):
    _close_2025(prior_year)
    pnl = ReportService(session).profit_and_loss(YEAR, compare="previous_year")
    assert pnl.columns[1].label == "2025-01-01 to 2025-12-31"
    rows = {r.label: (r.amounts, r.change, r.pct_change) for r in pnl.rows if r.amounts}
    assert rows["Services Revenue"] == ([D("6500.00"), D("3000.00")], D("3500.00"), D("116.7"))
    assert rows["Office Expenses"] == ([D("300.25"), D("500.00")], D("-199.75"), D("-40.0"))
    assert rows["Net Income"] == ([D("4999.25"), D("2500.00")], D("2499.25"), D("100.0"))
