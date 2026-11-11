"""Payroll to ledger: run lifecycle, FUTA/SUTA figures, settings and tax payments.

Revision ID: e5f1b3c9d7a2
Revises: d4e0a2b8c6f1
Create Date: 2026-11-11 00:00:00.000000

Adds to ``payroll_run`` the lifecycle (``status``: draft, approved, void, imported), the
posting links (``pay_date``, ``net_pay_*``, ``entry_id``, ``void_*``) and the FUTA/SUTA
figures. Every existing run becomes ``imported`` (history only) with FUTA and SUTA of
0.00; its other columns are not touched. New tables: ``payroll_settings`` (rates per
tax year), ``payroll_deposit_rules`` (singleton, one default row) and ``tax_payment``.

Integrity enforced in the database:
  * CHECKs on payroll_run and tax_payment keep status and its fields consistent;
  * a partial unique index allows one non-void run per pay period;
  * a trigger refuses UPDATE of any column of a non-draft run except status
    (approved -> void only), the void fields and modify_date, and refuses DELETE of a
    non-draft run.
The downgrade refuses to run while any run is approved, void or has an entry, or any
tax payment exists (dropping the columns would destroy the posting links).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f1b3c9d7a2"
down_revision: str | None = "d4e0a2b8c6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Columns of payroll_run that may change on a non-draft run (everything else is frozen).
_ALLOWED_ON_APPROVED = ("status", "void_entry_id", "void_date", "modify_date")


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("create_date", sa.DateTime(), nullable=False),
        sa.Column("modify_date", sa.DateTime(), nullable=False),
    ]


def _fk(column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], [target], ondelete="RESTRICT")


# (role, account type, account name, parent name or None) of the default chart accounts.
_PAYROLL_ROLE_ACCOUNTS = (
    ("payroll_wages", "expense", "Wages", "Payroll Expenses"),
    ("payroll_tax_expense", "expense", "Taxes", "Payroll Expenses"),
    ("payroll_federal_payable", "liability", "Federal Taxes (941/943/944)", "Payroll Liabilities"),
    ("payroll_ga_wh_payable", "liability", "GA Income Tax", "Payroll Liabilities"),
    ("payroll_futa_payable", "liability", "FUTA Payable", "Payroll Liabilities"),
    ("payroll_suta_payable", "liability", "GA SUTA Payable", "Payroll Liabilities"),
    ("payroll_health_payable", "liability", "S-Corp Health Insurance", "Payroll Liabilities"),
    ("payroll_hsa_payable", "liability", "Co. HSA", "Payroll Liabilities"),
)


def _bind_payroll_roles() -> None:
    """Bind each payroll role to its default account if it exists, is active and is unbound.

    A role already bound, or an account already holding another role, is left alone; the
    owner binds anything missing in Settings > Ledger.
    """
    bind = op.get_bind()
    for role, account_type, name, parent in _PAYROLL_ROLE_ACCOUNTS:
        bind.execute(
            sa.text(
                "UPDATE account SET system_role = :role, modify_date = (now() AT TIME ZONE 'UTC') "
                "WHERE id = (SELECT a.id FROM account a JOIN account p ON p.id = a.parent_id "
                "WHERE a.name = :name AND p.name = :parent AND p.parent_id IS NULL "
                "AND a.type = :type AND a.is_active AND a.system_role IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM account WHERE system_role = :role)"
            ),
            {"role": role, "name": name, "parent": parent, "type": account_type},
        )


def upgrade() -> None:
    money = sa.Numeric(14, 2)

    # --- payroll_run: new columns (existing rows become 'imported' with zero FUTA/SUTA) ---
    op.add_column(
        "payroll_run",
        sa.Column("status", sa.String(length=10), nullable=False, server_default="imported"),
    )
    op.add_column("payroll_run", sa.Column("pay_date", sa.Date(), nullable=True))
    op.add_column("payroll_run", sa.Column("net_pay_source", sa.String(length=10), nullable=True))
    op.add_column("payroll_run", sa.Column("net_pay_account_id", sa.Integer(), nullable=True))
    op.add_column("payroll_run", sa.Column("entry_id", sa.Integer(), nullable=True))
    op.add_column("payroll_run", sa.Column("void_entry_id", sa.Integer(), nullable=True))
    op.add_column("payroll_run", sa.Column("void_date", sa.Date(), nullable=True))
    for name in ("futa_wages", "futa", "suta_wages", "suta"):
        op.add_column("payroll_run", sa.Column(name, money, nullable=False, server_default="0.00"))
    op.add_column(
        "payroll_run",
        sa.Column("suta_rate", sa.Numeric(8, 6), nullable=False, server_default="0"),
    )
    # New rows get their values from the application (status 'draft', zero figures).
    for name in ("status", "futa_wages", "futa", "suta_wages", "suta", "suta_rate"):
        op.alter_column("payroll_run", name, server_default=None)

    op.create_foreign_key(
        "fk_payroll_run_net_pay_account_id",
        "payroll_run",
        "account",
        ["net_pay_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_payroll_run_entry_id",
        "payroll_run",
        "journal_entry",
        ["entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_payroll_run_void_entry_id",
        "payroll_run",
        "journal_entry",
        ["void_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_payroll_run_status",
        "payroll_run",
        "status IN ('draft', 'approved', 'void', 'imported')",
    )
    op.create_check_constraint(
        "ck_payroll_run_approved_fields",
        "payroll_run",
        "status <> 'approved' OR (pay_date IS NOT NULL AND entry_id IS NOT NULL "
        "AND net_pay_source IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_payroll_run_net_pay_source",
        "payroll_run",
        "(net_pay_source IS NULL AND net_pay_account_id IS NULL) "
        "OR (net_pay_source IS NOT NULL AND net_pay_source = 'owner' AND net_pay_account_id IS NULL) "
        "OR (net_pay_source IS NOT NULL AND net_pay_source = 'account' AND net_pay_account_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_payroll_run_void_fields",
        "payroll_run",
        "status <> 'void' OR (pay_date IS NOT NULL AND entry_id IS NOT NULL "
        "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL AND void_date >= pay_date)",
    )
    op.create_check_constraint(
        "ck_payroll_run_no_entry_fields",
        "payroll_run",
        "status NOT IN ('draft', 'imported') OR (entry_id IS NULL "
        "AND void_entry_id IS NULL AND void_date IS NULL)",
    )
    op.create_check_constraint(
        "ck_payroll_run_approved_no_void",
        "payroll_run",
        "status <> 'approved' OR (void_entry_id IS NULL AND void_date IS NULL)",
    )
    op.create_check_constraint(
        "ck_payroll_run_unemployment_nonneg",
        "payroll_run",
        "futa_wages >= 0 AND futa >= 0 AND suta_wages >= 0 AND suta >= 0",
    )
    op.create_index(
        "uq_payroll_run_period_active",
        "payroll_run",
        ["pay_period"],
        unique=True,
        postgresql_where=sa.text("status <> 'void'"),
    )

    # A non-draft run is frozen: only approved -> void (with the void fields) is allowed.
    allowed = " - ".join(f"'{c}'" for c in _ALLOWED_ON_APPROVED)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION payroll_run_guard_update() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'draft' THEN
                -- a draft is edited freely and can only become approved
                IF NEW.status NOT IN ('draft', 'approved') THEN
                    RAISE EXCEPTION 'payroll_run %: status draft -> % is not allowed',
                        OLD.id, NEW.status;
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status
               AND NOT (OLD.status = 'approved' AND NEW.status = 'void') THEN
                RAISE EXCEPTION 'payroll_run %: status % -> % is not allowed',
                    OLD.id, OLD.status, NEW.status;
            END IF;
            IF OLD.status <> 'approved'
               AND (NEW.void_entry_id IS DISTINCT FROM OLD.void_entry_id
                    OR NEW.void_date IS DISTINCT FROM OLD.void_date) THEN
                RAISE EXCEPTION 'payroll_run % is % and cannot change', OLD.id, OLD.status;
            END IF;
            -- Every column except the allowed ones must be unchanged (a column added
            -- later is therefore frozen by default).
            IF to_jsonb(NEW) - {allowed}
               IS DISTINCT FROM to_jsonb(OLD) - {allowed} THEN
                RAISE EXCEPTION 'payroll_run % is % and its figures cannot change',
                    OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER payroll_run_guard_update BEFORE UPDATE ON payroll_run "
        "FOR EACH ROW EXECUTE FUNCTION payroll_run_guard_update()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION payroll_run_guard_delete() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' THEN
                RAISE EXCEPTION 'payroll_run %: only a draft run can be deleted (status is %)',
                    OLD.id, OLD.status;
            END IF;
            RETURN OLD;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER payroll_run_guard_delete BEFORE DELETE ON payroll_run "
        "FOR EACH ROW EXECUTE FUNCTION payroll_run_guard_delete()"
    )

    # --- payroll_settings: rates per tax year ---
    op.create_table(
        "payroll_settings",
        *_base(),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("futa_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("futa_wage_base", money, nullable=False),
        sa.Column("suta_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("suta_wage_base", money, nullable=False),
        sa.CheckConstraint(
            "futa_rate BETWEEN 0 AND 1 AND suta_rate BETWEEN 0 AND 1",
            name="ck_payroll_settings_rates",
        ),
        sa.CheckConstraint(
            "futa_wage_base >= 0 AND suta_wage_base >= 0", name="ck_payroll_settings_bases"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tax_year", name="uq_payroll_settings_tax_year"),
    )

    # --- payroll_deposit_rules: singleton ---
    op.create_table(
        "payroll_deposit_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        *[
            col
            for agency in ("irs_941", "ga_dor", "futa", "ga_dol")
            for col in (
                sa.Column(f"{agency}_cadence", sa.String(length=10), nullable=False),
                sa.Column(f"{agency}_due_day", sa.Integer(), nullable=False),
            )
        ],
        sa.CheckConstraint("id = 1", name="ck_payroll_deposit_rules_singleton"),
        *[
            check
            for agency in ("irs_941", "ga_dor", "futa", "ga_dol")
            for check in (
                sa.CheckConstraint(
                    f"{agency}_cadence IN ('monthly', 'quarterly')",
                    name=f"ck_payroll_deposit_rules_{agency}_cadence",
                ),
                sa.CheckConstraint(
                    f"{agency}_due_day BETWEEN 0 AND 31",
                    name=f"ck_payroll_deposit_rules_{agency}_due_day",
                ),
            )
        ],
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO payroll_deposit_rules (id, irs_941_cadence, irs_941_due_day, "
        "ga_dor_cadence, ga_dor_due_day, futa_cadence, futa_due_day, "
        "ga_dol_cadence, ga_dol_due_day) VALUES "
        "(1, 'monthly', 15, 'monthly', 15, 'quarterly', 0, 'quarterly', 0)"
    )

    # --- tax_payment ---
    op.create_table(
        "tax_payment",
        *_base(),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("agency", sa.String(length=10), nullable=False),
        sa.Column("bank_account_id", sa.Integer(), nullable=False),
        sa.Column("amount", money, nullable=False),
        sa.Column("memo", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("void_entry_id", sa.Integer(), nullable=True),
        sa.Column("void_date", sa.Date(), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_tax_payment_amount"),
        sa.CheckConstraint(
            "agency IN ('irs_941', 'irs_futa', 'ga_dor', 'ga_dol')", name="ck_tax_payment_agency"
        ),
        sa.CheckConstraint("status IN ('posted', 'void')", name="ck_tax_payment_status"),
        sa.CheckConstraint(
            "(status = 'posted' AND void_entry_id IS NULL AND void_date IS NULL) "
            "OR (status = 'void' AND entry_id IS NOT NULL "
            "AND void_entry_id IS NOT NULL AND void_date IS NOT NULL)",
            name="ck_tax_payment_void_fields",
        ),
        sa.CheckConstraint(
            "void_date IS NULL OR void_date >= payment_date", name="ck_tax_payment_void_date"
        ),
        _fk("bank_account_id", "account.id"),
        _fk("entry_id", "journal_entry.id"),
        _fk("void_entry_id", "journal_entry.id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tax_payment_agency_date", "tax_payment", ["agency", "payment_date"])

    _bind_payroll_roles()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM payroll_run "
            "WHERE status IN ('approved', 'void') OR entry_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError(
            f"Refusing to downgrade {revision}: a payroll run is approved or void or has a "
            "journal entry. Dropping the columns would destroy those links and leave "
            "'payroll_run' journal entries that the earlier code cannot read. Restore a "
            "backup taken before the upgrade instead."
        )
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM tax_payment)")).scalar():
        raise RuntimeError(
            f"Refusing to downgrade {revision}: the tax_payment table has rows. Dropping it "
            "would destroy that data and leave 'tax_payment' journal entries that the earlier "
            "code cannot read. Restore a backup taken before the upgrade instead."
        )
    op.drop_index("ix_tax_payment_agency_date", table_name="tax_payment")
    op.drop_table("tax_payment")
    op.drop_table("payroll_deposit_rules")
    op.drop_table("payroll_settings")
    op.execute("DROP TRIGGER payroll_run_guard_delete ON payroll_run")
    op.execute("DROP FUNCTION payroll_run_guard_delete()")
    op.execute("DROP TRIGGER payroll_run_guard_update ON payroll_run")
    op.execute("DROP FUNCTION payroll_run_guard_update()")
    op.drop_index("uq_payroll_run_period_active", table_name="payroll_run")
    for name in (
        "ck_payroll_run_unemployment_nonneg",
        "ck_payroll_run_approved_no_void",
        "ck_payroll_run_no_entry_fields",
        "ck_payroll_run_void_fields",
        "ck_payroll_run_net_pay_source",
        "ck_payroll_run_approved_fields",
        "ck_payroll_run_status",
    ):
        op.drop_constraint(name, "payroll_run", type_="check")
    for name in (
        "fk_payroll_run_void_entry_id",
        "fk_payroll_run_entry_id",
        "fk_payroll_run_net_pay_account_id",
    ):
        op.drop_constraint(name, "payroll_run", type_="foreignkey")
    for name in (
        "suta_rate",
        "suta",
        "suta_wages",
        "futa",
        "futa_wages",
        "void_date",
        "void_entry_id",
        "entry_id",
        "net_pay_account_id",
        "net_pay_source",
        "pay_date",
        "status",
    ):
        op.drop_column("payroll_run", name)
