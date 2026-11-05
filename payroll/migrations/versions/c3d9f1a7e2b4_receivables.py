"""Receivables: customers, items, invoices, payments, deposits, refunds.

Revision ID: c3d9f1a7e2b4
Revises: a1c5e8d3b7f2
Create Date: 2026-11-03 00:00:00.000000

New tables only. Integrity enforced in the database:
  * payment_application and application_income are append-only (UPDATE and DELETE
    rejected);
  * a deferred constraint trigger on payment_application rejects, at COMMIT, a payment
    applied for more than its amount, or an invoice applied (from posted payments) for
    more than its total. The trigger takes FOR NO KEY UPDATE on the payment / invoice row
    before summing: it conflicts with itself (so concurrent commits are checked one after
    the other, each seeing the other's committed applications) but not with the FOR KEY
    SHARE the foreign-key checks hold until the transaction ends (FOR UPDATE would
    deadlock two committing transactions). The loser fails with an over-applied error.
The settings row (next invoice number 1001) is inserted here. The downgrade drops the
triggers, functions and tables (all receivables data).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d9f1a7e2b4"
down_revision: str | None = "a1c5e8d3b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
    ]


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete="RESTRICT")


def upgrade() -> None:
    money = sa.Numeric(14, 2)

    op.create_table(
        "customer",
        *_base(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("billing_address", sa.String(length=1000), nullable=True),
        sa.Column("terms_days", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("terms_days BETWEEN 0 AND 365", name="ck_customer_terms_days"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_customer_name", "customer", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "item",
        *_base(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("unit_price", money, nullable=False),
        sa.Column("income_account_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.CheckConstraint("unit_price >= 0", name="ck_item_unit_price"),
        _fk("income_account_id", "account.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_item_name", "item", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "invoice",
        *_base(),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("terms_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("total", money, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("void_date", sa.Date(), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'open', 'void')", name="ck_invoice_status"),
        sa.CheckConstraint(
            "status <> 'open' OR (number IS NOT NULL AND entry_id IS NOT NULL)",
            name="ck_invoice_open_has_number_entry",
        ),
        _fk("customer_id", "customer.id"),
        _fk("entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_invoice_number",
        "invoice",
        ["number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_index("ix_invoice_customer_id", "invoice", ["customer_id"])

    op.create_table(
        "invoice_line",
        *_base(),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", money, nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("income_account_id", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_invoice_line_qty"),
        sa.CheckConstraint("unit_price >= 0", name="ck_invoice_line_unit_price"),
        sa.CheckConstraint("amount >= 0", name="ck_invoice_line_amount"),
        _fk("invoice_id", "invoice.id"),
        _fk("item_id", "item.id"),
        _fk("income_account_id", "account.id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", "line_no", name="uq_invoice_line_invoice_line"),
    )

    op.create_table(
        "deposit",
        *_base(),
        sa.Column("deposit_date", sa.Date(), nullable=False),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("total", money, nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("void_date", sa.Date(), nullable=True),
        sa.CheckConstraint("total > 0", name="ck_deposit_total"),
        sa.CheckConstraint("status IN ('posted', 'void')", name="ck_deposit_status"),
        _fk("bank_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "payment",
        *_base(),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=True),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("amount", money, nullable=False),
        sa.Column("deposit_account_id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("void_date", sa.Date(), nullable=True),
        sa.Column("deposit_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_payment_amount"),
        sa.CheckConstraint("status IN ('posted', 'void')", name="ck_payment_status"),
        _fk("customer_id", "customer.id"),
        _fk("deposit_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        _fk("deposit_id", "deposit.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_customer_id", "payment", ["customer_id"])

    op.create_table(
        "payment_application",
        *_base(),
        sa.Column("payment_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("recognized_date", sa.Date(), nullable=False),
        sa.Column("ar_account_id", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_payment_application_amount"),
        _fk("payment_id", "payment.id"),
        _fk("invoice_id", "invoice.id"),
        _fk("ar_account_id", "account.id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_id", "invoice_id", name="uq_payment_application_pay_inv"),
    )
    op.create_index("ix_payment_application_invoice_id", "payment_application", ["invoice_id"])

    op.create_table(
        "application_income",
        *_base(),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_application_income_amount"),
        _fk("application_id", "payment_application.id"),
        _fk("account_id", "account.id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("application_id", "account_id", name="uq_application_income_app_acct"),
    )

    op.create_table(
        "customer_refund",
        *_base(),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("refund_date", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=True),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("amount", money, nullable=False),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("void_date", sa.Date(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_customer_refund_amount"),
        sa.CheckConstraint("status IN ('posted', 'void')", name="ck_customer_refund_status"),
        _fk("customer_id", "customer.id"),
        _fk("bank_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "receivables_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("next_invoice_no", sa.Integer(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("company_address", sa.String(length=1000), nullable=True),
        sa.Column("remit_to", sa.String(length=1000), nullable=True),
        sa.Column("invoice_footer", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_receivables_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO receivables_settings (id, next_invoice_no) VALUES (1, 1001)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION application_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("payment_application", "application_income"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION application_reject_change()"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION payment_application_check_limits() RETURNS trigger AS $$
        DECLARE
            pay_amount numeric;
            inv_total numeric;
            applied numeric;
        BEGIN
            -- FOR NO KEY UPDATE serializes concurrent commits without clashing with the
            -- FK checks' FOR KEY SHARE locks (FOR UPDATE would deadlock).
            SELECT amount INTO pay_amount FROM payment WHERE id = NEW.payment_id FOR NO KEY UPDATE;
            SELECT coalesce(sum(amount), 0) INTO applied
              FROM payment_application WHERE payment_id = NEW.payment_id;
            IF applied > pay_amount THEN
                RAISE EXCEPTION 'payment % is over-applied: applied % > amount %',
                    NEW.payment_id, applied, pay_amount;
            END IF;

            SELECT total INTO inv_total FROM invoice WHERE id = NEW.invoice_id FOR NO KEY UPDATE;
            SELECT coalesce(sum(a.amount), 0) INTO applied
              FROM payment_application a JOIN payment p ON p.id = a.payment_id
             WHERE a.invoice_id = NEW.invoice_id AND p.status = 'posted';
            IF applied > inv_total THEN
                RAISE EXCEPTION 'invoice % is over-applied: applied % > total %',
                    NEW.invoice_id, applied, inv_total;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER payment_application_limits AFTER INSERT ON "
        "payment_application DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION payment_application_check_limits()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER payment_application_limits ON payment_application")
    op.execute("DROP FUNCTION payment_application_check_limits()")
    op.execute("DROP TRIGGER application_income_immutable ON application_income")
    op.execute("DROP TRIGGER payment_application_immutable ON payment_application")
    op.execute("DROP FUNCTION application_reject_change()")
    op.drop_table("receivables_settings")
    op.drop_table("customer_refund")
    op.drop_table("application_income")
    op.drop_index("ix_payment_application_invoice_id", table_name="payment_application")
    op.drop_table("payment_application")
    op.drop_index("ix_payment_customer_id", table_name="payment")
    op.drop_table("payment")
    op.drop_table("deposit")
    op.drop_table("invoice_line")
    op.drop_index("ix_invoice_customer_id", table_name="invoice")
    op.drop_index("uq_invoice_number", table_name="invoice")
    op.drop_table("invoice")
    op.drop_index("uq_item_name", table_name="item")
    op.drop_table("item")
    op.drop_index("uq_customer_name", table_name="customer")
    op.drop_table("customer")
