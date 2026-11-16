"""Property tests (Hypothesis) for expenses, against the real Postgres.

Expenses, reimbursements and transfers are built only through the real services. The services
commit, so a savepoint cannot isolate examples; the fixture commits a fresh world per example
and examples accumulate inside the test's outer transaction (rolled back at the end of the
test), exactly like ``tests/receivables/test_properties.py``.

Operations the services legitimately refuse (a void dated before its document, an expense that
is already reimbursed, ...) raise ``AppBaseError``; the model skips them and the property
checks that a refusal left the ledger and the documents exactly as they were.
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from sqlmodel import select

from payroll.expenses.exceptions import (
    ExpenseError,
    InvalidExpenseError,
    InvalidReimbursementError,
    InvalidTransferError,
    ReimbursementError,
    TransferError,
)
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Expense, Reimbursement, Transfer
from payroll.expenses.owner import owner_summary
from payroll.expenses.reimbursements import ReimbursementService
from payroll.expenses.transfers import TransferService
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import AccountType, JournalLine, SystemRole
from payroll.reports.statements import ReportService

D = Decimal
ZERO = D("0.00")
YEAR_START = date(2026, 1, 1)
YEAR_END = date(2026, 12, 31)
SETTINGS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

RESET = (
    "TRUNCATE tax_payment, payroll_run, attachment, expense_line, expense, reimbursement, transfer, vendor, "
    "application_income, payment_application, invoice_line, invoice, deposit, payment, "
    "customer_refund, customer, item, journal_line, journal_entry, account"
)

day = st.integers(min_value=0, max_value=300).map(lambda n: YEAR_START + timedelta(days=n))
offset = st.integers(min_value=-15, max_value=60)  # negative: before the document, refused
cents = st.integers(min_value=1, max_value=300_000)
index = st.integers(min_value=0, max_value=50)
operation = st.one_of(
    st.tuples(st.just("account_expense"), day, st.lists(cents, min_size=1, max_size=4)),
    st.tuples(st.just("owner_expense"), day, st.lists(cents, min_size=1, max_size=4)),
    st.tuples(st.just("void_expense"), index, offset),
    st.tuples(st.just("reimburse"), index, st.lists(index, min_size=1, max_size=4), offset),
    st.tuples(st.just("void_reimbursement"), index, offset),
    st.tuples(st.just("transfer"), day, cents, st.booleans()),
    st.tuples(st.just("void_transfer"), index, offset),
)
activity = st.lists(operation, min_size=1, max_size=16)


@pytest.fixture
def world(session, owner_user):
    """A factory: each call wipes the books and builds a fresh committed chart."""

    def fresh():
        session.commit()  # the owner fixture row must survive a service rollback
        session.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(sa.text("SET session_replication_role = replica"))
        session.execute(sa.text(RESET))
        session.execute(sa.text("SET session_replication_role = DEFAULT"))
        session.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        seed_default_chart(session, actor=owner_user)
        chart = {node.full_name: node.account for node in AccountService(session).tree()}
        accounts = AccountService(session)
        chart["Card"] = accounts.create(
            actor=owner_user, name="Business Visa", type=AccountType.LIABILITY
        )
        chart["Savings"] = accounts.create(actor=owner_user, name="Savings", type=AccountType.ASSET)
        session.commit()
        return Model(session, owner_user, chart)

    return fresh


class Model:
    """Drives random activity through the services and keeps its own independent books.

    Which operations may be refused is decided by the model, never by the service: creates and
    reimbursements of genuinely open expenses must always succeed; a void may be refused only
    when its date precedes the document (an ``Invalid*`` error) or when the model knows the
    target is already void / reimbursed (the document's own ``*Error``). A void that the model
    knows is illegal must be refused. Any other exception fails the test.
    """

    def __init__(self, session, actor, chart):
        self.session = session
        self.actor = actor
        self.chart = chart
        self.expenses: dict[int, dict] = {}  # id -> {source, date, void, reimbursement}
        self.reimbursements: dict[int, dict] = {}  # id -> {date, live, expenses}
        self.transfers: dict[int, dict] = {}  # id -> {date, live}
        self.refusals = 0
        self.successes: Counter[str] = Counter()
        self.report_events = False  # hypothesis.event only works inside a @given test

    @staticmethod
    def _pick(seq, i):
        return seq[i % len(seq)] if seq else None

    def snapshot(self):
        """Everything a refused operation must leave untouched."""
        lines = self.session.exec(
            select(JournalLine.id, JournalLine.account_id, JournalLine.debit, JournalLine.credit)
        ).all()
        expenses = self.session.exec(
            select(Expense.id, Expense.status, Expense.reimbursement_id, Expense.void_entry_id)
        ).all()
        reimbursements = self.session.exec(select(Reimbursement.id, Reimbursement.status)).all()
        transfers = self.session.exec(select(Transfer.id, Transfer.status)).all()
        return (
            sorted(tuple(r) for r in lines),
            sorted(tuple(r) for r in expenses),
            sorted(tuple(r) for r in reimbursements),
            sorted(tuple(r) for r in transfers),
        )

    def _try(self, kind, fn, *, may_refuse=(), must_refuse=False):
        """Run a service call.

        ``may_refuse`` lists the only exception classes allowed; with none, any exception
        fails the test. ``must_refuse`` demands that the call is refused. A refusal must leave
        every row as it was. A success is counted and reported to Hypothesis.
        """
        self.session.expire_all()
        before = self.snapshot()
        try:
            result = fn()
        except may_refuse:
            self.session.rollback()
            self.refusals += 1
            assert self.snapshot() == before
            return None
        assert not must_refuse, f"{kind} should have been refused"
        self.successes[kind] += 1
        if self.report_events:
            event(kind)
        return result

    # ------------------------------------------------------------- operations

    def run(self, op):
        getattr(self, f"op_{op[0]}")(*op[1:])

    def _create_expense(self, kind, when, amounts, source):
        accounts = [self.chart["Office Expenses"], self.chart["Travel"], self.chart["Meals"]]
        lines = [
            ExpenseLineInput(accounts[n % 3].id, D(amount) / 100)
            for n, amount in enumerate(amounts)
        ]
        expense = self._try(
            kind,
            lambda: ExpenseService(self.session).create(
                actor=self.actor,
                expense_date=when,
                lines=lines,
                source=source,
                paid_from_account_id=(
                    self.chart["Business Checking"].id if source == "account" else None
                ),
                payee_text="Random Payee",
            ),
        )
        assert expense is not None and expense.status == "posted"
        self.expenses[expense.id] = {
            "source": source,
            "date": when,
            "void": False,
            "reimbursement": None,
        }

    def op_account_expense(self, when, amounts):
        self._create_expense("account_expense", when, amounts, "account")

    def op_owner_expense(self, when, amounts):
        self._create_expense("owner_expense", when, amounts, "owner")

    def op_void_expense(self, i, shift):
        expense_id = self._pick(list(self.expenses), i)
        if expense_id is None:
            return
        known = self.expenses[expense_id]
        illegal = known["void"] or known["reimbursement"] is not None
        allowed = (ExpenseError,) * illegal + (InvalidExpenseError,) * (shift < 0)
        done = self._try(
            "void_expense",
            lambda: ExpenseService(self.session).void(
                expense_id, actor=self.actor, void_date=known["date"] + timedelta(days=shift)
            ),
            may_refuse=allowed,
            must_refuse=illegal or shift < 0,
        )
        if done is not None:
            assert done.status == "void"
            known["void"] = True

    def open_owner_expense_ids(self):
        """Open owner-paid expenses straight from the Expense rows."""
        return [
            e.id
            for e in self.session.exec(select(Expense).order_by(Expense.id)).all()
            if e.source == "owner" and e.status == "posted" and e.reimbursement_id is None
        ]

    def model_open_ids(self):
        """The same set from the model's own books, in creation order."""
        return [
            e
            for e, k in self.expenses.items()
            if k["source"] == "owner" and not k["void"] and k["reimbursement"] is None
        ]

    def op_reimburse(self, i, picks, shift):
        open_ids = self.model_open_ids()
        if not open_ids:
            return
        chosen = sorted({open_ids[p % len(open_ids)] for p in picks})
        latest = max(self.expenses[e]["date"] for e in chosen)
        reimbursement = self._try(
            "reimburse",
            lambda: ReimbursementService(self.session).create(
                actor=self.actor,
                reimbursement_date=latest + timedelta(days=max(shift, 0)),
                paid_from_account_id=self.chart["Business Checking"].id,
                expense_ids=chosen,
            ),
        )
        assert reimbursement.status == "posted"
        self.reimbursements[reimbursement.id] = {
            "date": reimbursement.date,
            "live": True,
            "expenses": chosen,
        }
        for e in chosen:
            self.expenses[e]["reimbursement"] = reimbursement.id
        self.session.expire_all()
        for e in chosen:
            assert ExpenseService(self.session).get(e).reimbursement_id == reimbursement.id

    def op_void_reimbursement(self, i, shift):
        reimbursement_id = self._pick(list(self.reimbursements), i)
        if reimbursement_id is None:
            return
        known = self.reimbursements[reimbursement_id]
        illegal = not known["live"]
        allowed = (ReimbursementError,) * illegal + (InvalidReimbursementError,) * (shift < 0)
        done = self._try(
            "void_reimbursement",
            lambda: ReimbursementService(self.session).void(
                reimbursement_id, actor=self.actor, void_date=known["date"] + timedelta(days=shift)
            ),
            may_refuse=allowed,
            must_refuse=illegal or shift < 0,
        )
        if done is not None:
            assert done.status == "void"
            known["live"] = False
            for e in known["expenses"]:
                self.expenses[e]["reimbursement"] = None
            self.session.expire_all()
            for e in known["expenses"]:
                assert ExpenseService(self.session).get(e).reimbursement_id is None

    def op_transfer(self, when, amount, to_card):
        source = self.chart["Business Checking"]
        target = self.chart["Card" if to_card else "Savings"]
        transfer = self._try(
            "transfer",
            lambda: TransferService(self.session).create(
                actor=self.actor,
                transfer_date=when,
                from_account_id=source.id,
                to_account_id=target.id,
                amount=D(amount) / 100,
            ),
        )
        assert transfer.status == "posted"
        self.transfers[transfer.id] = {"date": when, "live": True}

    def op_void_transfer(self, i, shift):
        transfer_id = self._pick(list(self.transfers), i)
        if transfer_id is None:
            return
        known = self.transfers[transfer_id]
        illegal = not known["live"]
        allowed = (TransferError,) * illegal + (InvalidTransferError,) * (shift < 0)
        done = self._try(
            "void_transfer",
            lambda: TransferService(self.session).void(
                transfer_id, actor=self.actor, void_date=known["date"] + timedelta(days=shift)
            ),
            may_refuse=allowed,
            must_refuse=illegal or shift < 0,
        )
        if done is not None:
            assert done.status == "void"
            known["live"] = False


def _drive(world, ops):
    model = world()
    for op in ops:
        model.run(op)
    return model


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@SETTINGS
@example(amounts=[1])
@example(amounts=[1] * 20)
@given(amounts=st.lists(st.integers(min_value=1, max_value=10_000_000), min_size=1, max_size=20))
def test_any_split_posts_a_balanced_entry_summing_to_the_total(world, amounts):
    model = world()
    lines = [ExpenseLineInput(model.chart["Office Expenses"].id, D(a) / 100) for a in amounts]
    total = sum((line.amount for line in lines), ZERO)
    expense = ExpenseService(model.session).create(
        actor=model.actor,
        expense_date=date(2026, 3, 1),
        lines=lines,
        source="account",
        paid_from_account_id=model.chart["Business Checking"].id,
        payee_text="Split Co",
    )
    assert expense.total == total == D(sum(amounts)) / 100
    posted = model.session.exec(
        select(JournalLine).where(JournalLine.entry_id == expense.entry_id)
    ).all()
    debits = sum((line.debit for line in posted if line.debit), ZERO)
    credits = sum((line.credit for line in posted if line.credit), ZERO)
    assert debits == credits == expense.total
    assert len(ExpenseService(model.session).lines(expense.id)) == len(amounts)
    trial = ReportService(model.session).trial_balance(YEAR_END)
    assert trial.balanced and trial.total_debit == trial.total_credit == total


@SETTINGS
@given(
    amounts=st.lists(cents, min_size=1, max_size=6),
    picks=st.lists(index, min_size=1, max_size=6),
)
def test_any_subset_reimburses_for_exactly_its_sum(world, amounts, picks):
    model = world()
    for n, amount in enumerate(amounts):
        model.op_owner_expense(date(2026, 2, 1) + timedelta(days=n), [amount])
    open_ids = model.open_owner_expense_ids()
    assert len(open_ids) == len(amounts)
    chosen = sorted({open_ids[p % len(open_ids)] for p in picks})
    by_id = dict(zip(open_ids, amounts, strict=True))  # open ids are in creation order
    expected = sum((D(by_id[e]) for e in chosen), ZERO) / 100
    summary = owner_summary(model.session)
    assert summary.matches and summary.owed_to_me == sum((D(a) for a in amounts), ZERO) / 100

    reimbursement = ReimbursementService(model.session).create(
        actor=model.actor,
        reimbursement_date=date(2026, 6, 1),
        paid_from_account_id=model.chart["Business Checking"].id,
        expense_ids=chosen,
    )
    assert reimbursement.total == expected
    rest = [e for e in open_ids if e not in chosen]
    assert model.open_owner_expense_ids() == rest
    after = owner_summary(model.session)
    assert after.matches
    assert {e.id for e in after.open_expenses} == set(rest)
    assert after.owed_to_me == sum((D(by_id[e]) for e in rest), ZERO) / 100
    assert ReportService(model.session).trial_balance(YEAR_END).balanced


def _role_balance(session, role: SystemRole) -> Decimal:
    value = session.execute(
        sa.text(
            "SELECT COALESCE(SUM(l.debit - l.credit), 0) FROM journal_line l "
            "JOIN account a ON a.id = l.account_id WHERE a.system_role = :role"
        ),
        {"role": role.value},
    ).scalar_one()
    return Decimal(value)


def _check_invariants(model):
    session = model.session
    session.expire_all()
    reports = ReportService(session)
    for as_of in (date(2026, 6, 30), YEAR_END, date(2027, 12, 31)):
        trial = reports.trial_balance(as_of)
        assert trial.balanced and trial.total_debit == trial.total_credit
    summary = owner_summary(session)
    assert summary.matches, (summary.owed_to_me, summary.open_expense_total)
    expenses = session.exec(select(Expense)).all()
    for expense in expenses:
        assert not (expense.status == "void" and expense.reimbursement_id is not None)
    service = ReimbursementService(session)
    for row in session.exec(select(Reimbursement)).all():
        if row.status == "posted":
            linked = [e for e in expenses if e.reimbursement_id == row.id]
            assert linked and row.total == sum((e.total for e in linked), ZERO)
            assert service.expenses(row.id) and {e.id for e in service.expenses(row.id)} == {
                e.id for e in linked
            }
        else:
            assert all(e.reimbursement_id != row.id for e in expenses)
    # No other role account is ever touched by expenses, reimbursements or transfers.
    assert _role_balance(session, SystemRole.ACCOUNTS_RECEIVABLE) == ZERO
    assert _role_balance(session, SystemRole.UNDEPOSITED_FUNDS) == ZERO
    # Due to Owner is checked against totals summed straight from the Expense rows, so a bug in
    # owner_summary cannot hide.
    open_ids = model.open_owner_expense_ids()
    assert open_ids == model.model_open_ids()
    open_total = sum((e.total for e in expenses if e.id in set(open_ids)), ZERO)
    assert _role_balance(session, SystemRole.DUE_TO_OWNER) == -open_total
    assert summary.open_expense_total == open_total == summary.owed_to_me


EXAMPLE_SEQUENCES = [
    # create-of-each, then a legal void of each document kind, with refusals in between
    [
        ("owner_expense", date(2026, 3, 1), [1000]),
        ("reimburse", 0, [0], 0),
        ("void_expense", 0, 5),  # refused: reimbursed
        ("void_reimbursement", 0, 3),  # succeeds, the expense reopens
        ("void_reimbursement", 0, 3),  # refused: already void
        ("void_expense", 0, 5),  # succeeds now
        ("void_expense", 0, 5),  # refused: already void
        ("account_expense", date(2026, 3, 2), [500, 250]),
        ("void_expense", 1, -1),  # refused: before the document
        ("void_expense", 1, 0),
        ("transfer", date(2026, 3, 5), 12345, True),
        ("void_transfer", 0, -2),  # refused: before the document
        ("void_transfer", 0, 4),
        ("void_transfer", 0, 4),  # refused: already void
    ],
    [("account_expense", date(2026, 3, 1), [500]), ("void_expense", 0, -1)],
]
OP_KINDS = {
    "account_expense",
    "owner_expense",
    "void_expense",
    "reimburse",
    "void_reimbursement",
    "transfer",
    "void_transfer",
}


@SETTINGS
@example(ops=EXAMPLE_SEQUENCES[0])
@example(ops=EXAMPLE_SEQUENCES[1])
@given(ops=activity)
def test_random_activity_keeps_the_books_consistent(world, ops):
    model = world()
    model.report_events = True
    for op in ops:
        model.run(op)
        _check_invariants(model)


def test_the_explicit_examples_run_every_operation_successfully(world):
    """Not left to luck: the committed @example sequences succeed at least once per op kind."""
    total = Counter()
    for ops in EXAMPLE_SEQUENCES:
        model = world()
        for op in ops:
            model.run(op)
            _check_invariants(model)
        total.update(model.successes)
    assert set(total) == OP_KINDS, OP_KINDS - set(total)


def test_scripted_run_with_asserted_statuses(world):
    """A fixed script: every create and every void really happens, with statuses asserted."""
    model = world()
    session = model.session
    model.run(("owner_expense", date(2026, 2, 1), [1000, 250]))
    model.run(("owner_expense", date(2026, 2, 3), [4000]))
    model.run(("account_expense", date(2026, 2, 5), [999]))
    first, second, third = model.expenses
    assert len(model.expenses) == 3

    model.run(("reimburse", 0, [0], 2))
    reimbursement_id = next(iter(model.reimbursements))
    assert model.expenses[first]["reimbursement"] == reimbursement_id
    assert owner_summary(session).owed_to_me == D("40.00")

    model.run(("void_expense", 0, 5))  # refused: reimbursed
    assert model.refusals == 1 and ExpenseService(session).get(first).status == "posted"

    model.run(("void_reimbursement", 0, 3))
    reimbursement = ReimbursementService(session).get(reimbursement_id)
    assert reimbursement.status == "void"
    assert ExpenseService(session).get(first).reimbursement_id is None
    assert model.open_owner_expense_ids() == [first, second]  # reopened
    assert owner_summary(session).owed_to_me == D("52.50")

    model.run(("void_expense", 0, 5))
    assert ExpenseService(session).get(first).status == "void"
    model.run(("void_expense", 2, 0))
    assert ExpenseService(session).get(third).status == "void"

    model.run(("transfer", date(2026, 3, 1), 12345, True))
    model.run(("void_transfer", 0, -3))  # refused, state unchanged
    transfer_id = next(iter(model.transfers))
    assert TransferService(session).get(transfer_id).status == "posted"
    model.run(("void_transfer", 0, 2))
    assert TransferService(session).get(transfer_id).status == "void"

    assert model.refusals == 2
    assert set(model.successes) == OP_KINDS
    summary = owner_summary(session)
    assert summary.matches and summary.owed_to_me == D("40.00")
    _check_invariants(model)
