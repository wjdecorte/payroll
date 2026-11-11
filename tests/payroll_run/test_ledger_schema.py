"""
Payroll ledger schema: run lifecycle CHECKs, the freeze triggers, the one-run-per-period
index, settings, deposit rules, tax payments, and the migration itself.

Each ``_refused`` runs the statement in a savepoint, so a rejected statement leaves the
rest of the test's transaction usable.
"""

from datetime import date
from decimal import Decimal
from itertools import count

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from payroll.migrations import alembic_runner
from payroll.payroll_run.ledger_models import PayrollDepositRules, PayrollSettings, TaxPayment
from payroll.payroll_run.models import PayrollRun

D = Decimal
JAN = date(2026, 1, 15)
PRE_REVISION = "d4e0a2b8c6f1"  # head before the payroll ledger migration
NEW_TABLES = {"payroll_settings", "payroll_deposit_rules", "tax_payment"}
NEW_RUN_COLUMNS = {
    "status",
    "pay_date",
    "net_pay_source",
    "net_pay_account_id",
    "entry_id",
    "void_entry_id",
    "void_date",
    "futa_wages",
    "futa",
    "suta_wages",
    "suta",
    "suta_rate",
}
_periods = count(1)


def _refused(session, statement, match, exc=DBAPIError, **params):
    """Run ``statement`` in a savepoint and assert the database refuses it."""
    with pytest.raises(exc, match=match):
        with session.begin_nested():
            session.execute(sa.text(statement), params)


def _run(session, **kw) -> PayrollRun:
    kw.setdefault("pay_period", f"2030-{next(_periods):02d}")
    kw.setdefault("gross_salary", D("8000.00"))
    row = PayrollRun(**kw)
    session.add(row)
    session.flush()
    return row


def _approved(session, entries, **kw) -> PayrollRun:
    kw.setdefault("pay_date", JAN)
    kw.setdefault("net_pay_source", "owner")
    return _run(session, status="approved", entry_id=entries(), **kw)


def _void(session, entries, **kw) -> PayrollRun:
    kw.setdefault("pay_date", JAN)
    kw.setdefault("net_pay_source", "owner")
    kw.setdefault("void_date", JAN)
    return _run(session, status="void", entry_id=entries(), void_entry_id=entries(), **kw)


def _update(session, run, set_clause, **params):
    session.execute(
        sa.text(f"UPDATE payroll_run SET {set_clause} WHERE id = :id"), {"id": run.id, **params}
    )


# --------------------------------------------------------------------------- run CHECKs


def test_new_run_defaults_to_draft_with_zero_unemployment(session, draft_run):
    assert draft_run.status == "draft"
    assert draft_run.pay_date is None
    assert (draft_run.futa_wages, draft_run.futa, draft_run.suta_wages, draft_run.suta) == (
        D("0.00"),
    ) * 4
    assert draft_run.suta_rate == D("0")


def test_run_status_must_be_known(session):
    with pytest.raises(IntegrityError, match="ck_payroll_run_status"):
        _run(session, status="posted")
    session.rollback()


@pytest.mark.parametrize("missing", ["pay_date", "entry_id", "net_pay_source"])
def test_approved_run_requires_pay_date_entry_and_source(session, entries, missing):
    fields = {"pay_date": JAN, "entry_id": entries(), "net_pay_source": "owner"}
    fields[missing] = None
    with pytest.raises(IntegrityError, match="ck_payroll_run_approved_fields"):
        _run(session, status="approved", **fields)
    session.rollback()


def test_approved_run_with_all_fields_is_accepted(session, entries):
    assert _approved(session, entries).status == "approved"


def test_net_pay_source_owner_must_have_no_account(session, entries, bank):
    with pytest.raises(IntegrityError, match="ck_payroll_run_net_pay_source"):
        _approved(session, entries, net_pay_source="owner", net_pay_account_id=bank.id)
    session.rollback()


def test_net_pay_source_account_requires_an_account(session, entries):
    with pytest.raises(IntegrityError, match="ck_payroll_run_net_pay_source"):
        _approved(session, entries, net_pay_source="account", net_pay_account_id=None)
    session.rollback()


def test_net_pay_source_must_be_known(session, entries):
    with pytest.raises(IntegrityError, match="ck_payroll_run_net_pay_source"):
        _approved(session, entries, net_pay_source="cash")
    session.rollback()


def test_net_pay_account_without_a_source_is_refused(session, bank):
    with pytest.raises(IntegrityError, match="ck_payroll_run_net_pay_source"):
        _run(session, net_pay_account_id=bank.id)
    session.rollback()


def test_net_pay_source_account_with_account_is_accepted(session, entries, bank):
    run = _approved(session, entries, net_pay_source="account", net_pay_account_id=bank.id)
    assert run.net_pay_account_id == bank.id


@pytest.mark.parametrize("missing", ["entry_id", "void_entry_id", "void_date", "pay_date"])
def test_void_run_requires_entries_and_dates(session, entries, missing):
    fields = {
        "pay_date": JAN,
        "entry_id": entries(),
        "void_entry_id": entries(),
        "void_date": JAN,
    }
    fields[missing] = None
    with pytest.raises(IntegrityError, match="ck_payroll_run_void_fields"):
        _run(session, status="void", net_pay_source="owner", **fields)
    session.rollback()


def test_void_date_before_pay_date_refused(session, entries):
    with pytest.raises(IntegrityError, match="ck_payroll_run_void_fields"):
        _void(session, entries, void_date=date(2026, 1, 14))
    session.rollback()


def test_void_run_with_all_fields_is_accepted(session, entries):
    assert _void(session, entries, void_date=date(2026, 1, 16)).status == "void"


@pytest.mark.parametrize("status", ["draft", "imported"])
@pytest.mark.parametrize("field", ["entry_id", "void_entry_id", "void_date"])
def test_draft_and_imported_carry_no_entry_or_void_fields(session, entries, status, field):
    value = JAN if field == "void_date" else entries()
    with pytest.raises(IntegrityError, match="ck_payroll_run_no_entry_fields"):
        _run(session, status=status, **{field: value})
    session.rollback()


@pytest.mark.parametrize("field", ["void_entry_id", "void_date"])
def test_approved_run_carries_no_void_fields(session, entries, field):
    value = JAN if field == "void_date" else entries()
    with pytest.raises(IntegrityError, match="ck_payroll_run_approved_no_void"):
        _approved(session, entries, **{field: value})
    session.rollback()


@pytest.mark.parametrize("column", ["futa_wages", "futa", "suta_wages", "suta"])
def test_unemployment_figures_cannot_be_negative(session, column):
    with pytest.raises(IntegrityError, match="ck_payroll_run_unemployment_nonneg"):
        _run(session, **{column: D("-0.01")})
    session.rollback()


# ------------------------------------------------------------------ freeze triggers


def test_draft_can_be_edited_and_approved(session, draft_run, entries):
    _update(session, draft_run, "gross_salary = 9000.00, futa = 1.00")
    entry = entries()
    _update(
        session,
        draft_run,
        "status = 'approved', pay_date = :d, net_pay_source = 'owner', entry_id = :e",
        d=JAN,
        e=entry,
    )
    session.refresh(draft_run)
    assert (draft_run.status, draft_run.entry_id, draft_run.gross_salary) == (
        "approved",
        entry,
        D("9000.00"),
    )


@pytest.mark.parametrize("target", ["void", "imported"])
def test_draft_can_only_become_approved(session, draft_run, entries, target):
    _refused(
        session,
        f"UPDATE payroll_run SET status = '{target}', pay_date = :d, entry_id = :e, "
        "void_entry_id = :v, void_date = :d, net_pay_source = 'owner' WHERE id = :id"
        if target == "void"
        else "UPDATE payroll_run SET status = 'imported' WHERE id = :id",
        f"status draft -> {target} is not allowed",
        id=draft_run.id,
        d=JAN,
        e=entries(),
        v=entries(),
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "pay_period = '2031-01'",
        "gross_salary = 1.00",
        "health_insurance = 1.00",
        "health_in_income_tax = false",
        "ytd_fica_before = 1.00",
        "notes = 'changed'",
        "fica_wages = 1.00",
        "ss_ee = 1.00",
        "medicare_er = 1.00",
        "federal_withholding = 1.00",
        "ga_withholding = 1.00",
        "net_pay = 1.00",
        "total_employer_cost = 1.00",
        "ytd_fica_after = 1.00",
        "futa_wages = 1.00",
        "futa = 1.00",
        "suta_wages = 1.00",
        "suta = 1.00",
        "suta_rate = 0.027000",
        "tax_year = 2027",
        "pay_date = '2026-02-01'",
        "net_pay_source = 'account'",
        "net_pay_account_id = 1",
        "entry_id = 1",
        "create_date = now()",
    ],
)
@pytest.mark.parametrize("kind", ["approved", "void", "imported"])
def test_non_draft_run_figures_cannot_change(session, entries, kind, assignment):
    run = (
        _approved(session, entries)
        if kind == "approved"
        else _void(session, entries)
        if kind == "void"
        else _run(session, status="imported")
    )
    _refused(
        session,
        f"UPDATE payroll_run SET {assignment} WHERE id = :id",
        "figures cannot change",
        id=run.id,
    )


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("approved", "draft"),
        ("approved", "imported"),
        ("imported", "approved"),
        ("imported", "draft"),
        ("void", "approved"),
        ("void", "draft"),
    ],
)
def test_status_moves_other_than_approved_to_void_refused(session, entries, kind, target):
    run = (
        _approved(session, entries)
        if kind == "approved"
        else _void(session, entries)
        if kind == "void"
        else _run(session, status="imported")
    )
    _refused(
        session,
        f"UPDATE payroll_run SET status = '{target}' WHERE id = :id",
        f"status {kind} -> {target} is not allowed",
        id=run.id,
    )


def test_approved_run_can_be_voided_with_void_fields(session, entries):
    run = _approved(session, entries)
    _update(
        session,
        run,
        "status = 'void', void_entry_id = :v, void_date = :d, modify_date = now()",
        v=entries(),
        d=date(2026, 1, 20),
    )
    session.refresh(run)
    assert run.status == "void"
    assert run.void_date == date(2026, 1, 20)


def test_voiding_without_void_fields_hits_the_check(session, entries):
    run = _approved(session, entries)
    _refused(
        session,
        "UPDATE payroll_run SET status = 'void' WHERE id = :id",
        "ck_payroll_run_void_fields",
        exc=IntegrityError,
        id=run.id,
    )


def test_approved_run_modify_date_may_change(session, entries):
    run = _approved(session, entries)
    _update(session, run, "modify_date = now()")  # no error


def test_void_fields_of_a_void_run_are_frozen(session, entries):
    run = _void(session, entries)
    _refused(
        session,
        "UPDATE payroll_run SET void_date = '2026-02-01' WHERE id = :id",
        "is void and cannot change",
        id=run.id,
    )


def test_void_fields_cannot_be_set_on_an_imported_run(session):
    run = _run(session, status="imported")
    _refused(
        session,
        "UPDATE payroll_run SET void_date = '2026-02-01' WHERE id = :id",
        "is imported and cannot change",
        id=run.id,
    )


def test_draft_can_be_deleted(session, draft_run):
    session.execute(sa.text("DELETE FROM payroll_run WHERE id = :id"), {"id": draft_run.id})
    left = session.scalar(
        sa.text("SELECT count(*) FROM payroll_run WHERE id = :id"), {"id": draft_run.id}
    )
    assert left == 0


@pytest.mark.parametrize("kind", ["approved", "void", "imported"])
def test_non_draft_run_cannot_be_deleted(session, entries, kind):
    run = (
        _approved(session, entries)
        if kind == "approved"
        else _void(session, entries)
        if kind == "void"
        else _run(session, status="imported")
    )
    _refused(
        session,
        "DELETE FROM payroll_run WHERE id = :id",
        f"only a draft run can be deleted \\(status is {kind}\\)",
        id=run.id,
    )


# ---------------------------------------------------------- one non-void run per period


def test_one_non_void_run_per_period(session):
    _run(session, pay_period="2031-05")
    with pytest.raises(IntegrityError, match="uq_payroll_run_period_active"):
        _run(session, pay_period="2031-05")
    session.rollback()


def test_imported_run_blocks_a_second_run_in_its_period(session):
    _run(session, pay_period="2031-05", status="imported")
    with pytest.raises(IntegrityError, match="uq_payroll_run_period_active"):
        _run(session, pay_period="2031-05")
    session.rollback()


def test_period_is_free_again_once_the_run_is_void(session, entries):
    _void(session, entries, pay_period="2031-05")
    _void(session, entries, pay_period="2031-05")  # several void runs coexist
    assert _run(session, pay_period="2031-05").status == "draft"
    with pytest.raises(IntegrityError, match="uq_payroll_run_period_active"):
        _approved(session, entries, pay_period="2031-05")
    session.rollback()


def test_voiding_the_run_frees_its_period(session, entries):
    run = _approved(session, entries, pay_period="2031-06")
    _update(
        session,
        run,
        "status = 'void', void_entry_id = :v, void_date = :d",
        v=entries(),
        d=JAN,
    )
    assert _run(session, pay_period="2031-06").status == "draft"


# --------------------------------------------------------------------- settings tables


def _settings(session, **kw) -> PayrollSettings:
    kw.setdefault("tax_year", 2026)
    kw.setdefault("futa_rate", D("0.006"))
    kw.setdefault("futa_wage_base", D("7000.00"))
    kw.setdefault("suta_rate", D("0.027"))
    kw.setdefault("suta_wage_base", D("9500.00"))
    row = PayrollSettings(**kw)
    session.add(row)
    session.flush()
    return row


def test_payroll_settings_round_trip_and_unique_per_year(session):
    row = _settings(session)
    session.refresh(row)
    assert (row.futa_rate, row.suta_rate) == (D("0.006000"), D("0.027000"))
    with pytest.raises(IntegrityError, match="uq_payroll_settings_tax_year"):
        _settings(session)
    session.rollback()


def test_payroll_settings_second_year_allowed(session):
    _settings(session)
    assert _settings(session, tax_year=2027).tax_year == 2027


@pytest.mark.parametrize("column", ["futa_rate", "suta_rate"])
@pytest.mark.parametrize("value", [D("-0.000001"), D("1.000001")])
def test_payroll_settings_rates_between_zero_and_one(session, column, value):
    with pytest.raises(IntegrityError, match="ck_payroll_settings_rates"):
        _settings(session, **{column: value})
    session.rollback()


@pytest.mark.parametrize("column", ["futa_wage_base", "suta_wage_base"])
def test_payroll_settings_bases_not_negative(session, column):
    with pytest.raises(IntegrityError, match="ck_payroll_settings_bases"):
        _settings(session, **{column: D("-1.00")})
    session.rollback()


def test_deposit_rules_singleton_row_has_the_defaults(session):
    rows = session.scalars(sa.select(PayrollDepositRules)).all()
    assert len(rows) == 1
    rules = rows[0]
    assert rules.id == 1
    assert (rules.irs_941_cadence, rules.irs_941_due_day) == ("monthly", 15)
    assert (rules.ga_dor_cadence, rules.ga_dor_due_day) == ("monthly", 15)
    assert (rules.futa_cadence, rules.futa_due_day) == ("quarterly", 0)
    assert (rules.ga_dol_cadence, rules.ga_dol_due_day) == ("quarterly", 0)


def test_deposit_rules_cannot_have_a_second_row(session):
    with pytest.raises(IntegrityError, match="ck_payroll_deposit_rules_singleton"):
        session.add(PayrollDepositRules(id=2))
        session.flush()
    session.rollback()


def test_deposit_rules_cannot_duplicate_the_singleton(session):
    with pytest.raises(IntegrityError, match="payroll_deposit_rules_pkey"):
        session.add(PayrollDepositRules(id=1))
        session.flush()
    session.rollback()


@pytest.mark.parametrize("agency", ["irs_941", "ga_dor", "futa", "ga_dol"])
def test_deposit_rules_cadence_and_due_day_checks(session, agency):
    _refused(
        session,
        f"UPDATE payroll_deposit_rules SET {agency}_cadence = 'weekly'",
        f"ck_payroll_deposit_rules_{agency}_cadence",
        exc=IntegrityError,
    )
    for bad in (-1, 32):
        _refused(
            session,
            f"UPDATE payroll_deposit_rules SET {agency}_due_day = {bad}",
            f"ck_payroll_deposit_rules_{agency}_due_day",
            exc=IntegrityError,
        )
    session.execute(
        sa.text(
            f"UPDATE payroll_deposit_rules SET {agency}_due_day = 31, {agency}_cadence = 'monthly'"
        )
    )


# ----------------------------------------------------------------------- tax payments


def _payment(session, bank, **kw) -> TaxPayment:
    kw.setdefault("payment_date", JAN)
    kw.setdefault("agency", "irs_941")
    kw.setdefault("amount", D("100.00"))
    row = TaxPayment(bank_account_id=bank.id, **kw)
    session.add(row)
    session.flush()
    return row


def test_tax_payment_posted_is_accepted(session, bank, entries):
    row = _payment(session, bank, entry_id=entries())
    assert (row.status, row.void_entry_id) == ("posted", None)


@pytest.mark.parametrize("amount", [D("0.00"), D("-1.00")])
def test_tax_payment_amount_must_be_positive(session, bank, amount):
    with pytest.raises(IntegrityError, match="ck_tax_payment_amount"):
        _payment(session, bank, amount=amount)
    session.rollback()


def test_tax_payment_agency_must_be_known(session, bank):
    with pytest.raises(IntegrityError, match="ck_tax_payment_agency"):
        _payment(session, bank, agency="ga_other")
    session.rollback()


@pytest.mark.parametrize("agency", ["irs_941", "irs_futa", "ga_dor", "ga_dol"])
def test_tax_payment_known_agencies_accepted(session, bank, agency):
    assert _payment(session, bank, agency=agency).agency == agency


def test_tax_payment_status_must_be_known(session, bank):
    with pytest.raises(IntegrityError, match="ck_tax_payment_status"):
        _payment(session, bank, status="pending")
    session.rollback()


def test_tax_payment_posted_carries_no_void_fields(session, bank, entries):
    with pytest.raises(IntegrityError, match="ck_tax_payment_void_fields"):
        _payment(session, bank, entry_id=entries(), void_date=JAN)
    session.rollback()


@pytest.mark.parametrize("missing", ["entry_id", "void_entry_id", "void_date"])
def test_tax_payment_void_requires_all_fields(session, bank, entries, missing):
    fields = {"entry_id": entries(), "void_entry_id": entries(), "void_date": JAN}
    fields[missing] = None
    with pytest.raises(IntegrityError, match="ck_tax_payment_void_fields"):
        _payment(session, bank, status="void", **fields)
    session.rollback()


def test_tax_payment_void_date_not_before_payment_date(session, bank, entries):
    with pytest.raises(IntegrityError, match="ck_tax_payment_void_date"):
        _payment(
            session,
            bank,
            status="void",
            entry_id=entries(),
            void_entry_id=entries(),
            void_date=date(2026, 1, 14),
        )
    session.rollback()


def test_tax_payment_void_is_accepted(session, bank, entries):
    row = _payment(
        session,
        bank,
        status="void",
        entry_id=entries(),
        void_entry_id=entries(),
        void_date=date(2026, 1, 16),
    )
    assert row.status == "void"


# ------------------------------------------------------------------------- migration


def test_migration_head_matches_runner():
    assert alembic_runner._HEAD_REVISION == "e5f1b3c9d7a2"


def test_models_match_migration(scratch_engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == alembic_runner._HEAD_REVISION
        )
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, SQLModel.metadata) == []


_OLD_RUN = {
    "pay_period": "'2025-11'",
    "gross_salary": "8123.45",
    "health_insurance": "501.11",
    "hsa_contribution": "302.22",
    "health_in_income_tax": "true",
    "hsa_in_income_tax": "false",
    "ytd_fica_before": "70000.99",
    "notes": "'a note'",
    "fica_wages": "8000.12",
    "income_tax_wages": "7500.34",
    "ss_taxable_wages": "7000.56",
    "ss_ee": "434.01",
    "ss_er": "434.02",
    "medicare_ee": "101.03",
    "additional_medicare": "9.04",
    "total_medicare_ee": "110.07",
    "medicare_er": "101.05",
    "federal_withholding": "812.06",
    "ga_withholding": "345.07",
    "total_employee_deductions": "1700.08",
    "net_pay": "6423.37",
    "total_employer_taxes": "535.09",
    "total_employer_cost": "8658.54",
    "ytd_fica_after": "78001.11",
    "tax_year": "2025",
}


def _old_run_sql(period: str) -> str:
    values = {**_OLD_RUN, "pay_period": f"'{period}'"}
    cols = ", ".join(["create_date", "modify_date", *values])
    vals = ", ".join(["'2025-12-01 10:00:00'", "'2025-12-02 11:30:00'", *values.values()])
    return f"INSERT INTO payroll_run ({cols}) VALUES ({vals})"


def test_data_step_marks_existing_runs_imported_and_keeps_figures(scratch_engine):
    alembic_runner.upgrade(PRE_REVISION)
    with scratch_engine.begin() as conn:
        columns = [c["name"] for c in sa.inspect(conn).get_columns("payroll_run")]
        assert set(columns) == {"id", "create_date", "modify_date", *_OLD_RUN}  # covers all
        conn.exec_driver_sql(_old_run_sql("2025-11"))
        conn.exec_driver_sql(_old_run_sql("2025-12"))
        select = f"SELECT {', '.join(columns)} FROM payroll_run ORDER BY id"
        before = [tuple(r) for r in conn.exec_driver_sql(select)]
        before_text = [tuple(str(v) for v in r) for r in before]
    assert len(before) == 2

    alembic_runner.upgrade()

    with scratch_engine.connect() as conn:
        after = [tuple(r) for r in conn.exec_driver_sql(select)]
        assert after == before
        assert [tuple(str(v) for v in r) for r in after] == before_text  # scale kept too
        new = conn.exec_driver_sql(
            "SELECT status, pay_date, net_pay_source, net_pay_account_id, entry_id, "
            "void_entry_id, void_date, futa_wages, futa, suta_wages, suta, suta_rate "
            "FROM payroll_run"
        ).all()
        assert (
            new
            == [
                ("imported", None, None, None, None, None, None)
                + (D("0.00"), D("0.00"), D("0.00"), D("0.00"), D("0"))
            ]
            * 2
        )
        # the imported rows are frozen from the moment the migration ends
        with pytest.raises(DBAPIError, match="figures cannot change"):
            conn.exec_driver_sql("UPDATE payroll_run SET net_pay = 1.00")


def test_upgrade_inserts_the_deposit_rules_row(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT * FROM payroll_deposit_rules").all()
    assert row == [(1, "monthly", 15, "monthly", 15, "quarterly", 0, "quarterly", 0)]


def _plant_run(conn, **extra) -> None:
    zero = dict.fromkeys(("futa_wages", "futa", "suta_wages", "suta", "suta_rate"), "0")
    values = {**_OLD_RUN, "status": "'draft'", **zero, **extra}
    cols = ", ".join(["create_date", "modify_date", *values])
    conn.exec_driver_sql(
        "SET LOCAL session_replication_role = replica"  # skip the FK and freeze triggers
    )
    conn.exec_driver_sql(
        f"INSERT INTO payroll_run ({cols}) VALUES (now(), now(), {', '.join(values.values())})"
    )


def _plant(conn, sql: str) -> None:
    # replica role skips the FK and freeze triggers so a bare row can be planted
    conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
    conn.exec_driver_sql(sql)


_RUN_MESSAGE = "a payroll run is approved or void"
_PLANTED = {
    "approved run": (
        {
            "status": "'approved'",
            "pay_date": "'2026-01-31'",
            "net_pay_source": "'owner'",
            "entry_id": "999",
        },
        _RUN_MESSAGE,
    ),
    "void run": (
        {
            "status": "'void'",
            "pay_date": "'2026-01-31'",
            "net_pay_source": "'owner'",
            "entry_id": "998",
            "void_entry_id": "999",
            "void_date": "'2026-02-01'",
        },
        _RUN_MESSAGE,
    ),
}
_TAX_PAYMENT_SQL = (
    "INSERT INTO tax_payment (create_date, modify_date, payment_date, agency, bank_account_id, "
    "amount, status) VALUES (now(), now(), '2026-01-15', 'irs_941', 1, 1, 'posted')"
)


def _assert_still_at_head(scratch_engine) -> None:
    with scratch_engine.connect() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == alembic_runner._HEAD_REVISION
        )
        assert NEW_TABLES <= set(sa.inspect(conn).get_table_names())
        columns = {c["name"] for c in sa.inspect(conn).get_columns("payroll_run")}
        assert NEW_RUN_COLUMNS <= columns  # nothing was dropped


@pytest.mark.parametrize("planted", list(_PLANTED))
def test_downgrade_refuses_with_a_posted_payroll_run(scratch_engine, planted):
    extra, message = _PLANTED[planted]
    alembic_runner.upgrade()
    with scratch_engine.begin() as conn:
        _plant_run(conn, **extra)
    with pytest.raises(RuntimeError, match=message):
        alembic_runner.downgrade(PRE_REVISION)
    _assert_still_at_head(scratch_engine)


def test_downgrade_refuses_with_a_tax_payment(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.begin() as conn:
        _plant(conn, _TAX_PAYMENT_SQL)
    with pytest.raises(RuntimeError, match="the tax_payment table has rows"):
        alembic_runner.downgrade(PRE_REVISION)
    _assert_still_at_head(scratch_engine)


def test_downgrade_drops_everything_when_only_drafts_and_imports_exist(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.begin() as conn:
        _plant_run(conn, pay_period="'2026-01'", status="'imported'")
        _plant_run(conn, pay_period="'2026-02'", status="'draft'")
    alembic_runner.downgrade(PRE_REVISION)
    with scratch_engine.connect() as conn:
        assert (
            conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == PRE_REVISION
        )
        inspector = sa.inspect(conn)
        assert not NEW_TABLES & set(inspector.get_table_names())
        assert not NEW_RUN_COLUMNS & {c["name"] for c in inspector.get_columns("payroll_run")}
        assert not {i["name"] for i in inspector.get_indexes("payroll_run")} & {
            "uq_payroll_run_period_active"
        }
        names = {r[0] for r in conn.exec_driver_sql("SELECT proname FROM pg_proc")}
        assert not {n for n in names if n.startswith("payroll_run_")}
        triggers = {r[0] for r in conn.exec_driver_sql("SELECT tgname FROM pg_trigger")}
        assert not {t for t in triggers if t.startswith("payroll_run_")}
        checks = {
            r[0]
            for r in conn.exec_driver_sql(
                "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_payroll_run_%%'"
            )
        }
        assert not checks
        assert conn.exec_driver_sql("SELECT count(*) FROM payroll_run").scalar_one() == 2

    alembic_runner.upgrade()  # and it can come back
    with scratch_engine.connect() as conn:
        assert NEW_TABLES <= set(sa.inspect(conn).get_table_names())
