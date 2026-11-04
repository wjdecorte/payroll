"""
Receivables schema: constraints, indexes, append-only and over-application triggers.

The ``session`` fixture never really commits; ``_commit`` fires the deferred
constraint triggers exactly as COMMIT would (``SET CONSTRAINTS ALL IMMEDIATE``).
"""

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import text

from payroll.ledger.models import SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.migrations import alembic_runner
from payroll.receivables.models import (
    ApplicationIncome,
    Customer,
    Invoice,
    InvoiceLine,
    Item,
    Payment,
    PaymentApplication,
    ReceivablesSettings,
)

D = Decimal
JAN = date(2026, 1, 15)
PRE_REVISION = "a1c5e8d3b7f2"  # head before the receivables migration
RECEIVABLES_TABLES = {
    "customer",
    "item",
    "invoice",
    "invoice_line",
    "payment",
    "payment_application",
    "application_income",
    "deposit",
    "customer_refund",
    "receivables_settings",
}


def _commit(session) -> None:
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


def _entry(session, owner_user, chart) -> int:
    """A balanced posted entry to satisfy ``entry_id`` references."""
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id
    draft = EntryDraft(
        JAN, [LineDraft(cash, debit=D("1")), LineDraft(sales, credit=D("1"))], memo="x"
    )
    return LedgerPostingService(session).post(draft, actor=owner_user).id


def _invoice(session, customer, *, total="100.00", **kw) -> Invoice:
    kw.setdefault("invoice_date", JAN)
    kw.setdefault("due_date", date(2026, 2, 14))
    kw.setdefault("terms_days", 30)
    row = Invoice(customer_id=customer.id, total=D(total), **kw)
    session.add(row)
    session.flush()
    return row


def _payment(session, customer, bank, *, amount="100.00", **kw) -> Payment:
    row = Payment(
        customer_id=customer.id,
        received_date=JAN,
        amount=D(amount),
        deposit_account_id=bank.id,
        **kw,
    )
    session.add(row)
    session.flush()
    return row


def _apply(session, payment, invoice, chart, amount) -> PaymentApplication:
    row = PaymentApplication(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=D(amount),
        recognized_date=JAN,
        ar_account_id=chart["Accounts Receivable"].id,
    )
    session.add(row)
    session.flush()
    return row


def test_migration_upgrade_and_downgrade_roundtrip(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert RECEIVABLES_TABLES <= set(sa.inspect(conn).get_table_names())
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == alembic_runner._HEAD_REVISION

    alembic_runner.downgrade(PRE_REVISION)
    with scratch_engine.connect() as conn:
        assert not RECEIVABLES_TABLES & set(sa.inspect(conn).get_table_names())
        names = {r[0] for r in conn.exec_driver_sql("SELECT proname FROM pg_proc")}
        assert not names & {"application_reject_change", "payment_application_check_limits"}
        triggers = {r[0] for r in conn.exec_driver_sql("SELECT tgname FROM pg_trigger")}
        assert not {t for t in triggers if "application" in t}

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert RECEIVABLES_TABLES <= set(sa.inspect(conn).get_table_names())


def test_models_match_migration(scratch_engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, SQLModel.metadata) == []


def test_settings_row_seeded_with_1001(session):
    row = session.get(ReceivablesSettings, 1)
    assert row is not None
    assert row.next_invoice_no == 1001


def test_duplicate_customer_name_case_insensitive_rejected(session, customer):
    session.add(Customer(name="ACME corp"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_item_name_rejected(session, item):
    session.add(Item(name="CONSULTING", income_account_id=item.income_account_id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_invoice_number_unique_when_set_and_null_allowed_many_times(
    session, owner_user, chart, customer
):
    _invoice(session, customer)
    _invoice(session, customer)
    _invoice(session, customer)  # many drafts without a number
    entry_id = _entry(session, owner_user, chart)
    _invoice(session, customer, number=1001, status="open", entry_id=entry_id)
    with pytest.raises(IntegrityError):
        _invoice(session, customer, number=1001, status="draft")


def test_open_invoice_requires_number_and_entry(session, owner_user, chart, customer):
    entry_id = _entry(session, owner_user, chart)
    with pytest.raises(IntegrityError):
        _invoice(session, customer, status="open", entry_id=entry_id)  # no number


def test_open_invoice_requires_entry(session, customer):
    with pytest.raises(IntegrityError):
        _invoice(session, customer, status="open", number=1001)  # no entry


def test_open_invoice_with_number_and_entry_ok(session, owner_user, chart, customer):
    entry_id = _entry(session, owner_user, chart)
    inv = _invoice(session, customer, status="open", number=1001, entry_id=entry_id)
    assert inv.id is not None


def _line_ok(invoice, chart, **kw):
    base = dict(
        invoice_id=invoice.id,
        line_no=1,
        qty=D("1"),
        unit_price=D("10.00"),
        amount=D("10.00"),
        income_account_id=chart["Services Revenue"].id,
    )
    return InvoiceLine(**(base | kw))


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda s, c, ch, b, i: _line_ok(i, ch, qty=D("0")), id="qty-zero"),
        pytest.param(lambda s, c, ch, b, i: _line_ok(i, ch, unit_price=D("-1")), id="neg-price"),
        pytest.param(lambda s, c, ch, b, i: _line_ok(i, ch, amount=D("-0.01")), id="neg-amount"),
        pytest.param(lambda s, c, ch, b, i: Customer(name="X", terms_days=366), id="terms-366"),
        pytest.param(lambda s, c, ch, b, i: Customer(name="Y", terms_days=-1), id="terms-neg"),
        pytest.param(
            lambda s, c, ch, b, i: Item(
                name="Z", unit_price=D("-1"), income_account_id=ch["Services Revenue"].id
            ),
            id="neg-item-price",
        ),
        pytest.param(
            lambda s, c, ch, b, i: Payment(
                customer_id=c.id, received_date=JAN, amount=D("0"), deposit_account_id=b.id
            ),
            id="payment-zero",
        ),
        pytest.param(
            lambda s, c, ch, b, i: Payment(
                customer_id=c.id,
                received_date=JAN,
                amount=D("1"),
                deposit_account_id=b.id,
                status="paid",
            ),
            id="payment-status-paid",
        ),
        pytest.param(
            lambda s, c, ch, b, i: Invoice(
                customer_id=c.id,
                invoice_date=JAN,
                due_date=JAN,
                terms_days=0,
                total=D("1"),
                status="paid",
            ),
            id="invoice-status-paid",
        ),
    ],
)
def test_check_constraints(session, chart, customer, bank, build):
    invoice = _invoice(session, customer)
    session.add(build(session, customer, chart, bank, invoice))
    with pytest.raises(IntegrityError):
        session.flush()


def test_application_amount_checks(session, chart, customer, bank):
    inv = _invoice(session, customer)
    pay = _payment(session, customer, bank)
    with pytest.raises(IntegrityError):
        _apply(session, pay, inv, chart, "0")


def test_application_income_amount_check(session, chart, customer, bank):
    inv = _invoice(session, customer)
    pay = _payment(session, customer, bank)
    app = _apply(session, pay, inv, chart, "10.00")
    session.add(
        ApplicationIncome(
            application_id=app.id, account_id=chart["Services Revenue"].id, amount=D("0")
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_application_unique_per_payment_and_invoice(session, chart, customer, bank):
    inv = _invoice(session, customer)
    pay = _payment(session, customer, bank)
    _apply(session, pay, inv, chart, "10.00")
    with pytest.raises(IntegrityError):
        _apply(session, pay, inv, chart, "5.00")


def test_application_income_unique_per_account(session, chart, customer, bank):
    inv = _invoice(session, customer)
    pay = _payment(session, customer, bank)
    app = _apply(session, pay, inv, chart, "10.00")
    acct = chart["Services Revenue"].id
    session.add(ApplicationIncome(application_id=app.id, account_id=acct, amount=D("5")))
    session.flush()
    session.add(ApplicationIncome(application_id=app.id, account_id=acct, amount=D("5")))
    with pytest.raises(IntegrityError):
        session.flush()


def test_application_rows_cannot_be_updated_or_deleted(session, chart, customer, bank):
    inv = _invoice(session, customer)
    pay = _payment(session, customer, bank)
    app = _apply(session, pay, inv, chart, "10.00")
    split = ApplicationIncome(
        application_id=app.id, account_id=chart["Services Revenue"].id, amount=D("10.00")
    )
    session.add(split)
    session.flush()

    for sql, params in [
        ("UPDATE payment_application SET amount = 1 WHERE id = :i", {"i": app.id}),
        ("DELETE FROM payment_application WHERE id = :i", {"i": app.id}),
        ("UPDATE application_income SET amount = 1 WHERE id = :i", {"i": split.id}),
        ("DELETE FROM application_income WHERE id = :i", {"i": split.id}),
    ]:
        with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
            session.execute(text(sql), params)


def test_commit_rejected_when_payment_over_applied(session, chart, customer, bank):
    pay = _payment(session, customer, bank, amount="100.00")
    inv1 = _invoice(session, customer, total="80.00")
    inv2 = _invoice(session, customer, total="80.00")
    _apply(session, pay, inv1, chart, "60.00")
    _apply(session, pay, inv2, chart, "50.00")
    with pytest.raises(DBAPIError, match="over-applied"):
        _commit(session)


def test_commit_rejected_when_invoice_over_applied(session, chart, customer, bank):
    inv = _invoice(session, customer, total="100.00")
    p1 = _payment(session, customer, bank, amount="70.00")
    p2 = _payment(session, customer, bank, amount="70.00")
    _apply(session, p1, inv, chart, "70.00")
    _apply(session, p2, inv, chart, "40.00")
    with pytest.raises(DBAPIError, match="over-applied"):
        _commit(session)


def test_exact_application_commits(session, chart, customer, bank):
    inv = _invoice(session, customer, total="100.00")
    p1 = _payment(session, customer, bank, amount="70.00")
    p2 = _payment(session, customer, bank, amount="70.00")
    _apply(session, p1, inv, chart, "70.00")
    _apply(session, p2, inv, chart, "30.00")
    _commit(session)


def test_application_to_invoice_ignores_void_payments_in_limit(session, chart, customer, bank):
    inv = _invoice(session, customer, total="100.00")
    voided = _payment(session, customer, bank, amount="100.00")
    _apply(session, voided, inv, chart, "100.00")
    _commit(session)
    session.execute(
        text("UPDATE payment SET status = 'void', void_date = :d WHERE id = :i"),
        {"d": JAN, "i": voided.id},
    )
    fresh = _payment(session, customer, bank, amount="100.00")
    _apply(session, fresh, inv, chart, "100.00")
    _commit(session)


def test_source_type_refund_numbers_independently(session, owner_user, chart):
    posting = LedgerPostingService(session)
    cash, ar = chart["Business Checking"].id, chart["Accounts Receivable"].id
    lines = [LineDraft(ar, debit=D("5")), LineDraft(cash, credit=D("5"))]
    refund = posting.post(
        EntryDraft(JAN, lines, source_type=SourceType.REFUND, source_id="1"), actor=owner_user
    )
    manual = posting.post(EntryDraft(JAN, lines), actor=owner_user)
    assert refund.entry_no == 1
    assert manual.entry_no == 1
