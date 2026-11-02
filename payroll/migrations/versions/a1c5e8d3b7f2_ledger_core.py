"""Ledger core: account, journal_entry, journal_line, entry_counter, period_lock.

Revision ID: a1c5e8d3b7f2
Revises: 7c1f3a5e8b92
Create Date: 2026-11-02 00:00:00.000000

New tables only. Integrity is enforced in the database:
  * journal_line: one-sided positive amounts (CHECK); a deferred constraint trigger
    rejects, at COMMIT, any entry with fewer than 2 lines or debits != credits;
    UPDATE, DELETE and TRUNCATE are rejected (append-only).
  * journal_entry: only the status change posted -> void is allowed; DELETE and
    TRUNCATE are rejected.
The downgrade drops the triggers, the functions and then the tables (all ledger data).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c5e8d3b7f2"
down_revision: str | None = "7c1f3a5e8b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("detail_type", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("system_role", sa.String(length=30), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["account.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_account_number",
        "account",
        ["number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_index(
        "uq_account_parent_name",
        "account",
        [sa.text("coalesce(parent_id, 0)"), sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "uq_account_system_role",
        "account",
        ["system_role"],
        unique=True,
        postgresql_where=sa.text("system_role IS NOT NULL"),
    )

    op.create_table(
        "journal_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
        sa.Column("entry_no", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("reverses_entry_id", sa.Integer(), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=False),
        sa.Column("posted_by", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('posted', 'void')", name="ck_journal_entry_status"),
        sa.ForeignKeyConstraint(["reverses_entry_id"], ["journal_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["posted_by"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "entry_no", name="uq_journal_entry_source_no"),
    )
    op.create_index(
        "uq_journal_entry_reverses",
        "journal_entry",
        ["reverses_entry_id"],
        unique=True,
        postgresql_where=sa.text("reverses_entry_id IS NOT NULL"),
    )
    op.create_index("ix_journal_entry_entry_date", "journal_entry", ["entry_date"])

    op.create_table(
        "journal_line",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("debit", sa.Numeric(14, 2), nullable=False),
        sa.Column("credit", sa.Numeric(14, 2), nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "(debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)",
            name="ck_journal_line_one_side",
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["journal_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id", "line_no", name="uq_journal_line_entry_line"),
    )
    op.create_index("ix_journal_line_account_id", "journal_line", ["account_id"])

    op.create_table(
        "entry_counter",
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("last_no", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("source_type"),
    )
    op.create_table(
        "period_lock",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lock_date", sa.Date(), nullable=True),
        sa.Column("set_by", sa.Integer(), nullable=True),
        sa.Column("set_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["set_by"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO period_lock (id, lock_date) VALUES (1, NULL)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION journal_line_check_balanced() RETURNS trigger AS $$
        DECLARE
            n integer;
            d numeric;
            c numeric;
        BEGIN
            SELECT count(*), coalesce(sum(debit), 0), coalesce(sum(credit), 0)
              INTO n, d, c FROM journal_line WHERE entry_id = NEW.entry_id;
            IF n < 2 THEN
                RAISE EXCEPTION 'journal entry % must have at least 2 lines (has %)',
                    NEW.entry_id, n;
            END IF;
            IF d <> c THEN
                RAISE EXCEPTION 'journal entry % is unbalanced: debits % <> credits %',
                    NEW.entry_id, d, c;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER journal_line_balanced AFTER INSERT ON journal_line "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION journal_line_check_balanced()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION journal_line_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'journal_line is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER journal_line_immutable BEFORE UPDATE OR DELETE ON journal_line "
        "FOR EACH ROW EXECUTE FUNCTION journal_line_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER journal_line_no_truncate BEFORE TRUNCATE ON journal_line "
        "FOR EACH STATEMENT EXECUTE FUNCTION journal_line_reject_change()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION journal_entry_guard_change() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.status = 'posted' AND NEW.status = 'void'
               AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'journal_entry is immutable: % is not allowed (only status posted -> void)',
                TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER journal_entry_guard BEFORE UPDATE OR DELETE ON journal_entry "
        "FOR EACH ROW EXECUTE FUNCTION journal_entry_guard_change()"
    )
    op.execute(
        "CREATE TRIGGER journal_entry_no_truncate BEFORE TRUNCATE ON journal_entry "
        "FOR EACH STATEMENT EXECUTE FUNCTION journal_entry_guard_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER journal_entry_no_truncate ON journal_entry")
    op.execute("DROP TRIGGER journal_entry_guard ON journal_entry")
    op.execute("DROP FUNCTION journal_entry_guard_change()")
    op.execute("DROP TRIGGER journal_line_no_truncate ON journal_line")
    op.execute("DROP TRIGGER journal_line_immutable ON journal_line")
    op.execute("DROP FUNCTION journal_line_reject_change()")
    op.execute("DROP TRIGGER journal_line_balanced ON journal_line")
    op.execute("DROP FUNCTION journal_line_check_balanced()")
    op.drop_table("period_lock")
    op.drop_table("entry_counter")
    op.drop_index("ix_journal_line_account_id", table_name="journal_line")
    op.drop_table("journal_line")
    op.drop_index("ix_journal_entry_entry_date", table_name="journal_entry")
    op.drop_index("uq_journal_entry_reverses", table_name="journal_entry")
    op.drop_table("journal_entry")
    op.drop_index("uq_account_system_role", table_name="account")
    op.drop_index("uq_account_parent_name", table_name="account")
    op.drop_index("uq_account_number", table_name="account")
    op.drop_table("account")
