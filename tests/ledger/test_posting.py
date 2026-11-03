import re
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select
from sqlmodel import text as sql_text

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.ledger.accounts import AccountService
from payroll.ledger.exceptions import (
    AlreadyReversedError,
    EntryNotFoundError,
    InactiveAccountError,
    InvalidEntryError,
    PeriodLockedError,
    UnbalancedEntryError,
)
from payroll.ledger.models import EntryCounter, JournalEntry, JournalLine, SourceType
from payroll.ledger.services import EntryDraft, JournalService, LedgerPostingService, LineDraft

D = Decimal
JAN = date(2026, 1, 15)


def _ids(chart):
    return chart["Business Checking"].id, chart["Services Revenue"].id


def _draft(chart, amount="100.00", *, entry_date=JAN, **kw) -> EntryDraft:
    cash, sales = _ids(chart)
    return EntryDraft(
        entry_date=entry_date,
        lines=[LineDraft(cash, debit=D(amount)), LineDraft(sales, credit=D(amount))],
        **kw,
    )


def _check_db(session) -> None:
    """Fire the deferred balance triggers now (the test session never commits)."""
    session.execute(sql_text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.execute(sql_text("SET CONSTRAINTS ALL DEFERRED"))


@contextmanager
def _raises(exc_type, pattern: str):
    """``pytest.raises`` matching ``AppBaseError.message`` (their ``str()`` is empty)."""
    with pytest.raises(exc_type) as info:
        yield info
    assert re.search(pattern, info.value.message), info.value.message


def _set_lock(session, lock_date: date | None) -> None:
    session.execute(
        sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": lock_date}
    )


def _lines(session, entry: JournalEntry) -> list[JournalLine]:
    stmt = select(JournalLine).where(JournalLine.entry_id == entry.id).order_by(JournalLine.line_no)
    return list(session.exec(stmt).all())


def test_post_balanced_entry_assigns_entry_no_1_then_2(session, owner_user, chart):
    svc = LedgerPostingService(session)
    first = svc.post(_draft(chart, memo="first"), actor=owner_user)
    second = svc.post(_draft(chart, "5.5"), actor=owner_user)
    _check_db(session)
    assert (first.entry_no, second.entry_no) == (1, 2)
    assert first.source_type == SourceType.MANUAL
    assert first.status == "posted"
    assert first.posted_by == owner_user.id
    assert first.memo == "first"
    lines = _lines(session, second)
    assert [(ln.line_no, ln.debit, ln.credit) for ln in lines] == [
        (1, D("5.50"), D("0.00")),
        (2, D("0.00"), D("5.50")),
    ]
    cash, sales = _ids(chart)
    balances = AccountService(session).balances()
    assert balances[cash] == D("105.50")
    assert balances[sales] == D("-105.50")


def test_numbers_are_per_source_type(session, owner_user, chart):
    svc = LedgerPostingService(session)
    manual = svc.post(_draft(chart), actor=owner_user)
    opening = svc.post(
        _draft(chart, source_type=SourceType.OPENING_BALANCE, source_id="ob-1"), actor=owner_user
    )
    _check_db(session)
    assert (manual.entry_no, opening.entry_no) == (1, 1)
    assert opening.source_type == SourceType.OPENING_BALANCE
    assert opening.source_id == "ob-1"
    assert svc.post(_draft(chart), actor=owner_user).entry_no == 2


def test_failed_post_consumes_no_number(session, owner_user, chart):
    svc = LedgerPostingService(session)
    cash, sales = _ids(chart)
    bad = EntryDraft(JAN, [LineDraft(cash, debit=D("10")), LineDraft(sales, credit=D("9"))])
    with pytest.raises(UnbalancedEntryError):
        svc.post(bad, actor=owner_user)
    assert session.get(EntryCounter, SourceType.MANUAL) is None
    assert svc.post(_draft(chart), actor=owner_user).entry_no == 1


def test_rejects_three_decimal_amount(session, owner_user, chart):
    with _raises(InvalidEntryError, "2 decimal places"):
        LedgerPostingService(session).post(_draft(chart, "10.005"), actor=owner_user)


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_rejects_nan_and_infinity(session, owner_user, chart, amount):
    with _raises(InvalidEntryError, "finite"):
        LedgerPostingService(session).post(_draft(chart, amount), actor=owner_user)


def test_rejects_negative_and_zero(session, owner_user, chart):
    svc = LedgerPostingService(session)
    cash, sales = _ids(chart)
    negative = EntryDraft(JAN, [LineDraft(cash, debit=D("-5")), LineDraft(sales, credit=D("-5"))])
    with _raises(InvalidEntryError, "negative"):
        svc.post(negative, actor=owner_user)
    zero = EntryDraft(JAN, [LineDraft(cash), LineDraft(sales)])
    with _raises(InvalidEntryError, "exactly one"):
        svc.post(zero, actor=owner_user)


def test_rejects_non_decimal_amount(session, owner_user, chart):
    cash, sales = _ids(chart)
    draft = EntryDraft(JAN, [LineDraft(cash, debit=10.0), LineDraft(sales, credit=D("10"))])  # type: ignore[arg-type]
    with _raises(InvalidEntryError, "Decimal"):
        LedgerPostingService(session).post(draft, actor=owner_user)


def test_rejects_both_sides_on_one_line(session, owner_user, chart):
    cash, sales = _ids(chart)
    draft = EntryDraft(
        JAN,
        [LineDraft(cash, debit=D("5"), credit=D("5")), LineDraft(sales, credit=D("0.01"))],
    )
    with _raises(InvalidEntryError, "exactly one"):
        LedgerPostingService(session).post(draft, actor=owner_user)


def test_rejects_amount_over_max(session, owner_user, chart):
    svc = LedgerPostingService(session)
    with _raises(InvalidEntryError, "too large"):
        svc.post(_draft(chart, "1e20"), actor=owner_user)
    # the largest NUMERIC(14, 2) value still posts
    assert svc.post(_draft(chart, "999999999999.99"), actor=owner_user).entry_no == 1
    _check_db(session)


def test_rejects_bad_memo_and_source_id_lengths(session, owner_user, chart):
    svc = LedgerPostingService(session)
    with _raises(InvalidEntryError, "memo"):
        svc.post(_draft(chart, memo="x" * 501), actor=owner_user)
    with _raises(InvalidEntryError, "source id"):
        svc.post(_draft(chart, source_id="x" * 101), actor=owner_user)
    cash, sales = _ids(chart)
    long_line = EntryDraft(
        JAN, [LineDraft(cash, debit=D("1"), memo="x" * 501), LineDraft(sales, credit=D("1"))]
    )
    with _raises(InvalidEntryError, "memo"):
        svc.post(long_line, actor=owner_user)


def test_rejects_single_line_and_unbalanced(session, owner_user, chart):
    svc = LedgerPostingService(session)
    cash, sales = _ids(chart)
    with _raises(UnbalancedEntryError, "at least 2 lines"):
        svc.post(EntryDraft(JAN, [LineDraft(cash, debit=D("1"))]), actor=owner_user)
    with _raises(UnbalancedEntryError, "at least 2 lines"):
        svc.post(EntryDraft(JAN, []), actor=owner_user)
    unbalanced = EntryDraft(JAN, [LineDraft(cash, debit=D("10")), LineDraft(sales, credit=D("9"))])
    with _raises(UnbalancedEntryError, r"10\.00.*9\.00"):
        svc.post(unbalanced, actor=owner_user)
    assert session.exec(select(JournalEntry)).first() is None


def test_rejects_inactive_and_unknown_account(session, owner_user, chart):
    svc = LedgerPostingService(session)
    travel = chart["Travel"]
    AccountService(session).set_active(travel.id, False, actor=owner_user)
    cash, _ = _ids(chart)
    inactive = EntryDraft(JAN, [LineDraft(travel.id, debit=D("1")), LineDraft(cash, credit=D("1"))])
    with _raises(InactiveAccountError, "Travel"):
        svc.post(inactive, actor=owner_user)
    unknown = EntryDraft(JAN, [LineDraft(999999, debit=D("1")), LineDraft(cash, credit=D("1"))])
    with _raises(InvalidEntryError, "999999"):
        svc.post(unknown, actor=owner_user)
    assert session.get(EntryCounter, SourceType.MANUAL) is None


def test_rejects_date_on_or_before_lock_date(session, owner_user, chart):
    svc = LedgerPostingService(session)
    _set_lock(session, date(2026, 3, 31))
    for day in (date(2026, 3, 31), date(2026, 1, 1)):
        with _raises(PeriodLockedError, "2026-03-31"):
            svc.post(_draft(chart, entry_date=day), actor=owner_user)
    assert session.get(EntryCounter, SourceType.MANUAL) is None
    entry = svc.post(_draft(chart, entry_date=date(2026, 4, 1)), actor=owner_user)
    assert entry.entry_no == 1
    _check_db(session)


def test_reverse_swaps_sides_and_links(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart, "42.10", source_id="src-9"), actor=owner_user)
    reversal = svc.reverse(original.id, entry_date=date(2026, 2, 1), actor=owner_user)
    _check_db(session)
    assert reversal.reverses_entry_id == original.id
    assert reversal.entry_no == 2
    assert reversal.entry_date == date(2026, 2, 1)
    assert reversal.source_type == SourceType.MANUAL
    assert reversal.source_id == "src-9"
    assert reversal.memo == "Reversal of manual #1"
    session.refresh(original)
    assert original.status == "posted"
    orig_lines = _lines(session, original)
    rev_lines = _lines(session, reversal)
    assert [(ln.account_id, ln.debit, ln.credit) for ln in rev_lines] == [
        (ln.account_id, ln.credit, ln.debit) for ln in orig_lines
    ]
    balances = AccountService(session).balances()
    assert all(balances[ln.account_id] == 0 for ln in orig_lines)


def test_reverse_uses_given_memo(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    reversal = svc.reverse(original.id, entry_date=JAN, actor=owner_user, memo="Oops")
    assert reversal.memo == "Oops"


def test_reverse_twice_rejected(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    svc.reverse(original.id, entry_date=JAN, actor=owner_user)
    with pytest.raises(AlreadyReversedError):
        svc.reverse(original.id, entry_date=JAN, actor=owner_user)
    with pytest.raises(AlreadyReversedError):
        svc.void(original.id, entry_date=JAN, actor=owner_user)
    # the failed attempts consumed no number
    assert svc.post(_draft(chart), actor=owner_user).entry_no == 3


def test_reverse_unknown_entry(session, owner_user, chart):
    with pytest.raises(EntryNotFoundError):
        LedgerPostingService(session).reverse(999999, entry_date=JAN, actor=owner_user)


def test_void_marks_original_void_and_reversal_posts(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    session.commit()  # (releases the savepoint) so the void is a separate step
    reversal = svc.void(original.id, entry_date=date(2026, 1, 20), actor=owner_user, memo="Void")
    _check_db(session)
    session.refresh(original)
    assert original.status == "void"
    assert reversal.status == "posted"
    assert reversal.reverses_entry_id == original.id
    assert reversal.memo == "Void"
    cash, sales = _ids(chart)
    balances = AccountService(session).balances()
    assert balances[cash] == 0 and balances[sales] == 0
    actions = session.exec(select(AuditLog.action).order_by(AuditLog.id)).all()
    assert actions[-2:] == ["ledger.post", "ledger.void"]


def test_void_twice_rejected(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    svc.void(original.id, entry_date=JAN, actor=owner_user)
    with pytest.raises(AlreadyReversedError):
        svc.void(original.id, entry_date=JAN, actor=owner_user)


def test_reverse_into_locked_period_rejected(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart, entry_date=date(2026, 4, 10)), actor=owner_user)
    _set_lock(session, date(2026, 3, 31))
    with pytest.raises(PeriodLockedError):
        svc.reverse(original.id, entry_date=date(2026, 3, 31), actor=owner_user)
    with pytest.raises(PeriodLockedError):
        svc.void(original.id, entry_date=date(2026, 3, 1), actor=owner_user)
    session.refresh(original)
    assert original.status == "posted"
    reversal = svc.reverse(original.id, entry_date=date(2026, 4, 11), actor=owner_user)
    assert reversal.entry_no == 2


def test_reversal_of_reversal_allowed_once(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    reversal = svc.reverse(original.id, entry_date=JAN, actor=owner_user)
    again = svc.reverse(reversal.id, entry_date=JAN, actor=owner_user)
    _check_db(session)
    assert again.reverses_entry_id == reversal.id
    assert again.memo == "Reversal of manual #2"
    assert [(ln.debit, ln.credit) for ln in _lines(session, again)] == [
        (ln.debit, ln.credit) for ln in _lines(session, original)
    ]
    with pytest.raises(AlreadyReversedError):
        svc.reverse(reversal.id, entry_date=JAN, actor=owner_user)


def test_post_writes_audit_row(session, owner_user, chart):
    entry = LedgerPostingService(session).post(_draft(chart, "12.34"), actor=owner_user)
    row = session.exec(select(AuditLog).where(AuditLog.action == "ledger.post")).one()
    assert row.user_id == owner_user.id
    assert row.entity == "journal_entry"
    assert row.entity_id == str(entry.id)
    assert row.after == {
        "entry_no": 1,
        "source_type": "manual",
        "source_id": None,
        "entry_date": "2026-01-15",
        "total_debit": "12.34",
        "total_credit": "12.34",
        "line_count": 2,
        "reverses_entry_id": None,
    }


def test_manual_create_requires_owner(session, owner_user, accountant_user, viewer_user, chart):
    journals = JournalService(session)
    for user in (accountant_user, viewer_user):
        with pytest.raises(PermissionDeniedError):
            journals.create_manual(_draft(chart), actor=user)
    entry = journals.create_manual(_draft(chart), actor=owner_user)
    assert entry.entry_no == 1
    for user in (accountant_user, viewer_user):
        with pytest.raises(PermissionDeniedError):
            journals.reverse_manual(entry.id, entry_date=JAN, actor=user)
        with pytest.raises(PermissionDeniedError):
            journals.void_manual(entry.id, entry_date=JAN, actor=user)


def test_manual_service_only_handles_manual_entries(session, owner_user, chart):
    journals = JournalService(session)
    with _raises(InvalidEntryError, "manual"):
        journals.create_manual(_draft(chart, source_type=SourceType.INVOICE), actor=owner_user)
    invoice_entry = LedgerPostingService(session).post(
        _draft(chart, source_type=SourceType.INVOICE), actor=owner_user
    )
    with _raises(InvalidEntryError, "invoice"):
        journals.reverse_manual(invoice_entry.id, entry_date=JAN, actor=owner_user)
    with _raises(InvalidEntryError, "invoice"):
        journals.void_manual(invoice_entry.id, entry_date=JAN, actor=owner_user)


def test_manual_wrappers_commit_and_read_back(session, owner_user, chart):
    journals = JournalService(session)
    first = journals.create_manual(_draft(chart, entry_date=date(2026, 1, 5)), actor=owner_user)
    second = journals.create_manual(_draft(chart, entry_date=date(2026, 2, 5)), actor=owner_user)
    voided = journals.void_manual(second.id, entry_date=date(2026, 2, 6), actor=owner_user)
    reversed_ = journals.reverse_manual(first.id, entry_date=date(2026, 3, 1), actor=owner_user)
    LedgerPostingService(session).post(
        _draft(chart, source_type=SourceType.OPENING_BALANCE), actor=owner_user
    )
    assert journals.get(second.id).status == "void"
    assert voided.reverses_entry_id == second.id
    assert reversed_.reverses_entry_id == first.id
    with pytest.raises(EntryNotFoundError):
        journals.get(999999)
    all_dates = [e.entry_date for e in journals.list()]
    assert all_dates == sorted(all_dates, reverse=True)
    assert len(all_dates) == 5
    feb = journals.list(start=date(2026, 2, 1), end=date(2026, 2, 28))
    assert {e.id for e in feb} == {second.id, voided.id}
    manual = journals.list(source_type=SourceType.MANUAL)
    assert len(manual) == 4
    assert len(journals.list(limit=2)) == 2
    assert len(journals.list(limit=2, offset=4)) == 1


def test_rejects_unknown_source_type(session, owner_user, chart):
    with _raises(InvalidEntryError, "bogus"):
        LedgerPostingService(session).post(_draft(chart, source_type="bogus"), actor=owner_user)


def test_voiding_the_reversal_of_a_void_is_refused(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    reversal = svc.void(original.id, entry_date=JAN, actor=owner_user)
    for action in (svc.void, svc.reverse):
        with pytest.raises(AlreadyReversedError) as info:
            action(reversal.id, entry_date=JAN, actor=owner_user)
        assert "reversal created by voiding" in info.value.message
    session.refresh(original)
    assert original.status == "void"


def test_void_of_entry_in_locked_period_is_refused_but_reverse_works(session, owner_user, chart):
    svc = LedgerPostingService(session)
    original = svc.post(_draft(chart), actor=owner_user)
    session.execute(sql_text("UPDATE period_lock SET lock_date = '2026-01-31' WHERE id = 1"))
    with pytest.raises(PeriodLockedError):
        svc.void(original.id, entry_date=date(2026, 2, 5), actor=owner_user)
    session.refresh(original)
    assert original.status == "posted"
    reversal = svc.reverse(original.id, entry_date=date(2026, 2, 5), actor=owner_user)
    assert reversal.reverses_entry_id == original.id
