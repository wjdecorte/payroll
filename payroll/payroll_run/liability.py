"""
Payroll liability report (M4 design spec §8): read-only and derived, nothing is stored.

Per agency: the ledger balance on the payable account, the unpaid amount of each pay period
(approved runs, oldest first, less the tax payments applied oldest first) with its due date,
and an ``other`` line for whatever part of the balance the periods do not explain (opening
balance, manual journal, overpayment). ``other`` is never aged or hidden.
"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlmodel import Session, col, func, or_, select

from payroll.ledger.models import Account, JournalEntry, JournalLine
from payroll.payroll_run.ledger_models import PayrollDepositRules, TaxPayment
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.tax_payments import AGENCY_ROLES

ZERO = Decimal("0.00")

# Report order, label, and the prefix of the agency's columns on ``PayrollDepositRules``.
AGENCIES: tuple[tuple[str, str, str], ...] = (
    ("irs_941", "IRS (Form 941)", "irs_941"),
    ("irs_futa", "IRS (FUTA)", "futa"),
    ("ga_dor", "Georgia DOR (withholding)", "ga_dor"),
    ("ga_dol", "Georgia DOL (SUTA)", "ga_dol"),
)


@dataclass(frozen=True)
class PeriodLiability:
    pay_period: str
    due_date: date
    liability: Decimal
    paid: Decimal
    unpaid: Decimal
    overdue: bool


@dataclass(frozen=True)
class AgencyLiability:
    agency: str
    label: str
    balance: Decimal
    periods: list[PeriodLiability]
    other: Decimal
    next_due: date | None


@dataclass(frozen=True)
class LiabilityReport:
    as_of: date
    agencies: list[AgencyLiability]


def _run_liability(agency: str, run: PayrollRun) -> Decimal:
    if agency == "irs_941":
        return (
            run.federal_withholding
            + run.ss_ee
            + run.total_medicare_ee
            + run.ss_er
            + run.medicare_er
        )
    if agency == "ga_dor":
        return run.ga_withholding
    if agency == "irs_futa":
        return run.futa
    return run.suta


def _due_date(pay_period: str, cadence: str, due_day: int) -> date:
    year, month = int(pay_period[:4]), int(pay_period[5:7])
    if cadence == "quarterly":
        month = (month + 2) // 3 * 3  # the quarter's last month
    month += 1
    if month > 12:
        year, month = year + 1, 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last if due_day == 0 else min(due_day, last))


def _balance(session: Session, account_id: int, as_of: date) -> Decimal:
    stmt = (
        select(func.coalesce(func.sum(JournalLine.credit - JournalLine.debit), 0))
        .join(JournalEntry, col(JournalEntry.id) == col(JournalLine.entry_id))
        .where(JournalLine.account_id == account_id, JournalEntry.entry_date <= as_of)
    )
    return Decimal(session.exec(stmt).one()).quantize(ZERO)


def _in_force_runs(session: Session, as_of: date) -> list[PayrollRun]:
    """Approved runs paid on or before ``as_of``, plus runs voided after ``as_of`` (still in
    force on that date, which is what the ledger balance shows)."""
    stmt = (
        select(PayrollRun)
        .where(
            col(PayrollRun.pay_date) <= as_of,
            or_(
                PayrollRun.status == "approved",
                (PayrollRun.status == "void") & (col(PayrollRun.void_date) > as_of),
            ),
        )
        .order_by(col(PayrollRun.pay_period), col(PayrollRun.id))
    )
    return list(session.exec(stmt).all())


def _payment_total(session: Session, agency: str, as_of: date) -> Decimal:
    stmt = select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
        TaxPayment.agency == agency,
        TaxPayment.payment_date <= as_of,
        or_(
            TaxPayment.status == "posted",
            (TaxPayment.status == "void") & (col(TaxPayment.void_date) > as_of),
        ),
    )
    return Decimal(session.exec(stmt).one()).quantize(ZERO)


def _rules(session: Session) -> PayrollDepositRules:
    """The stored rules, or the model defaults when the row is absent (nothing is inserted:
    this is a read path)."""
    row = session.get(PayrollDepositRules, 1)
    return row if row is not None else PayrollDepositRules()


def payroll_liability_report(session: Session, as_of: date) -> LiabilityReport:
    rules = _rules(session)
    runs = _in_force_runs(session, as_of)
    agencies: list[AgencyLiability] = []
    for agency, label, prefix in AGENCIES:
        account_id = session.exec(
            select(Account.id).where(Account.system_role == AGENCY_ROLES[agency].value)
        ).first()
        if account_id is None:
            agencies.append(AgencyLiability(agency, label, ZERO, [], ZERO, None))
            continue
        balance = _balance(session, account_id, as_of)
        cadence = getattr(rules, f"{prefix}_cadence")
        due_day = getattr(rules, f"{prefix}_due_day")
        remaining = _payment_total(session, agency, as_of)
        periods: list[PeriodLiability] = []
        for run in runs:
            liability = _run_liability(agency, run).quantize(ZERO)
            if liability <= 0:
                continue
            paid = min(liability, remaining)
            remaining -= paid
            unpaid = liability - paid
            due = _due_date(run.pay_period, cadence, due_day)
            periods.append(
                PeriodLiability(
                    run.pay_period, due, liability, paid, unpaid, unpaid > 0 and due < as_of
                )
            )
        unpaid_total = sum((p.unpaid for p in periods), ZERO)
        next_due = min((p.due_date for p in periods if p.unpaid > 0), default=None)
        agencies.append(
            AgencyLiability(agency, label, balance, periods, balance - unpaid_total, next_due)
        )
    return LiabilityReport(as_of, agencies)
