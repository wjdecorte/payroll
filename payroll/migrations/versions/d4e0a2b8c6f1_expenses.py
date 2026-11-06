"""Expenses: vendors, expenses, transfers, owner reimbursements, attachments.

Revision ID: d4e0a2b8c6f1
Revises: c3d9f1a7e2b4
Create Date: 2026-11-06 00:00:00.000000

New tables only (``journal_entry.source_type`` is a plain string with no CHECK list,
so the new ``transfer`` source type needs no schema change). Integrity enforced in
the database:
  * expense_line and attachment are append-only (UPDATE and DELETE rejected);
  * a deferred constraint trigger rejects, at COMMIT, an expense whose lines do not
    sum to its total (an expense with no lines included);
  * expense.reimbursement_id is set-once: it can be set only on a posted owner-paid
    expense, never changed from one reimbursement to another, and cleared only when
    the owning reimbursement is void (the services void the reimbursement first);
  * a deferred constraint trigger rejects, at COMMIT, a posted reimbursement whose
    linked expenses do not sum to its total. It takes FOR NO KEY UPDATE on the
    reimbursement row before summing: that conflicts with itself (concurrent commits
    are checked one after the other, each seeing the other's committed links) but not
    with the FOR KEY SHARE the foreign-key checks hold until the transaction ends
    (FOR UPDATE would deadlock two committing transactions).
The downgrade drops the triggers, functions and tables (all expenses data).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e0a2b8c6f1"
down_revision: str | None = "c3d9f1a7e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_ATTACHMENT_BYTES = 10_485_760


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
    ]


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete="RESTRICT")


def _status_columns() -> list[sa.Column]:
    return [
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("void_entry_id", sa.Integer(), nullable=True),
        sa.Column("void_date", sa.Date(), nullable=True),
    ]


def _status_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint("status IN ('posted', 'void')", name=f"ck_{table}_status"),
        sa.CheckConstraint(
            "(status = 'posted' AND void_entry_id IS NULL AND void_date IS NULL) "
            "OR (status = 'void' AND entry_id IS NOT NULL "
            "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL)",
            name=f"ck_{table}_void_fields",
        ),
        sa.CheckConstraint("void_date IS NULL OR void_date >= date", name=f"ck_{table}_void_date"),
    ]


def upgrade() -> None:
    money = sa.Numeric(14, 2)

    op.create_table(
        "vendor",
        *_base(),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_vendor_name", "vendor", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "reimbursement",
        *_base(),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("paid_from_account_id", sa.Integer(), nullable=False),
        sa.Column("total", money, nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        *_status_columns(),
        sa.CheckConstraint("total > 0", name="ck_reimbursement_total"),
        *_status_checks("reimbursement"),
        _fk("paid_from_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        _fk("void_entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "expense",
        *_base(),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=True),
        sa.Column("payee_text", sa.String(length=200), nullable=True),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("paid_from_account_id", sa.Integer(), nullable=True),
        sa.Column("total", money, nullable=False),
        *_status_columns(),
        sa.Column("reimbursement_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("(vendor_id IS NULL) <> (payee_text IS NULL)", name="ck_expense_payee"),
        sa.CheckConstraint("source IN ('account', 'owner')", name="ck_expense_source"),
        sa.CheckConstraint(
            "(source = 'owner') = (paid_from_account_id IS NULL)", name="ck_expense_source_account"
        ),
        sa.CheckConstraint("total > 0", name="ck_expense_total"),
        *_status_checks("expense"),
        _fk("vendor_id", "vendor.id"),
        _fk("paid_from_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        _fk("void_entry_id", "journal_entry.id"),
        _fk("reimbursement_id", "reimbursement.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_expense_vendor_id", "expense", ["vendor_id"])
    op.create_index("ix_expense_reimbursement_id", "expense", ["reimbursement_id"])

    op.create_table(
        "expense_line",
        *_base(),
        sa.Column("expense_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("amount", money, nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_expense_line_amount"),
        _fk("expense_id", "expense.id"),
        _fk("account_id", "account.id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("expense_id", "line_no", name="uq_expense_line_expense_line"),
    )

    op.create_table(
        "transfer",
        *_base(),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("from_account_id", sa.Integer(), nullable=False),
        sa.Column("to_account_id", sa.Integer(), nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        *_status_columns(),
        sa.CheckConstraint("from_account_id <> to_account_id", name="ck_transfer_accounts"),
        sa.CheckConstraint("amount > 0", name="ck_transfer_amount"),
        *_status_checks("transfer"),
        _fk("from_account_id", "account.id"),
        _fk("to_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        _fk("void_entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_type", sa.String(length=20), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=150), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("stored_name", sa.String(length=200), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.CheckConstraint("owner_type IN ('expense')", name="ck_attachment_owner_type"),
        sa.CheckConstraint(f"size BETWEEN 1 AND {MAX_ATTACHMENT_BYTES}", name="ck_attachment_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_sha256"),
        _fk("uploaded_by", "app_user.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_attachment_owner", "attachment", ["owner_type", "owner_id"])

    # Append-only tables.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("expense_line", "attachment"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION expenses_reject_change()"
        )

    # Lines must sum to the expense total (checked at COMMIT).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_check_line_sum() RETURNS trigger AS $$
        DECLARE
            exp_id integer;
            exp_total numeric;
            line_sum numeric;
        BEGIN
            IF TG_TABLE_NAME = 'expense' THEN
                exp_id := NEW.id;
            ELSE
                exp_id := NEW.expense_id;
            END IF;
            SELECT total INTO exp_total FROM expense WHERE id = exp_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            SELECT coalesce(sum(amount), 0) INTO line_sum FROM expense_line
             WHERE expense_id = exp_id;
            IF line_sum <> exp_total THEN
                RAISE EXCEPTION 'expense % lines sum to % but the total is %',
                    exp_id, line_sum, exp_total;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER expense_line_sum AFTER INSERT OR UPDATE OF total ON "
        "expense DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION expenses_check_line_sum()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER expense_line_sum AFTER INSERT ON "
        "expense_line DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION expenses_check_line_sum()"
    )

    # expense.reimbursement_id: set-once, only on a posted owner-paid expense.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_guard_reimbursement_link() RETURNS trigger AS $$
        DECLARE
            reim_status text;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.reimbursement_id IS NOT DISTINCT FROM NEW.reimbursement_id
            THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.reimbursement_id IS NOT NULL THEN
                IF NEW.reimbursement_id IS NOT NULL THEN
                    RAISE EXCEPTION 'expense % reimbursement link is set once', NEW.id;
                END IF;
                SELECT status INTO reim_status FROM reimbursement
                 WHERE id = OLD.reimbursement_id;
                IF reim_status IS DISTINCT FROM 'void' THEN
                    RAISE EXCEPTION
                        'expense % reimbursement link can be cleared only when reimbursement % is void',
                        NEW.id, OLD.reimbursement_id;
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.reimbursement_id IS NOT NULL THEN
                IF NEW.source <> 'owner' THEN
                    RAISE EXCEPTION 'only an owner-paid expense can be reimbursed (expense %)', NEW.id;
                END IF;
                IF NEW.status <> 'posted' THEN
                    RAISE EXCEPTION 'only a posted expense can be reimbursed (expense %)', NEW.id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER expense_reimbursement_link BEFORE INSERT OR UPDATE OF reimbursement_id "
        "ON expense FOR EACH ROW EXECUTE FUNCTION expenses_guard_reimbursement_link()"
    )

    # A posted reimbursement's expenses must sum to its total (checked at COMMIT).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_check_reimbursement_total(reim_id integer)
        RETURNS void AS $$
        DECLARE
            reim_total numeric;
            reim_status text;
            linked numeric;
        BEGIN
            -- FOR NO KEY UPDATE serializes concurrent commits without clashing with the
            -- FK checks' FOR KEY SHARE locks (FOR UPDATE would deadlock).
            SELECT total, status INTO reim_total, reim_status
              FROM reimbursement WHERE id = reim_id FOR NO KEY UPDATE;
            IF NOT FOUND OR reim_status <> 'posted' THEN
                RETURN;
            END IF;
            SELECT coalesce(sum(total), 0) INTO linked FROM expense
             WHERE reimbursement_id = reim_id;
            IF linked <> reim_total THEN
                RAISE EXCEPTION 'reimbursement % expenses sum to % but the total is %',
                    reim_id, linked, reim_total;
            END IF;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_check_reimbursement_row() RETURNS trigger AS $$
        BEGIN
            PERFORM expenses_check_reimbursement_total(NEW.id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION expenses_check_reimbursement_link() RETURNS trigger AS $$
        BEGIN
            IF NEW.reimbursement_id IS NOT NULL THEN
                PERFORM expenses_check_reimbursement_total(NEW.reimbursement_id);
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.reimbursement_id IS NOT NULL
               AND OLD.reimbursement_id IS DISTINCT FROM NEW.reimbursement_id THEN
                PERFORM expenses_check_reimbursement_total(OLD.reimbursement_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER reimbursement_total AFTER INSERT OR UPDATE OF total, status "
        "ON reimbursement DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION expenses_check_reimbursement_row()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER expense_reimbursement_total AFTER INSERT OR UPDATE OF "
        "reimbursement_id, total ON expense DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION expenses_check_reimbursement_link()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER expense_reimbursement_total ON expense")
    op.execute("DROP TRIGGER reimbursement_total ON reimbursement")
    op.execute("DROP FUNCTION expenses_check_reimbursement_link()")
    op.execute("DROP FUNCTION expenses_check_reimbursement_row()")
    op.execute("DROP FUNCTION expenses_check_reimbursement_total(integer)")
    op.execute("DROP TRIGGER expense_reimbursement_link ON expense")
    op.execute("DROP FUNCTION expenses_guard_reimbursement_link()")
    op.execute("DROP TRIGGER expense_line_sum ON expense_line")
    op.execute("DROP TRIGGER expense_line_sum ON expense")
    op.execute("DROP FUNCTION expenses_check_line_sum()")
    op.execute("DROP TRIGGER attachment_immutable ON attachment")
    op.execute("DROP TRIGGER expense_line_immutable ON expense_line")
    op.execute("DROP FUNCTION expenses_reject_change()")
    op.drop_index("ix_attachment_owner", table_name="attachment")
    op.drop_table("attachment")
    op.drop_table("transfer")
    op.drop_table("expense_line")
    op.drop_index("ix_expense_reimbursement_id", table_name="expense")
    op.drop_index("ix_expense_vendor_id", table_name="expense")
    op.drop_table("expense")
    op.drop_table("reimbursement")
    op.drop_index("uq_vendor_name", table_name="vendor")
    op.drop_table("vendor")
