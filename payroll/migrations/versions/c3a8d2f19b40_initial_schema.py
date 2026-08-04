"""Initial schema — payroll_run and payroll_config tables.

Revision ID: c3a8d2f19b40
Revises:
Create Date: 2026-07-01 00:00:00.000000

NOTE: If you are adding Alembic to an existing database that already has these
tables (created by SQLModel's create_all), the alembic_runner.upgrade() helper
will automatically stamp the database at this revision and skip table creation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a8d2f19b40"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payroll_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
        sa.Column("pay_period", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("gross_salary", sa.Float(), nullable=False),
        sa.Column("health_insurance", sa.Float(), nullable=False),
        sa.Column("hsa_contribution", sa.Float(), nullable=False),
        sa.Column("health_in_income_tax", sa.Boolean(), nullable=False),
        sa.Column("hsa_in_income_tax", sa.Boolean(), nullable=False),
        sa.Column("ytd_fica_before", sa.Float(), nullable=False),
        sa.Column("notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("fica_wages", sa.Float(), nullable=False),
        sa.Column("income_tax_wages", sa.Float(), nullable=False),
        sa.Column("ss_taxable_wages", sa.Float(), nullable=False),
        sa.Column("ss_ee", sa.Float(), nullable=False),
        sa.Column("ss_er", sa.Float(), nullable=False),
        sa.Column("medicare_ee", sa.Float(), nullable=False),
        sa.Column("additional_medicare", sa.Float(), nullable=False),
        sa.Column("total_medicare_ee", sa.Float(), nullable=False),
        sa.Column("medicare_er", sa.Float(), nullable=False),
        sa.Column("federal_withholding", sa.Float(), nullable=False),
        sa.Column("ga_withholding", sa.Float(), nullable=False),
        sa.Column("total_employee_deductions", sa.Float(), nullable=False),
        sa.Column("net_pay", sa.Float(), nullable=False),
        sa.Column("total_employer_taxes", sa.Float(), nullable=False),
        sa.Column("total_employer_cost", sa.Float(), nullable=False),
        sa.Column("ytd_fica_after", sa.Float(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payroll_run_pay_period"), "payroll_run", ["pay_period"])

    op.create_table(
        "payroll_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
        sa.Column("default_gross_salary", sa.Float(), nullable=False),
        sa.Column("default_health_insurance", sa.Float(), nullable=False),
        sa.Column("default_hsa_contribution", sa.Float(), nullable=False),
        sa.Column("default_health_in_income_tax", sa.Boolean(), nullable=False),
        sa.Column("default_hsa_in_income_tax", sa.Boolean(), nullable=False),
        sa.Column("default_use_previous_ytd_fica", sa.Boolean(), nullable=False),
        sa.Column("default_save_run", sa.Boolean(), nullable=False),
        sa.Column("default_notes", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("acct_officer_compensation", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_payroll_tax_expense", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_health_insurance_exp", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_hsa_expense", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_fed_tax_payable", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_ga_tax_payable", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_ss_payable_ee", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_ss_payable_er", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_medicare_payable_ee", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_medicare_payable_er", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_health_ins_payable", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_hsa_payable", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("acct_checking", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("federal_due_date_note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("georgia_due_date_note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("payroll_config")
    op.drop_index(op.f("ix_payroll_run_pay_period"), table_name="payroll_run")
    op.drop_table("payroll_run")
