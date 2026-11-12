"""Payroll liability report: derived from approved runs, tax payments and the ledger balance.

Every figure comes from the real services; expected amounts are summed here from the runs'
stored columns (federal = withholding + employee SS + employee Medicare + employer SS +
employer Medicare), and FUTA / SUTA are worked by hand from the 2026 settings fixture
(FUTA 0.6% on a $7,000 base, SUTA 2.64% on a $9,500 base, $8,000 wages a month):
January FUTA 42.00 and SUTA 211.20; February FUTA 0.00 and SUTA 39.60.
"""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import delete, text
from sqlmodel import func, select

from payroll.ledger.models import SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.payroll_run.ledger_models import PayrollDepositRules
from payroll.payroll_run.liability import payroll_liability_report
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.posting import PayrollPostingService
from payroll.payroll_run.settings_service import PayrollSettingsService
from payroll.payroll_run.tax_payments import TaxPaymentService
from tests.payroll_run.test_posting import approve, last_day, plant, save

D = Decimal
ZERO = D("0.00")


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles, year_settings, bank):
    session.commit()


def fed(run: PayrollRun) -> Decimal:
    return run.federal_withholding + run.ss_ee + run.total_medicare_ee + run.ss_er + run.medicare_er


def post_run(session, owner_user, bank, period, chained=False) -> PayrollRun:
    extra = {"use_previous_ytd_fica": True} if chained else {}
    run = save(session, owner_user, period=period, gross="8000.00", **extra)
    approve(PayrollPostingService(session), owner_user, run, bank)
    return run


def two_runs(session, owner_user, bank):
    jan = post_run(session, owner_user, bank, "2026-01")
    feb = post_run(session, owner_user, bank, "2026-02", chained=True)
    return jan, feb


def pay(session, owner_user, bank, agency, amount, day):
    return TaxPaymentService(session).create(
        actor=owner_user, payment_date=day, agency=agency, bank_account_id=bank.id, amount=amount
    )


def agency(report, key):
    return next(a for a in report.agencies if a.agency == key)


def period(report, key, pay_period):
    return next(p for p in agency(report, key).periods if p.pay_period == pay_period)


def rules(session, owner_user, **fields):
    PayrollSettingsService(session).set_deposit_rules(actor=owner_user, **fields)


def liability_on(session, key, pay_period, day):
    return period(payroll_liability_report(session, day), key, pay_period)


# ------------------------------------------------------------------------- clean data


def test_clean_books_have_no_other_and_balance_equals_unpaid(session, owner_user, bank):
    jan, feb = two_runs(session, owner_user, bank)
    report = payroll_liability_report(session, date(2026, 3, 1))

    assert report.as_of == date(2026, 3, 1)
    assert [a.agency for a in report.agencies] == ["irs_941", "irs_futa", "ga_dor", "ga_dol"]
    expected = {
        "irs_941": [("2026-01", fed(jan)), ("2026-02", fed(feb))],
        "irs_futa": [("2026-01", D("42.00"))],  # February: base already reached, no period
        "ga_dor": [("2026-01", jan.ga_withholding), ("2026-02", feb.ga_withholding)],
        "ga_dol": [("2026-01", D("211.20")), ("2026-02", D("39.60"))],
    }
    for key, rows in expected.items():
        got = agency(report, key)
        assert [(p.pay_period, p.liability, p.paid, p.unpaid) for p in got.periods] == [
            (label, amount, ZERO, amount) for label, amount in rows
        ]
        assert got.other == ZERO
        assert got.balance == sum((amount for _, amount in rows), ZERO)
        assert all(isinstance(v, Decimal) for v in (got.balance, got.other))


def test_four_agencies_are_independent(session, owner_user, bank):
    jan, feb = two_runs(session, owner_user, bank)
    pay(session, owner_user, bank, "irs_941", fed(jan), date(2026, 2, 10))
    report = payroll_liability_report(session, date(2026, 3, 1))

    assert period(report, "irs_941", "2026-01").unpaid == ZERO
    assert period(report, "irs_941", "2026-02").unpaid == fed(feb)
    assert agency(report, "irs_941").balance == fed(feb)
    assert period(report, "ga_dor", "2026-01").unpaid == jan.ga_withholding
    assert period(report, "irs_futa", "2026-01").unpaid == D("42.00")
    assert period(report, "ga_dol", "2026-01").unpaid == D("211.20")


# ------------------------------------------------------------------------- payments


def test_partial_payment_ages_oldest_period_first(session, owner_user, bank):
    jan, feb = two_runs(session, owner_user, bank)
    pay(session, owner_user, bank, "irs_941", fed(jan) + D("100.00"), date(2026, 2, 20))
    report = payroll_liability_report(session, date(2026, 3, 1))

    january, february = period(report, "irs_941", "2026-01"), period(report, "irs_941", "2026-02")
    assert (january.paid, january.unpaid) == (fed(jan), ZERO)
    assert (february.paid, february.unpaid) == (D("100.00"), fed(feb) - D("100.00"))
    assert agency(report, "irs_941").balance == fed(feb) - D("100.00")
    assert agency(report, "irs_941").other == ZERO


def test_overpayment_shows_a_negative_other_and_pays_every_period(session, owner_user, bank):
    two_runs(session, owner_user, bank)
    pay(session, owner_user, bank, "ga_dol", D("300.00"), date(2026, 3, 5))  # owed 250.80
    report = payroll_liability_report(session, date(2026, 3, 10))

    got = agency(report, "ga_dol")
    assert [(p.paid, p.unpaid) for p in got.periods] == [(D("211.20"), ZERO), (D("39.60"), ZERO)]
    assert got.balance == D("-49.20")
    assert got.other == D("-49.20")
    assert got.next_due is None


def test_voided_payment_contributes_nothing(session, owner_user, bank):
    jan = post_run(session, owner_user, bank, "2026-01")
    made = pay(session, owner_user, bank, "irs_941", fed(jan), date(2026, 2, 10))
    TaxPaymentService(session).void(made.id, actor=owner_user, void_date=date(2026, 2, 12))

    after = payroll_liability_report(session, date(2026, 3, 1))
    assert period(after, "irs_941", "2026-01").paid == ZERO
    assert agency(after, "irs_941").balance == fed(jan)
    assert agency(after, "irs_941").other == ZERO

    between = payroll_liability_report(session, date(2026, 2, 11))  # payment still in force
    assert period(between, "irs_941", "2026-01").unpaid == ZERO
    assert agency(between, "irs_941").balance == ZERO
    assert agency(between, "irs_941").other == ZERO


# ------------------------------------------------------------------------- other


def test_manual_journal_credit_shows_as_other(session, owner_user, chart, payroll_roles):
    payable = payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE]
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 1, 2),
            [
                LineDraft(chart["Business Checking"].id, debit=D("75.00")),
                LineDraft(payable.id, credit=D("75.00")),
            ],
            memo="opening balance",
        ),
        actor=owner_user,
    )
    report = payroll_liability_report(session, date(2026, 3, 1))

    got = agency(report, "irs_941")
    assert (got.balance, got.periods, got.other, got.next_due) == (D("75.00"), [], D("75.00"), None)
    assert agency(report, "ga_dor").other == ZERO


def test_other_sits_beside_open_periods(session, owner_user, bank, chart, payroll_roles):
    jan = post_run(session, owner_user, bank, "2026-01")
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 1, 5),
            [
                LineDraft(chart["Business Checking"].id, debit=D("20.00")),
                LineDraft(payroll_roles[SystemRole.PAYROLL_GA_WH_PAYABLE].id, credit=D("20.00")),
            ],
        ),
        actor=owner_user,
    )
    got = agency(payroll_liability_report(session, date(2026, 3, 1)), "ga_dor")
    assert got.balance == jan.ga_withholding + D("20.00")
    assert got.other == D("20.00")
    assert got.periods[0].unpaid == jan.ga_withholding


# ------------------------------------------------------------------------- what makes a period


def test_voided_run_and_imported_run_create_no_period(session, owner_user, bank):
    plant(
        session,
        status="imported",
        period="2025-12",
        ga_withholding=D("5.00"),
        pay_date=date(2025, 12, 31),
    )
    jan = post_run(session, owner_user, bank, "2026-01")
    PayrollPostingService(session).void(jan.id, actor=owner_user, void_date=date(2026, 2, 5))
    report = payroll_liability_report(session, date(2026, 3, 1))

    for key in ("irs_941", "irs_futa", "ga_dor", "ga_dol"):
        got = agency(report, key)
        assert (got.periods, got.balance, got.other, got.next_due) == ([], ZERO, ZERO, None)


def test_run_voided_after_as_of_was_still_in_force(session, owner_user, bank):
    jan = post_run(session, owner_user, bank, "2026-01")
    PayrollPostingService(session).void(jan.id, actor=owner_user, void_date=date(2026, 2, 5))
    report = payroll_liability_report(session, date(2026, 2, 4))

    got = agency(report, "irs_941")
    assert got.balance == fed(jan)
    assert got.other == ZERO
    assert [p.pay_period for p in got.periods] == ["2026-01"]


def test_as_of_in_the_past_ignores_later_runs_and_payments(session, owner_user, bank):
    jan, feb = two_runs(session, owner_user, bank)
    pay(session, owner_user, bank, "irs_941", fed(jan), date(2026, 3, 5))
    report = payroll_liability_report(session, date(2026, 2, 15))  # before Feb 28 pay date

    got = agency(report, "irs_941")
    assert [(p.pay_period, p.paid, p.unpaid) for p in got.periods] == [("2026-01", ZERO, fed(jan))]
    assert got.balance == fed(jan)
    assert got.other == ZERO

    later = payroll_liability_report(session, date(2026, 3, 5))
    assert [(p.pay_period, p.unpaid) for p in agency(later, "irs_941").periods] == [
        ("2026-01", ZERO),
        ("2026-02", fed(feb)),
    ]


def test_unbound_role_gives_an_empty_agency(session, owner_user, bank):
    jan = post_run(session, owner_user, bank, "2026-01")
    # The service refuses to unbind once payroll has run, so unbind in the table directly.
    session.execute(
        text("UPDATE account SET system_role = NULL WHERE system_role = :role"),
        {"role": SystemRole.PAYROLL_SUTA_PAYABLE.value},
    )
    session.commit()
    report = payroll_liability_report(session, date(2026, 3, 1))

    got = agency(report, "ga_dol")
    assert (got.balance, got.periods, got.other, got.next_due) == (ZERO, [], ZERO, None)
    assert [p.unpaid for p in agency(report, "ga_dor").periods] == [jan.ga_withholding]


# ------------------------------------------------------------------------- due dates


def test_default_due_dates(session, owner_user, bank):
    post_run(session, owner_user, bank, "2026-01")
    report = payroll_liability_report(session, date(2026, 1, 31))

    assert period(report, "irs_941", "2026-01").due_date == date(2026, 2, 15)
    assert period(report, "ga_dor", "2026-01").due_date == date(2026, 2, 15)
    assert period(report, "irs_futa", "2026-01").due_date == date(2026, 4, 30)
    assert period(report, "ga_dol", "2026-01").due_date == date(2026, 4, 30)


def test_december_run_is_due_in_january_of_the_next_year(session, owner_user, bank):
    post_run(session, owner_user, bank, "2026-12")
    report = payroll_liability_report(session, date(2026, 12, 31))

    assert period(report, "irs_941", "2026-12").due_date == date(2027, 1, 15)
    assert period(report, "irs_futa", "2026-12").due_date == date(2027, 1, 31)


@pytest.mark.parametrize(
    ("pay_period", "due"),
    [
        ("2026-01", date(2026, 4, 30)),
        ("2026-03", date(2026, 4, 30)),
        ("2026-04", date(2026, 7, 31)),
        ("2026-06", date(2026, 7, 31)),
        ("2026-07", date(2026, 10, 31)),
        ("2026-09", date(2026, 10, 31)),
    ],
)
def test_quarterly_due_is_in_the_month_after_the_quarter(
    session, owner_user, bank, pay_period, due
):
    post_run(session, owner_user, bank, pay_period)
    report = payroll_liability_report(session, last_day(pay_period))
    assert period(report, "ga_dol", pay_period).due_date == due


def test_quarterly_cadence_with_a_day_for_941(session, owner_user, bank):
    rules(session, owner_user, irs_941_cadence="quarterly", irs_941_due_day=10)
    post_run(session, owner_user, bank, "2026-05")
    report = payroll_liability_report(session, date(2026, 5, 31))
    assert period(report, "irs_941", "2026-05").due_date == date(2026, 7, 10)


def test_monthly_end_of_month_day_zero(session, owner_user, bank):
    rules(session, owner_user, ga_dor_due_day=0)
    post_run(session, owner_user, bank, "2026-01")
    report = payroll_liability_report(session, date(2026, 1, 31))
    assert period(report, "ga_dor", "2026-01").due_date == date(2026, 2, 28)


def test_day_31_clamps_to_a_30_day_month(session, owner_user, bank):
    rules(session, owner_user, ga_dor_due_day=31)
    post_run(session, owner_user, bank, "2026-03")
    report = payroll_liability_report(session, date(2026, 3, 31))
    assert period(report, "ga_dor", "2026-03").due_date == date(2026, 4, 30)


# ------------------------------------------------------------------------- overdue and next due


def test_overdue_only_when_unpaid_and_due_before_as_of(session, owner_user, bank):
    jan = post_run(session, owner_user, bank, "2026-01")  # 941 due Feb 15
    assert liability_on(session, "irs_941", "2026-01", date(2026, 2, 14)).overdue is False
    assert liability_on(session, "irs_941", "2026-01", date(2026, 2, 15)).overdue is False
    assert liability_on(session, "irs_941", "2026-01", date(2026, 2, 16)).overdue is True

    pay(session, owner_user, bank, "irs_941", fed(jan), date(2026, 2, 16))
    paid = liability_on(session, "irs_941", "2026-01", date(2026, 2, 16))
    assert (paid.unpaid, paid.overdue) == (ZERO, False)


def test_next_due_is_the_earliest_unpaid_period(session, owner_user, bank):
    jan, feb = two_runs(session, owner_user, bank)
    first = payroll_liability_report(session, date(2026, 3, 1))
    assert agency(first, "irs_941").next_due == date(2026, 2, 15)

    pay(session, owner_user, bank, "irs_941", fed(jan), date(2026, 3, 2))
    second = payroll_liability_report(session, date(2026, 3, 2))
    assert agency(second, "irs_941").next_due == date(2026, 3, 15)

    pay(session, owner_user, bank, "irs_941", fed(feb), date(2026, 3, 3))
    third = payroll_liability_report(session, date(2026, 3, 3))
    assert agency(third, "irs_941").next_due is None


# ------------------------------------------------------------------------- read-only


def test_report_with_no_rules_row_uses_defaults_and_writes_nothing(session, owner_user, bank):
    post_run(session, owner_user, bank, "2026-01")
    session.execute(delete(PayrollDepositRules))
    session.commit()
    assert session.get(PayrollDepositRules, 1) is None

    report = payroll_liability_report(session, date(2026, 1, 31))

    assert period(report, "irs_941", "2026-01").due_date == date(2026, 2, 15)
    assert not session.new and not session.dirty
    session.rollback()
    assert session.exec(select(func.count()).select_from(PayrollDepositRules)).one() == 0


# ------------------------------------------------------------------------- opening bucket


def opening_credit(session, owner_user, chart, payroll_roles, amount, day=date(2026, 1, 2)):
    """A pre-upgrade liability journaled by hand: credit the 941 payable, debit the bank."""
    LedgerPostingService(session).post(
        EntryDraft(
            day,
            [
                LineDraft(chart["Business Checking"].id, debit=D(amount)),
                LineDraft(payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE].id, credit=D(amount)),
            ],
            memo="October 941 liability (before the upgrade)",
        ),
        actor=owner_user,
    )


def test_a_payment_of_the_opening_liability_does_not_settle_the_first_period(
    session, owner_user, bank, chart, payroll_roles
):
    opening_credit(session, owner_user, chart, payroll_roles, "3000.00")
    pay(session, owner_user, bank, "irs_941", D("3000.00"), date(2026, 1, 20))
    jan = post_run(session, owner_user, bank, "2026-01")
    report = payroll_liability_report(session, date(2026, 3, 1))

    got = agency(report, "irs_941")
    # 3,000.00 credited, 3,000.00 paid, then the run's own federal liability is all that is left.
    assert got.balance == fed(jan)
    assert [(p.liability, p.paid, p.unpaid) for p in got.periods] == [(fed(jan), ZERO, fed(jan))]
    assert got.other == ZERO
    assert got.next_due == date(2026, 2, 15)  # the January deposit still shows as due


def test_a_part_payment_of_the_opening_liability_leaves_the_rest_in_other(
    session, owner_user, bank, chart, payroll_roles
):
    opening_credit(session, owner_user, chart, payroll_roles, "3000.00")
    pay(session, owner_user, bank, "irs_941", D("1200.00"), date(2026, 1, 20))
    jan = post_run(session, owner_user, bank, "2026-01")
    got = agency(payroll_liability_report(session, date(2026, 3, 1)), "irs_941")

    assert got.balance == D("1800.00") + fed(jan)  # 3,000.00 - 1,200.00 + the run
    assert got.periods[0].paid == ZERO and got.periods[0].unpaid == fed(jan)
    assert got.other == D("1800.00")  # the unpaid remainder of the opening amount
    assert got.balance == sum((p.unpaid for p in got.periods), ZERO) + got.other


def test_payment_beyond_the_opening_liability_goes_to_the_oldest_period(
    session, owner_user, bank, chart, payroll_roles
):
    opening_credit(session, owner_user, chart, payroll_roles, "3000.00")
    pay(session, owner_user, bank, "irs_941", D("3500.00"), date(2026, 1, 20))
    jan = post_run(session, owner_user, bank, "2026-01")
    got = agency(payroll_liability_report(session, date(2026, 3, 1)), "irs_941")

    assert got.periods[0].paid == D("500.00")
    assert got.periods[0].unpaid == fed(jan) - D("500.00")
    assert got.other == ZERO
    assert got.balance == fed(jan) - D("500.00")


def test_an_opening_credit_dated_after_as_of_is_not_in_the_opening_bucket(
    session, owner_user, bank, chart, payroll_roles
):
    jan = post_run(session, owner_user, bank, "2026-01")
    opening_credit(session, owner_user, chart, payroll_roles, "3000.00", date(2026, 4, 1))
    pay(session, owner_user, bank, "irs_941", D("100.00"), date(2026, 2, 20))
    got = agency(payroll_liability_report(session, date(2026, 3, 1)), "irs_941")

    assert got.periods[0].paid == D("100.00")  # no opening bucket yet on that date
    assert got.other == ZERO and got.balance == fed(jan) - D("100.00")


def test_other_agencies_opening_amounts_are_kept_apart(
    session, owner_user, bank, chart, payroll_roles
):
    opening_credit(session, owner_user, chart, payroll_roles, "3000.00")
    jan = post_run(session, owner_user, bank, "2026-01")
    pay(session, owner_user, bank, "ga_dor", jan.ga_withholding, date(2026, 2, 10))
    report = payroll_liability_report(session, date(2026, 3, 1))

    assert period(report, "ga_dor", "2026-01").unpaid == ZERO  # no opening on GA withholding
    assert agency(report, "irs_941").other == D("3000.00")
