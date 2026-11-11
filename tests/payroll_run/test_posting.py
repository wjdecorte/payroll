"""Approving a draft run posts one journal entry; voiding an approved run reverses it.

The entry is asserted line by line against the run's STORED figures (never recalculated)
and the exact accounts, over the health x HSA x net-pay-destination matrix.
"""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.accounts import PAYROLL_ROLES, AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import (
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
    SystemRole,
)
from payroll.payroll_run.exceptions import (
    InvalidPayrollRunError,
    PayrollRoleNotBoundError,
    PayrollRunNotFoundError,
    PayrollRunStateError,
)
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.posting import PayrollPostingService, PostingAccounts, build_lines
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
ZERO = D("0.00")
JAN_END = date(2026, 1, 31)
FEB_END = date(2026, 2, 28)

FIGURES = (
    "gross_salary health_insurance hsa_contribution fica_wages income_tax_wages ss_ee ss_er "
    "medicare_ee additional_medicare total_medicare_ee medicare_er federal_withholding "
    "ga_withholding total_employee_deductions net_pay total_employer_taxes total_employer_cost "
    "ytd_fica_before ytd_fica_after futa_wages futa suta_wages suta suta_rate"
).split()


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles, year_settings):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


@pytest.fixture
def posting(session):
    return PayrollPostingService(session)


# --------------------------------------------------------------------------- helpers


def save(session, owner_user, period="2026-01", gross="8000.00", **kw) -> PayrollRun:
    payload = PayrollInput(pay_period=period, gross_salary=gross, save_run=True, **kw)
    result = PayrollRunService(session).calculate(payload, actor=owner_user)
    return session.get(PayrollRun, result.id)


def plant(session, status="imported", period="2025-12", **kw) -> PayrollRun:
    """A run in another status, inserted directly (history, or a void left behind)."""
    row = PayrollRun(
        pay_period=period,
        gross_salary=D("100.00"),
        fica_wages=D("100.00"),
        net_pay=D("100.00"),
        status=status,
        **kw,
    )
    session.add(row)
    session.flush()
    session.commit()
    return row


def approve(posting, owner_user, run, bank=None, day=None, **kw):
    kw.setdefault("net_pay_source", "account" if bank else "owner")
    kw.setdefault("net_pay_account_id", bank.id if bank else None)
    return posting.approve(run.id, actor=owner_user, pay_date=day or last_day(run.pay_period), **kw)


def last_day(period: str) -> date:
    year, month = int(period[:4]), int(period[5:])
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def counts(session) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        session.exec(select(func.count()).select_from(model)).one()
        for model in (JournalEntry, JournalLine, AuditLog, PayrollRun)
    )


@contextmanager
def nothing_written(session):
    """The block must leave no entry, line, audit row or run behind."""
    session.expire_all()
    before = counts(session)
    yield
    session.expire_all()
    assert counts(session) == before


def refused(session, call, error=InvalidPayrollRunError, fragment=None):
    with nothing_written(session):
        with pytest.raises(error) as caught:
            call()
    if fragment:
        assert fragment in caught.value.message
    return caught.value


def entry_lines(session, entry_id):
    rows = session.exec(
        select(JournalLine).where(JournalLine.entry_id == entry_id).order_by(JournalLine.line_no)
    ).all()
    return [(r.account_id, r.debit, r.credit) for r in rows]


def expected_lines(run, roles, net_id):
    """Spec section 6, computed here from the stored figures (not by the code under test)."""
    health_in = run.health_in_income_tax and run.health_insurance > 0
    wages = run.gross_salary + (run.health_insurance if health_in else ZERO) + run.hsa_contribution
    federal = (
        run.federal_withholding + run.ss_ee + run.total_medicare_ee + run.ss_er + run.medicare_er
    )
    rows = [
        (roles[SystemRole.PAYROLL_WAGES].id, wages, ZERO),
        (
            roles[SystemRole.PAYROLL_TAX_EXPENSE].id,
            run.total_employer_taxes + run.futa + run.suta,
            ZERO,
        ),
        (roles[SystemRole.PAYROLL_FEDERAL_PAYABLE].id, ZERO, federal),
        (roles[SystemRole.PAYROLL_GA_WH_PAYABLE].id, ZERO, run.ga_withholding),
        (roles[SystemRole.PAYROLL_FUTA_PAYABLE].id, ZERO, run.futa),
        (roles[SystemRole.PAYROLL_SUTA_PAYABLE].id, ZERO, run.suta),
        (
            roles[SystemRole.PAYROLL_HEALTH_PAYABLE].id,
            ZERO,
            run.health_insurance if health_in else ZERO,
        ),
        (roles[SystemRole.PAYROLL_HSA_PAYABLE].id, ZERO, run.hsa_contribution),
        (net_id, ZERO, run.net_pay),
    ]
    return [row for row in rows if row[1] or row[2]]


def figures(run) -> dict:
    return {name: getattr(run, name) for name in FIGURES}


# --------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("health_in_income_tax", [True, False], ids=["health-in-tax", "health-out"])
@pytest.mark.parametrize("hsa", ["0.00", "125.00"], ids=["no-hsa", "hsa"])
@pytest.mark.parametrize("to_owner", [False, True], ids=["bank", "owner"])
def test_entry_matches_the_stored_figures_line_by_line(
    session, owner_user, posting, payroll_roles, bank, health_in_income_tax, hsa, to_owner
):
    run = save(
        session,
        owner_user,
        gross="8000.00",
        health_insurance="450.00",
        hsa_contribution=hsa,
        health_in_income_tax=health_in_income_tax,
        ytd_fica_wages="5000.00",
    )
    assert run.futa > 0 and run.suta > 0  # the unemployment lines are in play
    stored = figures(run)
    due = AccountService(session).account_by_role(SystemRole.DUE_TO_OWNER)
    net_account = due if to_owner else bank

    approved = approve(posting, owner_user, run, None if to_owner else bank, day=date(2026, 2, 5))

    assert approved.id == run.id and approved.status == "approved"
    assert approved.pay_date == date(2026, 2, 5)
    assert approved.net_pay_source == ("owner" if to_owner else "account")
    assert approved.net_pay_account_id == (None if to_owner else bank.id)
    assert figures(approved) == stored  # approval never touches a figure
    entry = session.get(JournalEntry, approved.entry_id)
    assert (entry.source_type, entry.source_id) == (SourceType.PAYROLL_RUN, str(run.id))
    assert entry.entry_date == date(2026, 2, 5) and entry.memo == "Payroll run 2026-01"
    lines = entry_lines(session, entry.id)
    assert lines == expected_lines(run, payroll_roles, net_account.id)
    assert sum(d for _, d, _ in lines) == sum(c for _, _, c in lines)
    audit = session.exec(select(AuditLog).where(AuditLog.action == "payroll_run.approve")).one()
    assert (audit.entity, audit.entity_id, audit.user_id) == (
        "payroll_run",
        str(run.id),
        owner_user.id,
    )


def test_line_memos_name_the_component(session, owner_user, posting, payroll_roles, bank):
    run = save(
        session,
        owner_user,
        health_insurance="450.00",
        hsa_contribution="125.00",
        ytd_fica_wages="5000.00",
    )
    approved = approve(posting, owner_user, run, bank)
    memos = {
        r.account_id: r.memo
        for r in session.exec(select(JournalLine).where(JournalLine.entry_id == approved.entry_id))
    }
    roles = {role: payroll_roles[role].id for role in PAYROLL_ROLES}
    assert memos[roles[SystemRole.PAYROLL_WAGES]] == (
        "Gross salary $8,000.00 + medical insurance W-2 addback $450.00 + HSA contribution $125.00"
    )
    assert memos[roles[SystemRole.PAYROLL_TAX_EXPENSE]] == (
        "Employer Social Security + Medicare + FUTA + Georgia SUTA"
    )
    assert memos[roles[SystemRole.PAYROLL_FEDERAL_PAYABLE]] == (
        "Federal income tax withheld + Social Security and Medicare (employee and employer)"
    )
    assert memos[roles[SystemRole.PAYROLL_GA_WH_PAYABLE]] == "Georgia income tax withheld"
    assert memos[roles[SystemRole.PAYROLL_FUTA_PAYABLE]] == "Federal unemployment tax (FUTA)"
    assert memos[roles[SystemRole.PAYROLL_SUTA_PAYABLE]] == "Georgia unemployment tax (SUTA)"
    assert memos[roles[SystemRole.PAYROLL_HEALTH_PAYABLE]] == (
        "Medical insurance premium - clear when paid to insurer"
    )
    assert memos[roles[SystemRole.PAYROLL_HSA_PAYABLE]] == (
        "HSA contribution payable - clear when transferred to HSA"
    )
    assert memos[bank.id] == "Net paycheck to officer"


def test_zero_lines_are_dropped(session, owner_user, posting, payroll_roles, bank):
    # Past both wage bases: no FUTA or SUTA; no health premium and no HSA.
    run = save(session, owner_user, ytd_fica_wages="100000.00")
    assert run.futa == ZERO and run.suta == ZERO
    approved = approve(posting, owner_user, run, bank)
    accounts = {account_id for account_id, _, _ in entry_lines(session, approved.entry_id)}
    assert accounts == {
        payroll_roles[SystemRole.PAYROLL_WAGES].id,
        payroll_roles[SystemRole.PAYROLL_TAX_EXPENSE].id,
        payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE].id,
        payroll_roles[SystemRole.PAYROLL_GA_WH_PAYABLE].id,
        bank.id,
    }
    memos = {
        r.account_id: r.memo
        for r in session.exec(select(JournalLine).where(JournalLine.entry_id == approved.entry_id))
    }
    assert memos[payroll_roles[SystemRole.PAYROLL_WAGES].id] == "Gross salary $8,000.00"
    assert memos[payroll_roles[SystemRole.PAYROLL_TAX_EXPENSE].id] == (
        "Employer Social Security + Medicare"
    )


def test_build_lines_is_pure_and_balanced(session, owner_user, payroll_roles, bank):
    run = save(session, owner_user, health_insurance="450.00", hsa_contribution="125.00")
    accounts = PostingAccounts(
        roles={role: payroll_roles[role].id for role in PAYROLL_ROLES}, net_pay=bank.id
    )
    session.rollback()  # the function needs no session and no database
    lines = build_lines(run, accounts)
    assert [(ln.account_id, ln.debit, ln.credit) for ln in lines] == expected_lines(
        run, payroll_roles, bank.id
    )
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)


# --------------------------------------------------------------------------- balance, roles


def test_an_unbalanced_stored_figure_is_refused_and_nothing_is_written(
    session, owner_user, posting, bank
):
    run = save(session, owner_user)
    session.execute(
        text("UPDATE payroll_run SET federal_withholding = federal_withholding + 1 WHERE id = :i"),
        {"i": run.id},
    )
    session.commit()
    error = refused(session, lambda: approve(posting, owner_user, run, bank), fragment="balance")
    assert error.http_code == 422
    session.refresh(run)
    assert run.status == "draft" and run.entry_id is None


@pytest.mark.parametrize("role", PAYROLL_ROLES)
def test_a_missing_payroll_role_is_named_and_nothing_is_written(
    session, owner_user, posting, bank, role
):
    run = save(session, owner_user)
    AccountService(session).bind_role(role, None, actor=owner_user)
    session.commit()
    refused(
        session,
        lambda: approve(posting, owner_user, run, bank),
        PayrollRoleNotBoundError,
        fragment=role.value,
    )


def test_net_pay_to_owner_needs_the_due_to_owner_role(session, owner_user, posting):
    run = save(session, owner_user)
    AccountService(session).bind_role(SystemRole.DUE_TO_OWNER, None, actor=owner_user)
    session.commit()
    refused(
        session,
        lambda: approve(posting, owner_user, run),
        PayrollRoleNotBoundError,
        fragment=SystemRole.DUE_TO_OWNER.value,
    )


def test_a_balance_that_cannot_post_two_lines_is_refused(session, owner_user, posting, bank):
    run = save(session, owner_user)
    session.execute(
        text(
            "UPDATE payroll_run SET gross_salary = 0, federal_withholding = 0, ga_withholding = 0, "
            "ss_ee = 0, ss_er = 0, total_medicare_ee = 0, medicare_er = 0, net_pay = 0, "
            "total_employer_taxes = 0, futa = 0, suta = 0 WHERE id = :i"
        ),
        {"i": run.id},
    )
    session.commit()
    refused(session, lambda: approve(posting, owner_user, run, bank), fragment="nothing to post")


# --------------------------------------------------------------------------- net pay account


def _bad_accounts(session, owner_user, chart):
    service = AccountService(session)
    inactive = service.create(actor=owner_user, name="Old bank", type=AccountType.ASSET)
    service.set_active(inactive.id, False, actor=owner_user)
    liability = service.create(actor=owner_user, name="A card", type=AccountType.LIABILITY)
    receivable = service.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE)
    undeposited = service.account_by_role(SystemRole.UNDEPOSITED_FUNDS)
    due = service.account_by_role(SystemRole.DUE_TO_OWNER)
    session.commit()
    return {
        "inactive": inactive.id,
        "liability": liability.id,
        "receivable": receivable.id,
        "undeposited": undeposited.id,
        "due": due.id,
        "missing": 999999,
    }


@pytest.mark.parametrize(
    "which", ["inactive", "liability", "receivable", "undeposited", "due", "missing"]
)
def test_the_net_pay_account_must_be_an_active_plain_asset(
    session, owner_user, posting, chart, which
):
    run = save(session, owner_user)
    account_id = _bad_accounts(session, owner_user, chart)[which]
    refused(
        session,
        lambda: posting.approve(
            run.id,
            actor=owner_user,
            pay_date=JAN_END,
            net_pay_source="account",
            net_pay_account_id=account_id,
        ),
    )


@pytest.mark.parametrize(
    ("source", "account"),
    [
        ("owner", 1),  # owner net pay carries no account
        ("account", None),  # a bank source needs one
        ("cheque", None),
        (None, None),
        ("account", True),
        ("account", "7"),
    ],
)
def test_net_pay_source_and_account_must_agree(session, owner_user, posting, source, account):
    run = save(session, owner_user)
    refused(
        session,
        lambda: posting.approve(
            run.id,
            actor=owner_user,
            pay_date=JAN_END,
            net_pay_source=source,
            net_pay_account_id=account,
        ),
    )


# --------------------------------------------------------------------------- pay date


@pytest.mark.parametrize("day", [date(2026, 1, 1), date(2026, 1, 15), JAN_END, date(2026, 2, 6)])
def test_pay_date_on_or_after_the_start_of_the_period_is_accepted(
    session, owner_user, posting, bank, day
):
    run = save(session, owner_user)
    assert approve(posting, owner_user, run, bank, day=day).pay_date == day


@pytest.mark.parametrize("day", [date(2025, 12, 31), date(2025, 1, 15), None, "2026-01-31"])
def test_pay_date_before_the_period_or_not_a_date_is_refused(
    session, owner_user, posting, bank, day
):
    run = save(session, owner_user)
    refused(
        session,
        lambda: posting.approve(
            run.id,
            actor=owner_user,
            pay_date=day,
            net_pay_source="account",
            net_pay_account_id=bank.id,
        ),
    )


def test_pay_date_in_a_locked_period_is_refused(session, owner_user, posting, bank):
    run = save(session, owner_user)
    session.execute(text("UPDATE period_lock SET lock_date = '2026-01-31' WHERE id = 1"))
    session.commit()
    refused(session, lambda: approve(posting, owner_user, run, bank), PeriodLockedError)
    session.refresh(run)
    assert run.status == "draft"


# --------------------------------------------------------------------------- chain rule


def test_an_earlier_draft_blocks_approval_and_is_named(session, owner_user, posting, bank):
    save(session, owner_user, period="2026-01")
    feb = save(session, owner_user, period="2026-02", use_previous_ytd_fica=True)
    error = refused(
        session,
        lambda: approve(posting, owner_user, feb, bank),
        PayrollRunStateError,
        fragment="2026-01",
    )
    assert "draft" in error.message


@pytest.mark.parametrize("earlier", ["approved", "imported", "void"])
def test_an_earlier_approved_imported_or_void_run_allows_approval(
    session, owner_user, posting, bank, earlier
):
    if earlier == "approved":
        approve(posting, owner_user, save(session, owner_user, period="2026-01"), bank)
    elif earlier == "imported":
        plant(session, "imported", "2026-01")
    else:
        jan = approve(posting, owner_user, save(session, owner_user, period="2026-01"), bank)
        posting.void(jan.id, actor=owner_user, void_date=JAN_END)
    feb = save(session, owner_user, period="2026-02", use_previous_ytd_fica=True)
    assert approve(posting, owner_user, feb, bank).status == "approved"


def test_a_later_draft_or_another_year_does_not_block(session, owner_user, posting, bank):
    jan = save(session, owner_user, period="2026-01")
    save(session, owner_user, period="2026-03", gross="9000.00")
    plant(session, "draft", "2025-12", tax_year=2025)  # last year's draft is a different chain
    assert approve(posting, owner_user, jan, bank).status == "approved"


# --------------------------------------------------------------------------- state


def test_second_approve_is_refused(session, owner_user, posting, bank):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    refused(
        session,
        lambda: approve(posting, owner_user, run, bank),
        PayrollRunStateError,
        fragment="approved",
    )


@pytest.mark.parametrize("status", ["imported", "void"])
def test_imported_and_void_runs_cannot_be_approved(session, owner_user, posting, bank, status):
    run = plant(session, status, "2026-01") if status == "imported" else None
    if run is None:
        first = approve(posting, owner_user, save(session, owner_user), bank)
        posting.void(first.id, actor=owner_user, void_date=JAN_END)
        run = first
    refused(session, lambda: approve(posting, owner_user, run, bank), PayrollRunStateError, status)


def test_unknown_run_is_not_found(session, owner_user, posting, bank):
    refused(
        session,
        lambda: posting.approve(
            999999,
            actor=owner_user,
            pay_date=JAN_END,
            net_pay_source="account",
            net_pay_account_id=bank.id,
        ),
        PayrollRunNotFoundError,
    )
    refused(
        session,
        lambda: posting.void(999999, actor=owner_user, void_date=JAN_END),
        PayrollRunNotFoundError,
    )


@pytest.mark.parametrize("role", ["viewer", "accountant"])
def test_only_the_owner_may_approve_or_void(
    session, owner_user, posting, bank, accountant_user, viewer_user, role
):
    actor = accountant_user if role == "accountant" else viewer_user
    run = save(session, owner_user)
    refused(
        session,
        lambda: posting.approve(
            run.id,
            actor=actor,
            pay_date=JAN_END,
            net_pay_source="account",
            net_pay_account_id=bank.id,
        ),
        PermissionDeniedError,
    )
    approved = approve(posting, owner_user, run, bank)
    refused(
        session,
        lambda: posting.void(approved.id, actor=actor, void_date=JAN_END),
        PermissionDeniedError,
    )
    assert session.get(PayrollRun, run.id).status == "approved"


# --------------------------------------------------------------------------- void


def test_void_posts_the_exact_reversal_and_keeps_the_links(
    session, owner_user, posting, payroll_roles, bank
):
    run = save(session, owner_user, health_insurance="450.00", hsa_contribution="125.00")
    approved = approve(posting, owner_user, run, bank)
    stored = figures(approved)
    original = entry_lines(session, approved.entry_id)

    voided = posting.void(run.id, actor=owner_user, void_date=date(2026, 2, 3))

    assert voided.status == "void" and voided.void_date == date(2026, 2, 3)
    assert (voided.entry_id, voided.pay_date) == (approved.entry_id, JAN_END)
    assert voided.void_entry_id is not None and voided.void_entry_id != voided.entry_id
    assert figures(voided) == stored
    reversal = session.get(JournalEntry, voided.void_entry_id)
    assert reversal.reverses_entry_id == voided.entry_id
    assert (reversal.source_type, reversal.source_id) == (SourceType.PAYROLL_RUN, str(run.id))
    assert reversal.entry_date == date(2026, 2, 3)
    assert reversal.memo == "Void of payroll run 2026-01"
    assert entry_lines(session, reversal.id) == [(a, c, d) for a, d, c in original]
    assert session.get(JournalEntry, voided.entry_id).status == "void"
    audit = session.exec(select(AuditLog).where(AuditLog.action == "payroll_run.void")).one()
    assert audit.entity_id == str(run.id)


def test_void_uses_the_callers_memo(session, owner_user, posting, bank):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    voided = posting.void(run.id, actor=owner_user, void_date=JAN_END, memo="  wrong month  ")
    assert session.get(JournalEntry, voided.void_entry_id).memo == "wrong month"


@pytest.mark.parametrize("memo", ["bad\x00memo", "line\nbreak", "x" * 501, 7])
def test_void_memo_is_validated(session, owner_user, posting, bank, memo):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    refused(session, lambda: posting.void(run.id, actor=owner_user, void_date=JAN_END, memo=memo))
    assert session.get(PayrollRun, run.id).status == "approved"


def test_void_date_cannot_precede_the_pay_date(session, owner_user, posting, bank):
    run = approve(posting, owner_user, save(session, owner_user), bank, day=date(2026, 2, 5))
    refused(
        session,
        lambda: posting.void(run.id, actor=owner_user, void_date=date(2026, 2, 4)),
        fragment="2026-02-05",
    )
    refused(session, lambda: posting.void(run.id, actor=owner_user, void_date="2026-03-01"))
    assert posting.void(run.id, actor=owner_user, void_date=date(2026, 2, 5)).status == "void"


def test_void_in_a_locked_period_is_refused(session, owner_user, posting, bank):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    session.execute(text("UPDATE period_lock SET lock_date = '2026-01-31' WHERE id = 1"))
    session.commit()
    refused(
        session,
        lambda: posting.void(run.id, actor=owner_user, void_date=date(2026, 2, 3)),
        PeriodLockedError,
    )
    assert session.get(PayrollRun, run.id).status == "approved"


def test_void_must_run_in_reverse_order_and_names_the_later_run(session, owner_user, posting, bank):
    jan = approve(posting, owner_user, save(session, owner_user, period="2026-01"), bank)
    feb = approve(
        posting,
        owner_user,
        save(session, owner_user, period="2026-02", use_previous_ytd_fica=True),
        bank,
    )
    refused(
        session,
        lambda: posting.void(jan.id, actor=owner_user, void_date=FEB_END),
        PayrollRunStateError,
        fragment="2026-02",
    )
    assert posting.void(feb.id, actor=owner_user, void_date=FEB_END).status == "void"
    assert posting.void(jan.id, actor=owner_user, void_date=FEB_END).status == "void"


def test_a_later_draft_does_not_block_a_void(session, owner_user, posting, bank):
    jan = approve(posting, owner_user, save(session, owner_user, period="2026-01"), bank)
    save(session, owner_user, period="2026-02", use_previous_ytd_fica=True)
    assert posting.void(jan.id, actor=owner_user, void_date=JAN_END).status == "void"


def test_second_void_and_void_of_non_approved_runs_are_refused(session, owner_user, posting, bank):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    posting.void(run.id, actor=owner_user, void_date=JAN_END)
    refused(
        session,
        lambda: posting.void(run.id, actor=owner_user, void_date=JAN_END),
        PayrollRunStateError,
        fragment="void",
    )
    draft = save(session, owner_user, period="2026-03")
    refused(
        session,
        lambda: posting.void(draft.id, actor=owner_user, void_date=JAN_END),
        PayrollRunStateError,
        fragment="draft",
    )
    imported = plant(session, "imported", "2025-11")
    refused(
        session,
        lambda: posting.void(imported.id, actor=owner_user, void_date=JAN_END),
        PayrollRunStateError,
        fragment="imported",
    )


def test_a_voided_period_can_be_saved_and_approved_again(session, owner_user, posting, bank):
    first = approve(posting, owner_user, save(session, owner_user), bank)
    posting.void(first.id, actor=owner_user, void_date=JAN_END)

    second = save(session, owner_user, gross="9000.00")
    assert second.id != first.id and second.status == "draft"
    approved = approve(posting, owner_user, second, bank)

    assert approved.status == "approved" and approved.entry_id != first.entry_id
    statuses = session.exec(
        select(PayrollRun.status).where(PayrollRun.pay_period == "2026-01").order_by(PayrollRun.id)
    ).all()
    assert statuses == ["void", "approved"]
    # Each run's entry carries its own source id; the void one stays reversed.
    ids = session.exec(
        select(JournalEntry.source_id).where(JournalEntry.source_type == SourceType.PAYROLL_RUN)
    ).all()
    assert sorted(ids) == sorted([str(first.id)] * 2 + [str(second.id)])


# --------------------------------------------------------------------------- preview


def test_preview_shows_the_lines_without_writing(session, owner_user, posting, payroll_roles, bank):
    run = save(session, owner_user, health_insurance="450.00", hsa_contribution="125.00")
    before = counts(session)

    lines = posting.preview(run.id)

    assert counts(session) == before
    assert session.get(PayrollRun, run.id).status == "draft"
    expected = expected_lines(run, payroll_roles, -1)
    assert [(ln.debit, ln.credit) for ln in lines] == [(d, c) for _, d, c in expected]
    labels = [ln.account_label for ln in lines]
    assert labels[0] == payroll_roles[SystemRole.PAYROLL_WAGES].name
    assert labels[-1] == "Net pay account (chosen when approving)"
    assert lines[0].memo.startswith("Gross salary")


def test_preview_of_an_approved_run_names_the_account_that_was_used(
    session, owner_user, posting, bank
):
    run = approve(posting, owner_user, save(session, owner_user), bank)
    assert posting.preview(run.id)[-1].account_label == bank.name


def test_preview_of_an_imported_run_is_refused_and_a_missing_role_is_named(
    session, owner_user, posting
):
    imported = plant(session, "imported", "2026-01")
    with pytest.raises(PayrollRunStateError):
        posting.preview(imported.id)
    draft = save(session, owner_user, period="2026-03")
    AccountService(session).bind_role(SystemRole.PAYROLL_HSA_PAYABLE, None, actor=owner_user)
    session.commit()
    with pytest.raises(PayrollRoleNotBoundError) as caught:
        posting.preview(draft.id)
    assert SystemRole.PAYROLL_HSA_PAYABLE.value in caught.value.message
    with pytest.raises(PayrollRunNotFoundError):
        posting.preview(999999)


# --------------------------------------------------------------------------- stale delete


def test_delete_run_rereads_the_row_it_locks(session, owner_user, bank, entries):
    """A status changed behind the session's back must not be read from the identity map."""
    run = save(session, owner_user)
    assert run.status == "draft"  # in the identity map as a draft
    session.execute(
        text(
            "UPDATE payroll_run SET status = 'approved', pay_date = '2026-01-31', "
            "net_pay_source = 'owner', entry_id = :e WHERE id = :i"
        ),
        {"e": entries(), "i": run.id},
    )
    with pytest.raises(PayrollRunStateError):
        PayrollRunService(session).delete_run(run.id, actor=owner_user)
