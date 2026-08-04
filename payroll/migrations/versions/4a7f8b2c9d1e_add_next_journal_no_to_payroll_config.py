"""Add next_journal_no to payroll_config.

Revision ID: 4a7f8b2c9d1e
Revises: c3a8d2f19b40
Create Date: 2026-07-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a7f8b2c9d1e"
down_revision: str | None = "c3a8d2f19b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_config",
        sa.Column(
            "next_journal_no",
            sa.Integer(),
            nullable=False,
            server_default="202",
        ),
    )


def downgrade() -> None:
    op.drop_column("payroll_config", "next_journal_no")
