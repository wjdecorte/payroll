"""Audit log: append-only audit_log table.

Revision ID: 7c1f3a5e8b92
Revises: 5b2e7c9a4f18
Create Date: 2026-10-31 00:00:00.000000

New table only. ``user_id`` has no foreign key on purpose. Triggers reject UPDATE,
DELETE (row-level) and TRUNCATE (statement-level), so rows can only be added.
The downgrade drops the triggers, the function and the table (and all audit rows).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "7c1f3a5e8b92"
down_revision: str | None = "5b2e7c9a4f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.String(length=100), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_entity_entity_id", "audit_log", ["entity", "entity_id"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_reject_change() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION audit_log_reject_change()")
    op.drop_index("ix_audit_log_entity_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
