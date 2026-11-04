"""Property tests (Hypothesis) for receivables, against the real Postgres.

Random activity is built through the real services (``InvoiceService`` issue/void,
``PaymentService`` record/apply/void, ``DepositService``, ``RefundService``). The receivables
services commit, so a savepoint cannot isolate examples; instead the fixtures are committed once
and examples accumulate inside the test's outer transaction (rolled back at the end of the
test). Every property is a global invariant, so accumulation does not weaken it.

Operations the services legitimately refuse (over-application, back-dated negative credit,
state errors, ...) raise ``AppBaseError`` and the model simply skips them. The model keeps its
own books (invoice totals, live applications) so expected values never come from the code
under test.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from payroll.exceptions import AppBaseError
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import AccountType
from payroll.receivables.balances import ar_aging
from payroll.receivables.credits import RefundService
from payroll.receivables.deposits import DepositService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.models import Customer, Item
from payroll.receivables.payments import ApplicationInput, PaymentService
from payroll.reports.basis import Basis
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService

D = Decimal
ZERO = D("0.00")
YEAR_START = date(2026, 1, 1)
YEAR_END = date(2026, 12, 31)
YEAR = Period(YEAR_START, YEAR_END, "2026")
SETTINGS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

day = st.integers(min_value=0, max_value=330).map(lambda n: YEAR_START + timedelta(days=n))
cents = st.integers(min_value=1, max_value=400_000).map(lambda n: D(n) / 100)
index = st.integers(min_value=0, max_value=50)
operation = st.one_of(
    st.tuples(st.just("invoice"), index, day, cents, st.integers(1, 3)),
    st.tuples(st.just("pay"), index, day, cents, st.booleans(), st.booleans()),
    st.tuples(st.just("apply"), index, index, cents),
    st.tuples(st.just("void_payment"), index, day),
    st.tuples(st.just("void_invoice"), index, day),
    st.tuples(st.just("deposit"), index, day),
    st.tuples(st.just("void_deposit"), index, day),
    st.tuples(st.just("refund"), index, day, cents),
    st.tuples(st.just("void_refund"), index, day),
)
activity = st.lists(operation, min_size=1, max_size=14)


RESET = (
    "TRUNCATE application_income, payment_application, invoice_line, invoice, deposit, payment, "
    "customer_refund, customer, item, journal_line, journal_entry, account"
)


@pytest.fixture
def world(session, owner_user):
    """A factory: each call wipes the ledger and receivables and builds a fresh committed world.

    TRUNCATE is transactional in Postgres, so the test's outer rollback restores everything.
    """

    def fresh():
        session.commit()  # the fixture rows (the owner) must survive a service rollback
        # The ledger tables reject TRUNCATE through triggers; replica mode skips them (the test
        # database user is a superuser) for this one statement.
        session.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))  # run deferred checks first
        session.execute(sa.text("SET session_replication_role = replica"))
        session.execute(sa.text(RESET))
        session.execute(sa.text("SET session_replication_role = DEFAULT"))
        session.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        seed_default_chart(session, actor=owner_user)
        chart = {node.full_name: node.account for node in AccountService(session).tree()}
        training = AccountService(session).create(
            actor=owner_user, name="Training", type=AccountType.INCOME, number="4200"
        )
        customers = [Customer(name="Acme"), Customer(name="Birch")]
        items = [
            Item(
                name="Consulting", unit_price=ZERO, income_account_id=chart["Services Revenue"].id
            ),
            Item(name="Training", unit_price=ZERO, income_account_id=training.id),
        ]
        session.add_all([*customers, *items])
        session.commit()
        return Model(session, owner_user, chart, customers, items)

    return fresh


class Model:
    """Drives random activity through the services and keeps independent books."""

    def __init__(self, session, actor, chart, customers, items):
        self.session = session
        self.actor = actor
        self.chart = chart
        self.customers = customers
        self.items = items
        self.invoices: list[tuple[int, Decimal]] = []  # (id, total), all issued
        self.live_invoices: dict[int, Decimal] = {}
        self.payments: dict[int, dict] = {}  # id -> {"applied": {invoice_id: amount}, "live": bool}
        self.deposits: dict[int, bool] = {}  # id -> live
        self.refunds: dict[int, bool] = {}
        self.deposit_payments: dict[int, list[int]] = {}

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _pick(seq, i):
        return seq[i % len(seq)] if seq else None

    def _try(self, fn):
        """Run a service call; a refusal (any AppBaseError) is a skipped operation."""
        try:
            return fn()
        except AppBaseError:
            self.session.rollback()
            return None

    # ------------------------------------------------------------- operations

    def run(self, op):
        getattr(self, f"op_{op[0]}")(*op[1:])

    def op_invoice(self, i, when, price, qty):
        customer = self._pick(self.customers, i)
        item = self._pick(self.items, i // 2)
        service = InvoiceService(self.session)

        def work():
            draft = service.create_draft(
                actor=self.actor,
                customer_id=customer.id,
                invoice_date=when,
                lines=[InvoiceLineInput(item.id, "Work", D(qty), price)],
            )
            return service.issue(draft.id, actor=self.actor)

        invoice = self._try(work)
        if invoice is not None:
            total = (D(qty) * price).quantize(D("0.01"))
            self.invoices.append((invoice.id, total))
            self.live_invoices[invoice.id] = total

    def op_pay(self, i, when, amount, to_bank, apply_now):
        customer = self._pick(self.customers, i)
        account = self.chart["Business Checking" if to_bank else "Undeposited Funds"]
        applications = []
        if apply_now:
            remaining = amount
            for invoice_id, total in self.invoices:
                if invoice_id not in self.live_invoices or remaining <= 0:
                    continue
                if self._owner_of(invoice_id) != customer.id:
                    continue
                balance = total - self._applied_to(invoice_id)
                take = min(balance, remaining)
                if take > 0:
                    applications.append(ApplicationInput(invoice_id, take))
                    remaining -= take
        payment = self._try(
            lambda: PaymentService(self.session).record(
                actor=self.actor,
                customer_id=customer.id,
                received_date=when,
                amount=amount,
                deposit_account_id=account.id,
                method="check",
                applications=applications,
            )
        )
        if payment is not None:
            self.payments[payment.id] = {
                "applied": {a.invoice_id: a.amount for a in applications},
                "live": True,
                "customer": customer.id,
                "bank": to_bank,
            }

    def op_apply(self, i, j, amount):
        live_payments = [p for p, v in self.payments.items() if v["live"]]
        payment_id = self._pick(live_payments, i)
        invoice = self._pick(list(self.live_invoices), j)
        if payment_id is None or invoice is None:
            return
        done = self._try(
            lambda: PaymentService(self.session).apply(
                payment_id,
                actor=self.actor,
                applications=[ApplicationInput(invoice, amount)],
            )
        )
        if done is not None:
            self.payments[payment_id]["applied"][invoice] = amount

    def op_void_payment(self, i, when):
        payment_id = self._pick([p for p, v in self.payments.items() if v["live"]], i)
        if payment_id is None:
            return
        done = self._try(
            lambda: PaymentService(self.session).void(payment_id, actor=self.actor, void_date=when)
        )
        if done is not None:
            self.payments[payment_id]["live"] = False

    def op_void_invoice(self, i, when):
        invoice_id = self._pick(list(self.live_invoices), i)
        if invoice_id is None:
            return
        done = self._try(
            lambda: InvoiceService(self.session).void(invoice_id, actor=self.actor, void_date=when)
        )
        if done is not None:
            del self.live_invoices[invoice_id]

    def op_deposit(self, i, when):
        undeposited = [
            p
            for p, v in self.payments.items()
            if v["live"] and not v["bank"] and not self._deposited(p)
        ]
        if not undeposited:
            return
        chosen = [undeposited[i % len(undeposited)]]
        deposit = self._try(
            lambda: DepositService(self.session).create(
                actor=self.actor,
                deposit_date=when,
                bank_account_id=self.chart["Business Checking"].id,
                payment_ids=chosen,
            )
        )
        if deposit is not None:
            self.deposits[deposit.id] = True
            self.deposit_payments[deposit.id] = chosen

    def op_void_deposit(self, i, when):
        deposit_id = self._pick([d for d, live in self.deposits.items() if live], i)
        if deposit_id is None:
            return
        done = self._try(
            lambda: DepositService(self.session).void(deposit_id, actor=self.actor, void_date=when)
        )
        if done is not None:
            self.deposits[deposit_id] = False

    def op_refund(self, i, when, amount):
        customer = self._pick(self.customers, i)
        refund = self._try(
            lambda: RefundService(self.session).record(
                actor=self.actor,
                customer_id=customer.id,
                refund_date=when,
                amount=amount,
                bank_account_id=self.chart["Business Checking"].id,
                method="check",
            )
        )
        if refund is not None:
            self.refunds[refund.id] = True

    def op_void_refund(self, i, when):
        refund_id = self._pick([r for r, live in self.refunds.items() if live], i)
        if refund_id is None:
            return
        done = self._try(
            lambda: RefundService(self.session).void(refund_id, actor=self.actor, void_date=when)
        )
        if done is not None:
            self.refunds[refund_id] = False

    # ------------------------------------------------------- independent books

    def _owner_of(self, invoice_id):
        return InvoiceService(self.session).get(invoice_id).customer_id

    def _applied_to(self, invoice_id):
        return sum(
            (v["applied"].get(invoice_id, ZERO) for v in self.payments.values() if v["live"]),
            ZERO,
        )

    def _deposited(self, payment_id):
        return any(
            self.deposits.get(d) and payment_id in ids for d, ids in self.deposit_payments.items()
        )

    def live_applied_total(self):
        return sum(
            (
                amount
                for v in self.payments.values()
                if v["live"]
                for amount in v["applied"].values()
            ),
            ZERO,
        )

    def void_everything(self):
        end = YEAR_END
        for deposit_id, live in list(self.deposits.items()):
            if live:
                DepositService(self.session).void(deposit_id, actor=self.actor, void_date=end)
                self.deposits[deposit_id] = False
        for refund_id, live in list(self.refunds.items()):
            if live:
                RefundService(self.session).void(refund_id, actor=self.actor, void_date=end)
                self.refunds[refund_id] = False
        for payment_id, state in list(self.payments.items()):
            if state["live"]:
                PaymentService(self.session).void(payment_id, actor=self.actor, void_date=end)
                state["live"] = False
        for invoice_id in list(self.live_invoices):
            InvoiceService(self.session).void(invoice_id, actor=self.actor, void_date=end)
            del self.live_invoices[invoice_id]


AS_OF_DATES = (date(2026, 3, 31), date(2026, 8, 15), YEAR_END, date(2027, 6, 30))


def _total(statement, label):
    return next(r.amounts[0] for r in statement.rows if r.label == label)


def _has(statement, label):
    return any(r.label == label for r in statement.rows)


def _drive(world, ops):
    model = world()
    for op in ops:
        model.run(op)
    return model


@SETTINGS
@given(ops=activity)
def test_random_receivable_activity_keeps_trial_balance_balanced(world, ops):
    model = _drive(world, ops)
    reports = ReportService(model.session)
    for as_of in AS_OF_DATES:
        trial = reports.trial_balance(as_of)
        assert trial.balanced
        assert trial.total_debit == trial.total_credit


# Regression cases: voids dated before the document are refused (the model skips them), so the
# A/R subledger keeps matching the ledger on every date.
@SETTINGS
@example(
    ops=[("invoice", 0, date(2026, 4, 1), D("0.01"), 1), ("void_invoice", 0, date(2026, 1, 1))]
)
@example(
    ops=[
        ("pay", 0, date(2026, 8, 17), D("57.64"), False, False),
        ("void_payment", 0, date(2026, 3, 9)),
    ]
)
@example(
    ops=[
        ("pay", 0, date(2026, 2, 1), D("100.00"), False, False),
        ("refund", 0, date(2026, 9, 1), D("50.00")),
        ("void_refund", 0, date(2026, 4, 1)),
    ]
)
@given(ops=activity)
def test_ar_subledger_always_equals_ledger_for_random_activity(world, ops):
    model = _drive(world, ops)
    for as_of in AS_OF_DATES:
        aging = ar_aging(model.session, as_of)
        assert aging.difference == ZERO, (as_of, aging.difference)
        assert all(i.ok for i in aging.invariants)
        assert aging.totals.credit >= ZERO
        assert all(r.credit >= ZERO for r in aging.rows)


@SETTINGS
@given(ops=activity)
def test_cash_balance_sheet_balances_for_random_activity(world, ops):
    model = _drive(world, ops)
    reports = ReportService(model.session)
    for as_of in AS_OF_DATES:
        for basis in (Basis.CASH, Basis.ACCRUAL):
            sheet = reports.balance_sheet(as_of, basis=basis)
            assert all(i.ok for i in sheet.invariants), (as_of, basis)
            assert _total(sheet, "Total Assets") == _total(sheet, "Liabilities + Equity")


@SETTINGS
@given(ops=activity)
def test_cash_income_total_equals_sum_of_applications_not_voided(world, ops):
    model = _drive(world, ops)
    pnl = ReportService(model.session).profit_and_loss(YEAR, basis=Basis.CASH)
    expected = model.live_applied_total()
    actual = _total(pnl, "Total Income") if _has(pnl, "Total Income") else ZERO
    assert actual == expected
    assert _total(pnl, "Net Income") == expected


@SETTINGS
@given(ops=activity)
def test_accrual_income_total_equals_sum_of_live_invoices(world, ops):
    model = _drive(world, ops)
    pnl = ReportService(model.session).profit_and_loss(YEAR, basis=Basis.ACCRUAL)
    expected = sum(model.live_invoices.values(), ZERO)
    actual = _total(pnl, "Total Income") if _has(pnl, "Total Income") else ZERO
    assert actual == expected


@SETTINGS
@given(ops=activity)
def test_voiding_everything_returns_every_report_to_zero(world, ops):
    model = _drive(world, ops)
    model.void_everything()
    reports = ReportService(model.session)
    for basis in (Basis.CASH, Basis.ACCRUAL):
        pnl = reports.profit_and_loss(YEAR, basis=basis)
        assert _total(pnl, "Net Income") == ZERO
        assert not _has(pnl, "Total Income") or _total(pnl, "Total Income") == ZERO
        sheet = reports.balance_sheet(YEAR_END, basis=basis)
        assert _total(sheet, "Total Assets") == ZERO
        assert _total(sheet, "Liabilities + Equity") == ZERO
    aging = ar_aging(model.session, YEAR_END)
    assert aging.rows == [] and aging.invoices == []
    assert aging.ledger_ar_balance == ZERO and aging.difference == ZERO


def test_the_model_really_creates_activity(world):
    """Guard against vacuous properties: a fixed script produces income and a balance."""
    model = world()
    model.run(("invoice", 0, date(2026, 2, 1), D("100.00"), 2))
    model.run(("pay", 0, date(2026, 3, 1), D("80.00"), False, True))
    assert model.live_invoices and model.live_applied_total() == D("80.00")
    pnl = ReportService(model.session).profit_and_loss(YEAR, basis=Basis.CASH)
    assert _total(pnl, "Total Income") == D("80.00")
    accrual = ReportService(model.session).profit_and_loss(YEAR, basis=Basis.ACCRUAL)
    assert _total(accrual, "Total Income") == D("200.00")


def test_ar_subledger_property_is_not_vacuous_on_a_clean_scenario(world):
    """The same check passes on activity whose void dates respect the document dates."""
    model = world()
    model.run(("invoice", 0, date(2026, 2, 1), D("100.00"), 1))
    model.run(("pay", 0, date(2026, 3, 1), D("40.00"), False, True))
    model.run(("refund", 1, date(2026, 3, 5), D("10.00")))  # refused: no credit; skipped
    for as_of in AS_OF_DATES:
        aging = ar_aging(model.session, as_of)
        assert aging.difference == ZERO
    mid = ar_aging(model.session, date(2026, 4, 1))
    assert mid.totals.total == D("60.00")


def test_a_void_dated_before_the_document_is_refused_by_the_model_run(world):
    model = world()
    model.run(("invoice", 0, date(2026, 4, 1), D("5.00"), 1))
    model.run(("void_invoice", 0, date(2026, 1, 1)))
    assert len(model.live_invoices) == 1  # refused, still live
    model.run(("void_invoice", 0, date(2026, 4, 1)))
    assert not model.live_invoices
