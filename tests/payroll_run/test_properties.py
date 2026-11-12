"""Property tests (Hypothesis) for payroll runs, tax payments and the liability report.

Runs and payments are built only through the real services. The services commit, so a
savepoint cannot isolate examples; the fixture commits a fresh world per example and examples
accumulate inside the test's outer transaction (rolled back at the end of the test), exactly
like ``tests/expenses/test_properties.py``.

Three properties:

1. For any generated inputs, approving a run posts one balanced entry whose lines equal the
   stored figures, and net pay is credited to the chosen account.
2. ``calc_unemployment`` summed over a year of monthly wages: the taxable wages add up to
   exactly ``min(total wages, base)``; the tax is rounded half-up to the cent on each run's
   taxable wages, so the yearly sum differs from the unrounded figure by at most half a cent
   per run that has taxable wages.
3. A random sequence of save, approve, void, delete-draft, tax payment and void-payment
   operations keeps the books consistent. The model decides, from its own records, which
   operations must be refused and with which error class; a refusal must leave the ledger and
   the documents exactly as they were.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from sqlmodel import col, select

from payroll.ledger.accounts import PAYROLL_ROLES, AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import Account, JournalEntry, JournalLine, SystemRole
from payroll.payroll_run.exceptions import (
    InvalidPayrollRunError,
    InvalidTaxPaymentError,
    PayrollRunAlreadyExistsError,
    PayrollRunStateError,
    TaxPaymentError,
)
from payroll.payroll_run.ledger_models import TaxPayment
from payroll.payroll_run.liability import payroll_liability_report
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.posting import PayrollPostingService
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService
from payroll.payroll_run.settings_service import PayrollSettingsService
from payroll.payroll_run.tax_payments import AGENCY_ROLES, TaxPaymentService
from payroll.reports.statements import ReportService
from payroll.tax.constants import TAX_YEAR
from payroll.tax.unemployment import UnemploymentRates, calc_unemployment

D = Decimal
ZERO = D("0.00")
HALF_CENT = D("0.005")
CENT = D("0.01")
FAR = date(2030, 12, 31)  # after every date a generated operation can use
SETTINGS = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

RESET = (
    "TRUNCATE tax_payment, payroll_run, payroll_settings, payroll_deposit_rules, attachment, "
    "expense_line, expense, reimbursement, transfer, vendor, "
    "application_income, payment_application, invoice_line, invoice, deposit, payment, "
    "customer_refund, customer, item, journal_line, journal_entry, account"
)

AGENCIES = tuple(AGENCY_ROLES)  # irs_941, irs_futa, ga_dor, ga_dol
RATES = UnemploymentRates(D("0.006"), D("7000.00"), D("0.0264"), D("9500.00"))


def _cents(value: int) -> Decimal:
    return D(value) / 100


@pytest.fixture
def world(session, owner_user):
    """A factory: each call wipes the books and builds a fresh committed chart and settings."""

    def fresh():
        session.commit()  # the owner fixture row must survive a service rollback
        session.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(sa.text("SET session_replication_role = replica"))
        session.execute(sa.text(RESET))
        session.execute(sa.text("SET session_replication_role = DEFAULT"))
        session.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        seed_default_chart(session, actor=owner_user)
        PayrollSettingsService(session).set_year(
            actor=owner_user,
            tax_year=TAX_YEAR,
            futa_rate=RATES.futa_rate,
            futa_wage_base=RATES.futa_wage_base,
            suta_rate=RATES.suta_rate,
            suta_wage_base=RATES.suta_wage_base,
        )
        chart = {node.full_name: node.account for node in AccountService(session).tree()}
        session.commit()
        return Model(session, owner_user, chart)

    return fresh


# ---------------------------------------------------------------------------
# Independent figures, summed straight from the stored run columns
# ---------------------------------------------------------------------------


def run_liability(agency: str, run: PayrollRun) -> Decimal:
    if agency == "irs_941":
        return (
            run.federal_withholding
            + run.ss_ee
            + run.total_medicare_ee
            + run.ss_er
            + run.medicare_er
        )
    return {"ga_dor": run.ga_withholding, "irs_futa": run.futa, "ga_dol": run.suta}[agency]


def expected_lines(run: PayrollRun) -> dict[str, tuple[Decimal, Decimal]]:
    """role-or-'net_pay' -> (debit, credit) the approved run must post, zero lines left out."""
    health = run.health_insurance if run.health_in_income_tax else ZERO
    wages = run.gross_salary + health + run.hsa_contribution
    federal = (
        run.federal_withholding + run.ss_ee + run.total_medicare_ee + run.ss_er + run.medicare_er
    )
    lines = {
        SystemRole.PAYROLL_WAGES.value: (wages, ZERO),
        SystemRole.PAYROLL_TAX_EXPENSE.value: (
            run.total_employer_taxes + run.futa + run.suta,
            ZERO,
        ),
        SystemRole.PAYROLL_FEDERAL_PAYABLE.value: (ZERO, federal),
        SystemRole.PAYROLL_GA_WH_PAYABLE.value: (ZERO, run.ga_withholding),
        SystemRole.PAYROLL_FUTA_PAYABLE.value: (ZERO, run.futa),
        SystemRole.PAYROLL_SUTA_PAYABLE.value: (ZERO, run.suta),
        SystemRole.PAYROLL_HEALTH_PAYABLE.value: (ZERO, health),
        SystemRole.PAYROLL_HSA_PAYABLE.value: (ZERO, run.hsa_contribution),
        "net_pay": (ZERO, run.net_pay),
    }
    return {name: dc for name, dc in lines.items() if dc != (ZERO, ZERO)}


def role_balance(session, role: SystemRole) -> Decimal:
    """Debit minus credit of the account bound to ``role``."""
    value = session.execute(
        sa.text(
            "SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM journal_line l "
            "JOIN account a ON a.id = l.account_id WHERE a.system_role = :role"
        ),
        {"role": role.value},
    ).scalar_one()
    return D(value)


def account_balance(session, account_id: int) -> Decimal:
    value = session.execute(
        sa.text("SELECT COALESCE(SUM(debit - credit), 0) FROM journal_line WHERE account_id = :id"),
        {"id": account_id},
    ).scalar_one()
    return D(value)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class Model:
    """Drives random activity through the services and keeps its own independent records.

    Whether an operation succeeds, and with which error class it is refused, is decided by the
    model from its own records of statuses and periods, never by the service. A call that the
    model expects to succeed must succeed; a call it expects to be refused must raise exactly
    the predicted class, and leave every row as it was.
    """

    def __init__(self, session, actor, chart):
        self.session = session
        self.actor = actor
        self.chart = chart
        self.bank = chart["Business Checking"]
        self.runs: dict[int, dict] = {}  # id -> {period, status, pay_date}
        self.payments: dict[int, dict] = {}  # id -> {agency, amount, date, status}
        self.refusals = 0
        self.successes: Counter[str] = Counter()
        self.report_events = False  # hypothesis.event only works inside a @given test

    @staticmethod
    def _pick(seq, i):
        return seq[i % len(seq)] if seq else None

    def snapshot(self):
        """Everything a refused operation must leave untouched."""
        s = self.session
        lines = s.exec(
            select(JournalLine.id, JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        ).all()
        entries = s.exec(select(JournalEntry.id, JournalEntry.status)).all()
        runs = s.exec(
            select(
                PayrollRun.id,
                PayrollRun.status,
                PayrollRun.pay_date,
                PayrollRun.entry_id,
                PayrollRun.void_entry_id,
                PayrollRun.net_pay_account_id,
            )
        ).all()
        payments = s.exec(
            select(TaxPayment.id, TaxPayment.status, TaxPayment.entry_id, TaxPayment.void_entry_id)
        ).all()
        return tuple(sorted(tuple(r) for r in rows) for rows in (lines, entries, runs, payments))

    def _try(self, kind, fn, *, refused_with=None):
        """Run a service call.

        ``refused_with`` is the one exception class the model predicts, or None when the call
        must succeed. Any other outcome fails the test. A refusal must leave every row as it
        was. A success is counted and reported to Hypothesis.
        """
        self.session.expire_all()
        before = self.snapshot()
        if refused_with is None:
            result = fn()
            self.successes[kind] += 1
            if self.report_events:
                event(kind)
            return result
        with pytest.raises(refused_with):
            fn()
        self.session.rollback()
        self.refusals += 1
        assert self.snapshot() == before, f"{kind} was refused but changed rows"
        return None

    # ------------------------------------------------------------ model reads

    def _live(self):
        return [(i, r) for i, r in self.runs.items() if r["status"] != "void"]

    def _row(self, run_id) -> PayrollRun:
        self.session.expire_all()
        return self.session.get(PayrollRun, run_id)

    def _latest_earlier(self, run_id):
        period = self.runs[run_id]["period"]
        earlier = [(i, r) for i, r in self._live() if r["period"] < period]
        return max(earlier, key=lambda t: t[1]["period"], default=None)

    # ------------------------------------------------------------- operations

    def run(self, op):
        getattr(self, f"op_{op[0]}")(*op[1:])

    def op_save(self, month, gross_cents, health_pct, hsa_pct, health_in_tax):
        period = f"{TAX_YEAR}-{month + 1:02d}"
        live = [r["period"] for _i, r in self._live()]
        refused = None
        if period in live:
            refused = PayrollRunAlreadyExistsError
        elif any(p > period for p in live):
            refused = PayrollRunStateError
        payload = PayrollInput(
            pay_period=period,
            gross_salary=_cents(gross_cents),
            health_insurance=_cents(gross_cents * health_pct // 100),
            hsa_contribution=_cents(gross_cents * hsa_pct // 100),
            health_in_income_tax=health_in_tax,
            use_previous_ytd_fica=True,
            save_run=True,
        )
        result = self._try(
            "save",
            lambda: PayrollRunService(self.session).calculate(payload, actor=self.actor),
            refused_with=refused,
        )
        if refused is None:
            assert result is not None and result.id is not None and result.status == "draft"
            self.runs[result.id] = {"period": period, "status": "draft", "pay_date": None}

    def op_approve(self, i, offset, to_owner):
        run_id = self._pick(list(self.runs), i)
        if run_id is None:
            return
        info = self.runs[run_id]
        start = date(int(info["period"][:4]), int(info["period"][5:]), 1)
        pay_date = start + timedelta(days=offset)  # negative: before the period, refused
        earlier = self._latest_earlier(run_id)
        row = self._row(run_id)
        refused = None
        if info["status"] != "draft":
            refused = PayrollRunStateError
        elif pay_date < start:
            refused = InvalidPayrollRunError
        elif any(
            r["status"] == "draft" and r["period"] < info["period"] for r in self.runs.values()
        ):
            refused = PayrollRunStateError
        elif earlier is not None and row.ytd_fica_before != self._row(earlier[0]).ytd_fica_after:
            refused = InvalidPayrollRunError
        run = self._try(
            "approve",
            lambda: PayrollPostingService(self.session).approve(
                run_id,
                actor=self.actor,
                pay_date=pay_date,
                net_pay_source="owner" if to_owner else "account",
                net_pay_account_id=None if to_owner else self.bank.id,
            ),
            refused_with=refused,
        )
        if refused is None:
            assert run.status == "approved" and run.pay_date == pay_date
            info.update(status="approved", pay_date=pay_date)

    def op_void(self, i, offset):
        run_id = self._pick(list(self.runs), i)
        if run_id is None:
            return
        info = self.runs[run_id]
        base = info["pay_date"] or date(int(info["period"][:4]), int(info["period"][5:]), 1)
        void_date = base + timedelta(days=offset)  # negative: before the pay date, refused
        later = [r for _i, r in self._live() if r["period"] > info["period"]]
        refused = None
        if info["status"] != "approved":
            refused = PayrollRunStateError
        elif void_date < base:
            refused = InvalidPayrollRunError
        elif later:  # any later non-void run, draft or approved
            refused = PayrollRunStateError
        run = self._try(
            "void",
            lambda: PayrollPostingService(self.session).void(
                run_id, actor=self.actor, void_date=void_date
            ),
            refused_with=refused,
        )
        if refused is None:
            assert run.status == "void" and not later
            info["status"] = "void"

    def op_delete(self, i):
        run_id = self._pick(list(self.runs), i)
        if run_id is None:
            return
        refused = None if self.runs[run_id]["status"] == "draft" else PayrollRunStateError
        self._try(
            "delete",
            lambda: PayrollRunService(self.session).delete_run(run_id, actor=self.actor),
            refused_with=refused,
        )
        if refused is None:
            del self.runs[run_id]
            self.session.expire_all()
            assert self.session.get(PayrollRun, run_id) is None

    def op_pay(self, agency, day, cents, bad_bank):
        when = date(TAX_YEAR, 1, 1) + timedelta(days=day)
        # A payroll account cannot be the bank of a tax payment.
        bank_id = (
            self.chart["Payroll Liabilities:S-Corp Health Insurance"].id
            if bad_bank
            else self.bank.id
        )
        payment = self._try(
            "pay",
            lambda: TaxPaymentService(self.session).create(
                actor=self.actor,
                payment_date=when,
                agency=AGENCIES[agency % 4],
                bank_account_id=bank_id,
                amount=_cents(cents),
            ),
            refused_with=InvalidTaxPaymentError if bad_bank else None,
        )
        if payment is not None:
            assert payment.status == "posted"
            self.payments[payment.id] = {
                "agency": AGENCIES[agency % 4],
                "amount": _cents(cents),
                "date": when,
                "status": "posted",
            }

    def op_void_payment(self, i, offset):
        payment_id = self._pick(list(self.payments), i)
        if payment_id is None:
            return
        info = self.payments[payment_id]
        void_date = info["date"] + timedelta(days=offset)
        refused = None
        if info["status"] != "posted":
            refused = TaxPaymentError
        elif void_date < info["date"]:
            refused = InvalidTaxPaymentError
        payment = self._try(
            "void_payment",
            lambda: TaxPaymentService(self.session).void(
                payment_id, actor=self.actor, void_date=void_date
            ),
            refused_with=refused,
        )
        if refused is None:
            assert payment.status == "void"
            info["status"] = "void"


# ---------------------------------------------------------------------------
# Invariants, checked after every operation
# ---------------------------------------------------------------------------


def check_invariants(model: Model) -> None:
    session = model.session
    session.expire_all()
    reports = ReportService(session)
    for as_of in (date(2026, 6, 30), date(2026, 12, 31), FAR):
        trial = reports.trial_balance(as_of)
        assert trial.balanced and trial.total_debit == trial.total_credit

    runs = session.exec(select(PayrollRun).order_by(col(PayrollRun.pay_period))).all()
    payments = session.exec(select(TaxPayment)).all()
    # The model's records agree with the database.
    assert {r.id: r.status for r in runs} == {i: r["status"] for i, r in model.runs.items()}
    assert {p.id: p.status for p in payments} == {i: p["status"] for i, p in model.payments.items()}

    approved = [r for r in runs if r.status == "approved"]
    # No run is approved while an earlier run of its year is a draft, and an approved run
    # continues the year-to-date wages of the latest earlier non-void run.
    for run in approved:
        earlier = [e for e in runs if e.pay_period < run.pay_period and e.status != "void"]
        assert all(e.status != "draft" for e in earlier), run.pay_period
        if earlier:
            assert run.ytd_fica_before == earlier[-1].ytd_fica_after, run.pay_period
    # One non-void run per period.
    live_periods = [r.pay_period for r in runs if r.status != "void"]
    assert len(live_periods) == len(set(live_periods))

    posted = [p for p in payments if p.status == "posted"]
    report = payroll_liability_report(session, FAR)
    for key in AGENCIES:
        liability = sum((run_liability(key, r) for r in approved), ZERO)
        paid = sum((p.amount for p in posted if p.agency == key), ZERO)
        # Voided runs and payments reverse to nothing: the ledger balance is what is left.
        assert -role_balance(session, AGENCY_ROLES[key]) == liability - paid
        row = next(a for a in report.agencies if a.agency == key)
        unpaid = sum((p.unpaid for p in row.periods), ZERO)
        assert row.balance == liability - paid
        # Payments are applied oldest period first, so what is unpaid is exactly the
        # liability less the payments, never below zero; any overpayment is `other`.
        assert unpaid == max(liability - paid, ZERO)
        assert row.other == row.balance - unpaid == -max(paid - liability, ZERO)
        if paid <= liability:
            assert row.other == ZERO  # no manual journals in this world
        assert [p.pay_period for p in row.periods] == sorted(p.pay_period for p in row.periods)
    # Wages, the other payables and the accounts net pay is paid from, summed from the runs.
    assert role_balance(session, SystemRole.PAYROLL_WAGES) == sum(
        (expected_lines(r)[SystemRole.PAYROLL_WAGES.value][0] for r in approved), ZERO
    )
    assert role_balance(session, SystemRole.PAYROLL_TAX_EXPENSE) == sum(
        (r.total_employer_taxes + r.futa + r.suta for r in approved), ZERO
    )
    assert -role_balance(session, SystemRole.PAYROLL_HEALTH_PAYABLE) == sum(
        (r.health_insurance for r in approved if r.health_in_income_tax), ZERO
    )
    assert -role_balance(session, SystemRole.PAYROLL_HSA_PAYABLE) == sum(
        (r.hsa_contribution for r in approved), ZERO
    )
    assert -role_balance(session, SystemRole.DUE_TO_OWNER) == sum(
        (r.net_pay for r in approved if r.net_pay_source == "owner"), ZERO
    )
    assert account_balance(session, model.bank.id) == -sum(
        (r.net_pay for r in approved if r.net_pay_source == "account"), ZERO
    ) - sum((p.amount for p in posted), ZERO)
    # Each approved run's entry equals its stored figures line by line.
    for run in approved:
        assert_entry_matches(session, run)


def assert_entry_matches(session, run: PayrollRun) -> None:
    """The run's entry has one line per non-zero stored figure and balances."""
    rows = session.exec(
        select(JournalLine.debit, JournalLine.credit, Account.system_role)
        .join(Account, col(Account.id) == col(JournalLine.account_id))
        .where(JournalLine.entry_id == run.entry_id)
    ).all()
    payroll_roles = {role.value for role in PAYROLL_ROLES}
    posted: dict[str, tuple[Decimal, Decimal]] = {}
    for debit, credit, role in rows:
        name = role if role in payroll_roles else "net_pay"  # the bank or Due to Owner
        assert name not in posted, f"two lines for {name}"
        posted[name] = (debit, credit)
    assert posted == expected_lines(run)
    assert sum((d for d, _c in posted.values()), ZERO) == sum(
        (c for _d, c in posted.values()), ZERO
    )
    assert posted["net_pay"][1] == run.net_pay


# ---------------------------------------------------------------------------
# Property 3: random activity
# ---------------------------------------------------------------------------

month = st.integers(min_value=0, max_value=11)
index = st.integers(min_value=0, max_value=30)
operation = st.one_of(
    st.tuples(
        st.just("save"),
        month,
        st.integers(min_value=100, max_value=4_000_000),  # gross in cents, to $40,000
        st.integers(min_value=0, max_value=25),  # health, percent of gross
        st.integers(min_value=0, max_value=10),  # HSA, percent of gross
        st.booleans(),
    ),
    st.tuples(st.just("approve"), index, st.integers(min_value=-3, max_value=40), st.booleans()),
    st.tuples(st.just("void"), index, st.integers(min_value=-3, max_value=40)),
    st.tuples(st.just("delete"), index),
    st.tuples(
        st.just("pay"),
        st.integers(min_value=0, max_value=3),
        st.integers(min_value=0, max_value=400),
        st.integers(min_value=1, max_value=400_000),
        st.sampled_from([False] * 7 + [True]),  # True: a payroll account as the bank, refused
    ),
    st.tuples(st.just("void_payment"), index, st.integers(min_value=-3, max_value=30)),
)
activity = st.lists(operation, min_size=1, max_size=18)

OP_KINDS = {"save", "approve", "void", "delete", "pay", "void_payment"}


def save(month_, gross=800_000, health=5, hsa=3, in_tax=True):
    return ("save", month_, gross, health, hsa, in_tax)


# Whole-year scripts that exercise every operation kind, refusals in between.
EXAMPLE_SEQUENCES = [
    [
        save(0),
        save(0),  # refused: the period exists
        save(1),
        ("approve", 1, 20, False),  # refused: January is a draft
        ("approve", 0, -1, False),  # refused: before the period
        ("approve", 0, 30, False),
        ("approve", 0, 30, False),  # refused: already approved
        ("approve", 1, 28, True),  # net pay to Due to Owner
        ("void", 0, 0),  # refused: February is approved after it
        ("void", 1, -1),  # refused: before the pay date
        ("pay", 0, 60, 12_345, False),
        ("pay", 2, 60, 99, True),  # refused: a payroll account as the bank
        ("void_payment", 0, -1),  # refused: before the payment
        ("void_payment", 0, 2),
        ("void_payment", 0, 2),  # refused: already void
        ("void", 1, 0),
        ("void", 1, 0),  # refused: already void
        save(1, gross=300_000, health=0, hsa=0),  # the voided period is saved afresh
        save(3),
        save(2),  # refused: April is later
        ("void", 0, 1),  # refused: a later draft exists
        ("delete", 1),  # the draft April is deleted ... (index is among all runs)
        ("delete", 0),  # refused: approved
    ],
    # A draft deleted from the middle leaves the next draft with stale year-to-date wages.
    [
        save(0, gross=3_000_000),
        ("approve", 0, 5, False),
        save(1, gross=3_000_000),
        save(2, gross=3_000_000),
        ("delete", 1),
        ("approve", 1, 5, False),  # refused: out of date
        ("pay", 1, 100, 500_000, False),  # an overpayment to FUTA
    ],
]


@SETTINGS
@example(ops=EXAMPLE_SEQUENCES[0])
@example(ops=EXAMPLE_SEQUENCES[1])
@given(ops=activity)
def test_random_activity_keeps_the_books_consistent(world, ops):
    model = world()
    model.report_events = True
    for op in ops:
        model.run(op)
        check_invariants(model)


def test_the_explicit_examples_run_every_operation_successfully(world):
    """Not left to luck: the committed @example sequences succeed at least once per op kind."""
    total = Counter()
    refusals = 0
    for ops in EXAMPLE_SEQUENCES:
        model = world()
        for op in ops:
            model.run(op)
            check_invariants(model)
        total.update(model.successes)
        refusals += model.refusals
    assert set(total) == OP_KINDS, OP_KINDS - set(total)
    assert refusals >= 10


# ---------------------------------------------------------------------------
# Property 1: the posted entry for any generated run
# ---------------------------------------------------------------------------


@SETTINGS
@example(
    gross=800_000,
    health_pct=10,
    hsa_pct=5,
    health_in_tax=True,
    hsa_in_tax=False,
    ytd=0,
    to_owner=False,
)
@example(
    gross=800_000,
    health_pct=0,
    hsa_pct=0,
    health_in_tax=False,
    hsa_in_tax=True,
    ytd=20_000_000,
    to_owner=True,
)  # past the Social Security cap
@example(
    gross=100, health_pct=0, hsa_pct=0, health_in_tax=True, hsa_in_tax=False, ytd=0, to_owner=False
)  # tiny: most lines are zero and dropped
@given(
    gross=st.integers(min_value=100, max_value=6_000_000),
    health_pct=st.integers(min_value=0, max_value=25),
    hsa_pct=st.integers(min_value=0, max_value=10),
    health_in_tax=st.booleans(),
    hsa_in_tax=st.booleans(),
    ytd=st.integers(min_value=0, max_value=30_000_000),
    to_owner=st.booleans(),
)
def test_any_run_posts_one_balanced_entry_equal_to_its_stored_figures(
    world, gross, health_pct, hsa_pct, health_in_tax, hsa_in_tax, ytd, to_owner
):
    model = world()
    session = model.session
    result = PayrollRunService(session).calculate(
        PayrollInput(
            pay_period=f"{TAX_YEAR}-05",
            gross_salary=_cents(gross),
            health_insurance=_cents(gross * health_pct // 100),
            hsa_contribution=_cents(gross * hsa_pct // 100),
            health_in_income_tax=health_in_tax,
            hsa_in_income_tax=hsa_in_tax,
            ytd_fica_wages=_cents(ytd),
            save_run=True,
        ),
        actor=model.actor,
    )
    run = PayrollPostingService(session).approve(
        result.id,
        actor=model.actor,
        pay_date=date(TAX_YEAR, 5, 31),
        net_pay_source="owner" if to_owner else "account",
        net_pay_account_id=None if to_owner else model.bank.id,
    )
    assert run.net_pay == result.tax_detail.net_pay > 0
    lines = session.exec(select(JournalLine).where(JournalLine.entry_id == run.entry_id)).all()
    debits = sum((ln.debit for ln in lines), ZERO)
    credits = sum((ln.credit for ln in lines), ZERO)
    assert debits == credits > 0
    # Every line is a non-zero stored figure; each account appears once; net pay goes where chosen.
    assert all((ln.debit == 0) != (ln.credit == 0) for ln in lines)
    by_account = {ln.account_id: ln for ln in lines}
    assert len(by_account) == len(lines)
    net_account = (
        AccountService(session).account_by_role(SystemRole.DUE_TO_OWNER).id
        if to_owner
        else model.bank.id
    )
    assert by_account[net_account].credit == run.net_pay
    assert_entry_matches(session, run)
    assert len(lines) == len(expected_lines(run))
    model.runs[run.id] = {"period": run.pay_period, "status": "approved", "pay_date": run.pay_date}
    check_invariants(model)


# ---------------------------------------------------------------------------
# Property 2: unemployment tax over a year
# ---------------------------------------------------------------------------


def half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@SETTINGS
@example(
    wages=[800_000] * 12, futa_micro=6000, futa_base=700_000, suta_micro=26_400, suta_base=950_000
)
@example(wages=[500] * 12, futa_micro=6000, futa_base=700_000, suta_micro=26_400, suta_base=950_000)
@example(wages=[0] * 11 + [700_000], futa_micro=6000, futa_base=700_000, suta_micro=0, suta_base=1)
@example(wages=[1] * 12, futa_micro=200_000, futa_base=10, suta_micro=1, suta_base=10_000_000)
@given(
    wages=st.lists(st.integers(min_value=0, max_value=3_000_000), min_size=1, max_size=12),
    futa_micro=st.integers(min_value=0, max_value=200_000),
    futa_base=st.integers(min_value=1, max_value=10_000_000),
    suta_micro=st.integers(min_value=0, max_value=200_000),
    suta_base=st.integers(min_value=1, max_value=10_000_000),
)
def test_unemployment_over_a_year_never_exceeds_the_base_and_rounds_per_run(
    wages, futa_micro, futa_base, suta_micro, suta_base
):
    """The rule: each run's taxable wages are exact cents; its tax is that figure times the
    rate rounded half-up to the cent. So the wages add up exactly to min(total, base) and the
    taxes differ from the unrounded yearly figure by at most half a cent per taxable run."""
    rates = UnemploymentRates(
        D(futa_micro) / 1_000_000, _cents(futa_base), D(suta_micro) / 1_000_000, _cents(suta_base)
    )
    total = _cents(sum(wages))
    ytd = ZERO
    results = []
    for cents in wages:
        after = ytd + _cents(cents)
        results.append(calc_unemployment(ytd, after, rates))
        ytd = after
    one_shot = calc_unemployment(ZERO, total, rates)

    for wages_attr, tax_attr, base, rate in (
        ("futa_wages", "futa", rates.futa_wage_base, rates.futa_rate),
        ("suta_wages", "suta", rates.suta_wage_base, rates.suta_rate),
    ):
        taxable = [getattr(r, wages_attr) for r in results]
        taxes = [getattr(r, tax_attr) for r in results]
        assert all(t >= 0 for t in taxable) and all(t >= 0 for t in taxes)
        assert sum(taxable, ZERO) == min(total, base) == getattr(one_shot, wages_attr)
        # Per run: half-up on that run's taxable wages, and nothing for a run with none.
        assert taxes == [half_up(t * rate) for t in taxable]
        assert all(tax == 0 for t, tax in zip(taxable, taxes, strict=True) if t == 0)
        runs_taxed = sum(1 for t in taxable if t > 0)
        exact = min(total, base) * rate
        bound = HALF_CENT * runs_taxed
        assert abs(sum(taxes, ZERO) - exact) <= bound
        # Never above base x rate by more than the rounding allowance.
        assert sum(taxes, ZERO) <= base * rate + bound
        # The one-shot figure carries its own half-cent of rounding.
        assert abs(sum(taxes, ZERO) - getattr(one_shot, tax_attr)) <= bound + HALF_CENT
        if runs_taxed <= 1:  # a single taxable run is the one-shot figure exactly
            assert sum(taxes, ZERO) == getattr(one_shot, tax_attr)
