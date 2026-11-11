"""Tax payments to agencies: validation, posting, voids, audit."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text as sql_text
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.accounts import PAYROLL_ROLES, AccountService
from payroll.ledger.exceptions import PeriodLockedError
from payroll.ledger.models import AccountType, JournalEntry, SystemRole
from payroll.payroll_run.exceptions import (
    InvalidTaxPaymentError,
    PayrollRoleNotBoundError,
    TaxPaymentError,
    TaxPaymentNotFoundError,
)
from payroll.payroll_run.ledger_models import TaxPayment
from payroll.payroll_run.tax_payments import AGENCY_ROLES, TaxPaymentService
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 3, 1)
LATER = date(2026, 3, 5)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles, bank):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def make(session, owner_user, bank, amount="50.00", agency="irs_941", **kw):
    kw.setdefault("payment_date", DAY)
    return TaxPaymentService(session).create(
        actor=owner_user, agency=agency, bank_account_id=bank.id, amount=D(amount), **kw
    )


def entry_count(session):
    return session.exec(select(func.count()).select_from(JournalEntry)).one()


def payment_count(session):
    return session.exec(select(func.count()).select_from(TaxPayment)).one()


def audit_count(session):
    return session.exec(select(func.count()).select_from(AuditLog)).one()


def balance(session, account):
    row = session.execute(
        sql_text(
            "SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM journal_line l "
            "JOIN journal_entry e ON e.id = l.entry_id WHERE l.account_id = :a"
        ),
        {"a": account.id},
    ).scalar_one()
    return D(row)


def rejected(session, owner_user, bank, fragment=None, error=InvalidTaxPaymentError, **kw):
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(error) as exc:
        make(session, owner_user, bank, **kw)
    if fragment:
        assert fragment in exc.value.message
    assert entry_count(session) == entries
    assert payment_count(session) == 0
    assert audit_count(session) == audits
    return exc.value


# ------------------------------------------------------------------ create


def test_agency_role_mapping():
    assert AGENCY_ROLES == {
        "irs_941": SystemRole.PAYROLL_FEDERAL_PAYABLE,
        "irs_futa": SystemRole.PAYROLL_FUTA_PAYABLE,
        "ga_dor": SystemRole.PAYROLL_GA_WH_PAYABLE,
        "ga_dol": SystemRole.PAYROLL_SUTA_PAYABLE,
    }


@pytest.mark.parametrize("agency", list(AGENCY_ROLES))
def test_each_agency_posts_to_its_payable_account(session, owner_user, bank, payroll_roles, agency):
    payment = make(session, owner_user, bank, "125.50", agency=agency, memo="  Q1  ")
    payable = payroll_roles[AGENCY_ROLES[agency]]
    assert (payment.amount, payment.status, payment.memo) == (D("125.50"), "posted", "Q1")
    assert (payment.agency, payment.bank_account_id) == (agency, bank.id)
    assert balance(session, payable) == D("125.50")  # Dr the payable
    assert balance(session, bank) == D("-125.50")  # Cr the bank
    for role, account in payroll_roles.items():
        if account.id != payable.id:
            assert balance(session, account) == D("0")
    entry = session.get(JournalEntry, payment.entry_id)
    assert entry.source_type == "tax_payment" and entry.source_id == str(payment.id)
    assert entry.entry_date == DAY and entry.memo == "Q1"


def test_default_memo_is_readable(session, owner_user, bank):
    payment = make(session, owner_user, bank, agency="ga_dor")
    assert session.get(JournalEntry, payment.entry_id).memo == "Tax payment ga_dor"


def test_overpayment_accepted(session, owner_user, bank, payroll_roles):
    make(session, owner_user, bank, "999.00", agency="irs_futa")
    assert balance(session, payroll_roles[SystemRole.PAYROLL_FUTA_PAYABLE]) == D("999.00")


def test_create_audits(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    row = session.exec(select(AuditLog).where(AuditLog.action == "tax_payment.create")).one()
    assert row.entity == "tax_payment" and row.entity_id == str(payment.id)


def test_unbound_role_is_named(session, owner_user, bank, payroll_roles):
    AccountService(session).bind_role(SystemRole.PAYROLL_GA_WH_PAYABLE, None, actor=owner_user)
    session.commit()
    error = rejected(session, owner_user, bank, error=PayrollRoleNotBoundError, agency="ga_dor")
    assert "payroll_ga_wh_payable" in error.message


@pytest.mark.parametrize("bad", ["irs", "", "IRS_941", None, 5, True, ["irs_941"], "irs_941 "])
def test_bad_agency_refused(session, owner_user, bank, bad):
    rejected(session, owner_user, bank, "agency", agency=bad)


@pytest.mark.parametrize(
    "role",
    [SystemRole.ACCOUNTS_RECEIVABLE, SystemRole.UNDEPOSITED_FUNDS, SystemRole.DUE_TO_OWNER],
)
def test_bound_role_bank_refused(session, owner_user, bank, role):
    bound = AccountService(session).account_by_role(role)
    assert bound is not None
    session.commit()
    rejected(session, owner_user, bound)


@pytest.mark.parametrize("role", PAYROLL_ROLES)
def test_any_payroll_role_account_as_bank_refused(session, owner_user, payroll_roles, role):
    rejected(session, owner_user, payroll_roles[role], "cannot pay a tax")


def test_revenue_account_refused(session, owner_user, bank):
    revenue = AccountService(session).create(
        actor=owner_user, name="Sales Test", type=AccountType.INCOME
    )
    session.commit()
    rejected(session, owner_user, revenue)


def test_inactive_account_refused(session, owner_user, bank):
    AccountService(session).set_active(bank.id, False, actor=owner_user)
    session.commit()
    rejected(session, owner_user, bank, "inactive")


def test_missing_bank_refused(session, owner_user):
    entries = entry_count(session)
    with pytest.raises(InvalidTaxPaymentError):
        TaxPaymentService(session).create(
            actor=owner_user,
            payment_date=DAY,
            agency="irs_941",
            bank_account_id=999999,
            amount=D("1.00"),
        )
    assert payment_count(session) == 0 and entry_count(session) == entries


@pytest.mark.parametrize("bad", ["1", 1.5, True, None, [1]])
def test_non_int_bank_id_refused(session, owner_user, bad):
    with pytest.raises(InvalidTaxPaymentError):
        TaxPaymentService(session).create(
            actor=owner_user,
            payment_date=DAY,
            agency="irs_941",
            bank_account_id=bad,
            amount=D("1.00"),
        )
    assert payment_count(session) == 0


@pytest.mark.parametrize(
    ("amount", "fragment"),
    [
        (D("0.00"), "between"),
        (D("-1.00"), "between"),
        (D("1.005"), "two decimal"),
        (D("1E+30"), "between"),
        (D("100000000.01"), "between"),
        (D("NaN"), "number"),
        (D("Infinity"), "number"),
        (50, "number"),
        (50.0, "number"),
        ("50.00", "number"),
        (True, "number"),
        (None, "number"),
    ],
)
def test_bad_amounts_refused(session, owner_user, bank, amount, fragment):
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(InvalidTaxPaymentError) as exc:
        TaxPaymentService(session).create(
            actor=owner_user,
            payment_date=DAY,
            agency="irs_941",
            bank_account_id=bank.id,
            amount=amount,
        )
    assert fragment in exc.value.message
    assert entry_count(session) == entries and payment_count(session) == 0
    assert audit_count(session) == audits


def test_max_amount_accepted(session, owner_user, bank):
    assert make(session, owner_user, bank, "100000000.00").amount == D("100000000.00")


@pytest.mark.parametrize(
    "bad", [None, "2026-03-01", 20260301, date(1899, 12, 31), date(9999, 12, 31)]
)
def test_unsupported_dates_refused(session, owner_user, bank, bad):
    rejected(session, owner_user, bank, payment_date=bad)


def test_memo_validation(session, owner_user, bank):
    rejected(session, owner_user, bank, "Memo", memo="x" * 501)
    rejected(session, owner_user, bank, "control", memo="a\x00b")
    rejected(session, owner_user, bank, "text", memo=5)


def test_memo_at_cap_accepted(session, owner_user, bank):
    assert len(make(session, owner_user, bank, memo="x" * 500).memo) == 500


def test_lock_date_refused(session, owner_user, bank):
    session.execute(sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LATER})
    session.commit()
    rejected(session, owner_user, bank, error=PeriodLockedError)


@pytest.mark.parametrize("who", ["accountant_user", "viewer_user"])
def test_non_owner_refused_create_and_void(session, owner_user, bank, request, who):
    actor = request.getfixturevalue(who)
    session.commit()
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(PermissionDeniedError):
        TaxPaymentService(session).create(
            actor=actor,
            payment_date=DAY,
            agency="irs_941",
            bank_account_id=bank.id,
            amount=D("1.00"),
        )
    assert payment_count(session) == 0 and entry_count(session) == entries
    assert audit_count(session) == audits
    payment = make(session, owner_user, bank)
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(PermissionDeniedError):
        TaxPaymentService(session).void(payment.id, actor=actor, void_date=LATER)
    assert session.get(TaxPayment, payment.id).status == "posted"
    assert entry_count(session) == entries and audit_count(session) == audits


# -------------------------------------------------------------------- void


def test_void_reverses_and_audits(session, owner_user, bank, payroll_roles):
    payment = make(session, owner_user, bank, agency="ga_dol")
    voided = TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    assert (voided.status, voided.void_date) == ("void", LATER)
    assert voided.void_entry_id is not None
    assert balance(session, bank) == D("0.00")
    assert balance(session, payroll_roles[SystemRole.PAYROLL_SUTA_PAYABLE]) == D("0.00")
    reversal = session.get(JournalEntry, voided.void_entry_id)
    assert reversal.entry_date == LATER and "2026-03-01" in reversal.memo
    assert "tax payment" in reversal.memo
    assert session.exec(select(AuditLog).where(AuditLog.action == "tax_payment.void")).one()


def test_void_custom_memo(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    voided = TaxPaymentService(session).void(
        payment.id, actor=owner_user, void_date=LATER, memo="oops"
    )
    assert session.get(JournalEntry, voided.void_entry_id).memo == "oops"


def test_void_on_payment_date_accepted(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    assert TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=DAY).void_date


def test_void_date_before_payment_refused(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    entries, audits = entry_count(session), audit_count(session)
    for bad in (date(2026, 2, 28), None, "2026-03-05"):
        with pytest.raises(InvalidTaxPaymentError):
            TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=bad)
    assert entry_count(session) == entries and audit_count(session) == audits
    assert session.get(TaxPayment, payment.id).status == "posted"


def test_void_bad_memo_refused(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    entries = entry_count(session)
    with pytest.raises(InvalidTaxPaymentError):
        TaxPaymentService(session).void(
            payment.id, actor=owner_user, void_date=LATER, memo="a\x00b"
        )
    assert entry_count(session) == entries


def test_void_on_lock_date_refused(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    session.execute(sql_text("UPDATE period_lock SET lock_date = :d WHERE id = 1"), {"d": LATER})
    session.commit()
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(PeriodLockedError):
        TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    assert entry_count(session) == entries and audit_count(session) == audits
    assert session.get(TaxPayment, payment.id).status == "posted"


def test_second_void_refused(session, owner_user, bank):
    payment = make(session, owner_user, bank)
    TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(TaxPaymentError):
        TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    assert entry_count(session) == entries and audit_count(session) == audits


def test_void_missing_payment(session, owner_user):
    with pytest.raises(TaxPaymentNotFoundError):
        TaxPaymentService(session).void(999999, actor=owner_user, void_date=LATER)


@pytest.mark.parametrize("bad", ["1", 1.5, True, None])
def test_void_non_int_id_refused(session, owner_user, bad):
    with pytest.raises(InvalidTaxPaymentError):
        TaxPaymentService(session).void(bad, actor=owner_user, void_date=LATER)


def test_void_unbound_role_named_and_nothing_written(session, owner_user, bank, payroll_roles):
    payment = make(session, owner_user, bank)
    payable = payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE]
    payable.system_role = None  # a direct write: the service refuses to unbind it
    session.add(payable)
    session.commit()
    entries, audits = entry_count(session), audit_count(session)
    with pytest.raises(PayrollRoleNotBoundError):
        TaxPaymentService(session).void(payment.id, actor=owner_user, void_date=LATER)
    assert entry_count(session) == entries and audit_count(session) == audits
    assert session.get(TaxPayment, payment.id).status == "posted"


def test_payroll_role_frozen_once_a_payment_exists(session, owner_user, bank, payroll_roles):
    from payroll.ledger.exceptions import InvalidAccountError

    make(session, owner_user, bank)
    with pytest.raises(InvalidAccountError):
        AccountService(session).bind_role(
            SystemRole.PAYROLL_FEDERAL_PAYABLE, None, actor=owner_user
        )


# ------------------------------------------------------------------- reads


def test_get_and_list(session, owner_user, bank):
    first = make(session, owner_user, bank, "1.00", agency="irs_941")
    second = make(session, owner_user, bank, "2.00", agency="ga_dor", payment_date=LATER)
    third = make(session, owner_user, bank, "3.00", agency="irs_941", payment_date=LATER)
    service = TaxPaymentService(session)
    assert service.get(first.id).amount == D("1.00")
    with pytest.raises(TaxPaymentNotFoundError):
        service.get(999999)
    with pytest.raises(TaxPaymentNotFoundError):
        service.get(True)
    assert [p.id for p in service.list()] == [third.id, second.id, first.id]
    assert [p.id for p in service.list(agency="irs_941")] == [third.id, first.id]
    assert [p.id for p in service.list(limit=1, offset=1)] == [second.id]


@pytest.mark.parametrize(
    ("limit", "offset", "agency"),
    [
        (0, 0, None),
        (201, 0, None),
        (-1, 0, None),
        (10, -1, None),
        (True, 0, None),
        ("5", 0, None),
        (5, None, None),
        (50, 0, "irs"),
        (50, 0, 5),
    ],
)
def test_list_validates_arguments(session, limit, offset, agency):
    with pytest.raises(InvalidTaxPaymentError):
        TaxPaymentService(session).list(agency=agency, limit=limit, offset=offset)
