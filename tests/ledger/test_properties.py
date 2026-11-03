"""Property tests (Hypothesis) for the ledger and reports, against the real Postgres.

Isolation: Hypothesis re-runs a test body many times with the same function-scoped
fixtures. The accounts are created once (the shared ``session`` fixture rolls them
back at the end of the test); every example then builds its entries inside a
SAVEPOINT (``session.begin_nested()``) that is always rolled back, so no data is
left between examples. Posting only flushes, never commits, so the savepoint holds.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlmodel import func, select

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import (
    Account,
    AccountType,
    JournalEntry,
    SourceType,
    SystemRole,
)
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.reports import queries
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService

SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
START = date(2025, 1, 1)
END = date(2026, 12, 31)
D2 = Decimal("0.01")
D0 = Decimal("0.00")

# A line is (account index, side, cents); an entry is (day offset from START, lines).
line_st = st.tuples(
    st.integers(0, 10_000),
    st.sampled_from(["debit", "credit"]),
    st.integers(1, 5_000_000),
)
entry_st = st.tuples(
    st.integers(0, (END - START).days),
    st.lists(line_st, min_size=1, max_size=4),
)
entries_st = st.lists(entry_st, min_size=1, max_size=8)


@pytest.fixture
def accounts(session, owner_user, chart) -> list[Account]:
    """The default chart plus one account of each type the default chart lacks."""
    service = AccountService(session)
    for name, type_, number in [
        ("Materials", AccountType.COGS, "5000"),
        ("Interest Income", AccountType.OTHER_INCOME, "7000"),
        ("Penalties", AccountType.OTHER_EXPENSE, "8000"),
    ]:
        chart[name] = service.create(actor=owner_user, name=name, type=type_, number=number)
    session.commit()
    result = list(chart.values())
    assert {a.type for a in result} == set(AccountType)
    return result


@contextmanager
def fresh_state(session) -> Iterator[None]:
    """One Hypothesis example's worth of data: always rolled back."""
    nested = session.begin_nested()
    try:
        yield
    finally:
        if nested.is_active:
            nested.rollback()
        # Nothing may survive an example (the accounts are the only seeded data).
        assert session.exec(select(func.count()).select_from(JournalEntry)).one() == 0


def _cents(value: int) -> Decimal:
    return (Decimal(value) * D2).quantize(D2)


def _draft(accounts: list[Account], raw, source=SourceType.MANUAL) -> EntryDraft:
    """Balance the random lines with one extra line so the entry always posts."""
    offset, raw_lines = raw
    lines = []
    net = 0
    for index, side, cents in raw_lines:
        account = accounts[index % len(accounts)]
        amount = _cents(cents)
        lines.append(
            LineDraft(account.id, debit=amount)
            if side == "debit"
            else LineDraft(account.id, credit=amount)
        )
        net += cents if side == "debit" else -cents
    balancing = accounts[(raw_lines[0][0] + 1) % len(accounts)]
    if net > 0:
        lines.append(LineDraft(balancing.id, credit=_cents(net)))
    elif net < 0:
        lines.append(LineDraft(balancing.id, debit=_cents(-net)))
    # net == 0: the random lines already balance (and there are at least two of them)
    return EntryDraft(START + timedelta(days=offset), lines, source_type=source)


def _post_all(session, owner, accounts, raws) -> list:
    posting = LedgerPostingService(session)
    return [posting.post(_draft(accounts, raw), actor=owner) for raw in raws]


def _snapshot(session, as_of: date = END):
    """Everything the reports say about the whole ledger, as comparable plain values."""
    reports = ReportService(session)
    trial = reports.trial_balance(as_of)
    year_2025 = reports.profit_and_loss(Period(START, date(2025, 12, 31), "2025"))
    year_2026 = reports.profit_and_loss(Period(date(2026, 1, 1), END, "2026"))
    sheet = reports.balance_sheet(as_of)
    return (
        [(r.account_id, r.debit, r.credit) for r in trial.rows],
        [(r.kind, r.label, tuple(r.amounts)) for r in year_2025.rows],
        [(r.kind, r.label, tuple(r.amounts)) for r in year_2026.rows],
        [(r.kind, r.label, tuple(r.amounts)) for r in sheet.rows],
    )


@SETTINGS
@given(raws=entries_st, as_of_offsets=st.lists(st.integers(0, (END - START).days), max_size=4))
def test_random_valid_entries_keep_trial_balance_balanced(
    session, owner_user, accounts, raws, as_of_offsets
):
    with fresh_state(session):
        _post_all(session, owner_user, accounts, raws)
        reports = ReportService(session)
        for as_of in [END, *(START + timedelta(days=o) for o in as_of_offsets)]:
            trial = reports.trial_balance(as_of)
            assert trial.balanced
            assert trial.total_debit == trial.total_credit
        debit, credit = queries.debit_credit_totals(
            session, on_or_before=END, basis=queries.LEDGER_BASIS
        )
        assert debit == credit
        posted = sum((line.debit for raw in raws for line in _draft(accounts, raw).lines), D0)
        assert debit == posted


def _close_year(session, owner, accounts, year: int) -> None:
    """Post a year-end close: zero the year's income-statement accounts into Retained Earnings."""
    retained = AccountService(session).account_by_role(SystemRole.RETAINED_EARNINGS)
    lines = []
    net = Decimal("0")  # credit - debit of the income statement accounts
    for total in queries.account_totals(
        session,
        start=date(year, 1, 1),
        end=date(year, 12, 31),
        basis=queries.LEDGER_BASIS,
        account_types=queries.INCOME_STATEMENT_TYPES,
        exclude_sources=queries.PNL_EXCLUDED_SOURCES,
    ):
        balance = total.debit - total.credit
        if balance > 0:
            lines.append(LineDraft(total.account.id, credit=balance))
        elif balance < 0:
            lines.append(LineDraft(total.account.id, debit=-balance))
        net -= balance
    if not lines:
        return
    if net > 0:
        lines.append(LineDraft(retained.id, credit=net))
    elif net < 0:
        lines.append(LineDraft(retained.id, debit=-net))
    LedgerPostingService(session).post(
        EntryDraft(date(year, 12, 31), lines, memo="Close", source_type=SourceType.YEAR_END_CLOSE),
        actor=owner,
    )


@SETTINGS
@given(raws=entries_st, as_of_offsets=st.lists(st.integers(0, (END - START).days), max_size=4))
def test_assets_equal_liabilities_plus_equity_for_any_valid_sequence(
    session, owner_user, accounts, raws, as_of_offsets
):
    """Every account type, dates over two years, no close entry."""
    with fresh_state(session):
        _post_all(session, owner_user, accounts, raws)
        reports = ReportService(session)
        for as_of in [END, *(START + timedelta(days=o) for o in as_of_offsets)]:
            sheet = reports.balance_sheet(as_of)
            assert all(i.ok for i in sheet.invariants), sheet.invariants
            totals = {r.label: r.amounts[0] for r in sheet.rows if r.kind == "total"}
            assert totals["Total Assets"] == totals["Liabilities + Equity"]


@SETTINGS
@given(raws=entries_st, later=entries_st, as_of_offset=st.integers(0, (END - START).days))
def test_assets_equal_liabilities_plus_equity_with_a_year_end_close(
    session, owner_user, accounts, raws, later, as_of_offset
):
    """The same equation holds when 2025 is closed into retained earnings."""
    with fresh_state(session):
        # Keep the first batch inside 2025 and the second inside 2026.
        in_2025 = [(o % 365, lines) for o, lines in raws]
        in_2026 = [(365 + o % 365, lines) for o, lines in later]
        _post_all(session, owner_user, accounts, in_2025)
        reports = ReportService(session)
        before = reports.profit_and_loss(Period(START, date(2025, 12, 31), "2025"))
        _close_year(session, owner_user, accounts, 2025)
        after = reports.profit_and_loss(Period(START, date(2025, 12, 31), "2025"))
        assert [(r.label, r.amounts) for r in after.rows] == [
            (r.label, r.amounts) for r in before.rows
        ]  # the close is not that year's activity
        _post_all(session, owner_user, accounts, in_2026)
        for as_of in (date(2025, 12, 31), START + timedelta(days=as_of_offset), END):
            sheet = reports.balance_sheet(as_of)
            assert all(i.ok for i in sheet.invariants), (as_of, sheet.invariants)


@SETTINGS
@given(raw=entry_st, gap=st.integers(0, 60))
def test_reverse_then_original_nets_to_zero(session, owner_user, accounts, raw, gap):
    with fresh_state(session):
        posting = LedgerPostingService(session)
        original = posting.post(_draft(accounts, raw), actor=owner_user)
        reversal = posting.reverse(
            original.id, entry_date=original.entry_date + timedelta(days=gap), actor=owner_user
        )
        assert reversal.id != original.id
        far = END + timedelta(days=100)
        totals = queries.account_totals(session, on_or_before=far, basis=queries.LEDGER_BASIS)
        assert totals  # the accounts were touched...
        assert all(t.debit == t.credit for t in totals)  # ...and every one nets to zero
        trial = ReportService(session).trial_balance(far)
        assert trial.rows == []
        assert trial.total_debit == trial.total_credit == Decimal("0.00")


@SETTINGS
@given(raw=entry_st)
def test_void_and_repost_equals_single_post_in_reports(session, owner_user, accounts, raw):
    posting = LedgerPostingService(session)
    draft = _draft(accounts, raw)
    with fresh_state(session):
        posting.post(draft, actor=owner_user)
        single = _snapshot(session)
    with fresh_state(session):
        first = posting.post(draft, actor=owner_user)
        posting.void(first.id, entry_date=first.entry_date, actor=owner_user)
        posting.post(draft, actor=owner_user)
        reposted = _snapshot(session)
    assert reposted == single


@SETTINGS
@given(raws=st.lists(entry_st, min_size=2, max_size=8), data=st.data())
def test_report_totals_independent_of_posting_order(session, owner_user, accounts, raws, data):
    shuffled = data.draw(st.permutations(raws))
    with fresh_state(session):
        _post_all(session, owner_user, accounts, raws)
        in_order = _snapshot(session)
    with fresh_state(session):
        _post_all(session, owner_user, accounts, shuffled)
        reordered = _snapshot(session)
    assert reordered == in_order
