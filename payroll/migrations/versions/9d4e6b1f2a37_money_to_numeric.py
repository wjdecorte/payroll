"""Store money as NUMERIC(14,2) instead of float.

Revision ID: 9d4e6b1f2a37
Revises: 4a7f8b2c9d1e
Create Date: 2026-10-31 00:00:00.000000

Converts every money column on ``payroll_run`` and ``payroll_config`` from
double precision to ``NUMERIC(14, 2)``, rounding existing values to cents with
``round(col::numeric, 2)`` (Postgres rounds numeric half away from zero).
There are no rate/percent columns on these tables.

PRODUCTION DATA: take a ``pg_dump`` before deploying this migration.
The downgrade casts back to double precision (values already rounded to cents
stay rounded; sub-cent float noise removed by the upgrade is not restored).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d4e6b1f2a37"
down_revision: str | None = "4a7f8b2c9d1e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY_COLUMNS: dict[str, list[str]] = {
    "payroll_run": [
        # inputs
        "gross_salary",
        "health_insurance",
        "hsa_contribution",
        "ytd_fica_before",
        # calculated
        "fica_wages",
        "income_tax_wages",
        "ss_taxable_wages",
        "ss_ee",
        "ss_er",
        "medicare_ee",
        "additional_medicare",
        "total_medicare_ee",
        "medicare_er",
        "federal_withholding",
        "ga_withholding",
        "total_employee_deductions",
        "net_pay",
        "total_employer_taxes",
        "total_employer_cost",
        "ytd_fica_after",
    ],
    "payroll_config": [
        "default_gross_salary",
        "default_health_insurance",
        "default_hsa_contribution",
    ],
}


def upgrade() -> None:
    for table, columns in MONEY_COLUMNS.items():
        for col in columns:
            op.alter_column(
                table,
                col,
                existing_type=sa.Float(),
                existing_nullable=False,
                type_=sa.Numeric(14, 2),
                postgresql_using=f"round({col}::numeric, 2)",
            )


def downgrade() -> None:
    for table, columns in MONEY_COLUMNS.items():
        for col in columns:
            op.alter_column(
                table,
                col,
                existing_type=sa.Numeric(14, 2),
                existing_nullable=False,
                type_=sa.Float(),
                postgresql_using=f"{col}::double precision",
            )
