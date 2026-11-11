"""The eight payroll account roles: typing, default chart, migration, freeze and races."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlmodel import Session, col, select

from payroll.auth.models import User
from payroll.common.documents import locked_role_account
from payroll.exceptions import AppBaseError
from payroll.frontend.ledger_routers import ROLE_LABELS
from payroll.ledger.accounts import PAYROLL_ROLES, ROLE_TYPES, AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.exceptions import InvalidAccountError
from payroll.ledger.models import Account, AccountType, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.migrations import alembic_runner
from payroll.payroll_run.ledger_models import TaxPayment
from payroll.payroll_run.models import PayrollRun
from tests.conftest import make_user
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
DAY = date(2026, 1, 15)
PRE_REVISION = "d4e0a2b8c6f1"

EXPECTED_VALUES = {
    "PAYROLL_WAGES": "payroll_wages",
    "PAYROLL_TAX_EXPENSE": "payroll_tax_expense",
    "PAYROLL_FEDERAL_PAYABLE": "payroll_federal_payable",
    "PAYROLL_GA_WH_PAYABLE": "payroll_ga_wh_payable",
    "PAYROLL_FUTA_PAYABLE": "payroll_futa_payable",
    "PAYROLL_SUTA_PAYABLE": "payroll_suta_payable",
    "PAYROLL_HEALTH_PAYABLE": "payroll_health_payable",
    "PAYROLL_HSA_PAYABLE": "payroll_hsa_payable",
}
DEFAULT_ACCOUNTS = {
    SystemRole.PAYROLL_WAGES: "Payroll Expenses:Wages",
    SystemRole.PAYROLL_TAX_EXPENSE: "Payroll Expenses:Taxes",
    SystemRole.PAYROLL_FEDERAL_PAYABLE: "Payroll Liabilities:Federal Taxes (941/943/944)",
    SystemRole.PAYROLL_GA_WH_PAYABLE: "Payroll Liabilities:GA Income Tax",
    SystemRole.PAYROLL_FUTA_PAYABLE: "Payroll Liabilities:FUTA Payable",
    SystemRole.PAYROLL_SUTA_PAYABLE: "Payroll Liabilities:GA SUTA Payable",
    SystemRole.PAYROLL_HEALTH_PAYABLE: "Payroll Liabilities:S-Corp Health Insurance",
    SystemRole.PAYROLL_HSA_PAYABLE: "Payroll Liabilities:Co. HSA",
}


def test_role_values_and_types_are_exact():
    assert {r.name: r.value for r in PAYROLL_ROLES} == EXPECTED_VALUES
    assert all(len(r.value) <= 30 for r in SystemRole)
    for role in PAYROLL_ROLES:
        expected = (
            AccountType.EXPENSE
            if role in (SystemRole.PAYROLL_WAGES, SystemRole.PAYROLL_TAX_EXPENSE)
            else AccountType.LIABILITY
        )
        assert ROLE_TYPES[role] == expected


@pytest.mark.parametrize("role", PAYROLL_ROLES)
def test_role_binds_only_to_an_account_of_its_type(session, owner_user, payroll_roles, role):
    service = AccountService(session)
    other = AccountType.ASSET if ROLE_TYPES[role] != AccountType.ASSET else AccountType.LIABILITY
    wrong = service.create(actor=owner_user, name="Wrong type", type=other)
    with pytest.raises(InvalidAccountError) as caught:
        service.bind_role(role, wrong.id, actor=owner_user)
    assert f"Role {role.value} needs an {ROLE_TYPES[role].value} account" in caught.value.message
    right = service.create(actor=owner_user, name="Right type", type=ROLE_TYPES[role])
    service.bind_role(role, right.id, actor=owner_user)
    assert service.account_by_role(role).id == right.id


def test_default_chart_binds_all_eight_roles(session, chart):
    service = AccountService(session)
    for role in PAYROLL_ROLES:
        assert service.account_by_role(role).id == chart[DEFAULT_ACCOUNTS[role]].id


def test_seed_binds_existing_accounts_without_roles_and_keeps_bound_ones(
    session, owner_user, chart
):
    service = AccountService(session)
    keep = service.create(actor=owner_user, name="My own wages", type=AccountType.EXPENSE)
    for role in PAYROLL_ROLES:
        service.bind_role(role, None, actor=owner_user)
    service.bind_role(SystemRole.PAYROLL_WAGES, keep.id, actor=owner_user)

    assert seed_default_chart(session, actor=owner_user) == 0

    assert service.account_by_role(SystemRole.PAYROLL_WAGES).id == keep.id  # left alone
    for role in PAYROLL_ROLES[1:]:
        assert service.account_by_role(role).id == chart[DEFAULT_ACCOUNTS[role]].id


# ---------------------------------------------------------------------------
# Freeze once payroll documents exist
# ---------------------------------------------------------------------------


@pytest.fixture
def spares(session, owner_user, payroll_roles) -> dict[SystemRole, Account]:
    service = AccountService(session)
    return {
        role: service.create(actor=owner_user, name=f"Spare {role.value}", type=ROLE_TYPES[role])
        for role in PAYROLL_ROLES
    }


@pytest.fixture
def entry_id(session, owner_user, chart) -> int:
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id
    draft = EntryDraft(
        DAY,
        [LineDraft(cash, debit=D("1")), LineDraft(sales, credit=D("1"))],
        source_type=SourceType.PAYROLL_RUN,
    )
    return LedgerPostingService(session).post(draft, actor=owner_user).id


def _run(session, entry_id: int | None, status: str, period: str = "2026-01") -> PayrollRun:
    fields: dict = {"pay_period": period, "gross_salary": D("100.00"), "status": status}
    if status in ("approved", "void"):
        fields.update(pay_date=DAY, net_pay_source="owner", entry_id=entry_id)
    if status == "void":
        fields.update(void_entry_id=entry_id, void_date=DAY)
    run = PayrollRun(**fields)
    session.add(run)
    session.flush()
    return run


def _assert_frozen(session, owner_user, payroll_roles, spares):
    service = AccountService(session)
    for role in PAYROLL_ROLES:
        for target in (spares[role].id, None):
            with pytest.raises(InvalidAccountError) as caught:
                service.bind_role(role, target, actor=owner_user)
            assert role.value in caught.value.message
            assert "cannot change after payroll runs" in caught.value.message
        assert service.account_by_role(role).id == payroll_roles[role].id
        service.bind_role(role, payroll_roles[role].id, actor=owner_user)  # no-op rebind


def test_roles_move_unbind_and_bind_first_time_before_any_document(
    session, owner_user, payroll_roles, spares
):
    service = AccountService(session)
    for role in PAYROLL_ROLES:
        service.bind_role(role, spares[role].id, actor=owner_user)
        service.bind_role(role, None, actor=owner_user)
        assert service.account_by_role(role) is None
        service.bind_role(role, payroll_roles[role].id, actor=owner_user)


@pytest.mark.parametrize("status", ["draft", "imported"])
def test_drafts_and_imported_runs_do_not_freeze(session, owner_user, payroll_roles, spares, status):
    _run(session, None, status)
    AccountService(session).bind_role(
        SystemRole.PAYROLL_WAGES, spares[SystemRole.PAYROLL_WAGES].id, actor=owner_user
    )


@pytest.mark.parametrize("status", ["approved", "void"])
def test_an_approved_or_void_run_freezes_every_payroll_role(
    session, owner_user, payroll_roles, spares, entry_id, status
):
    _run(session, entry_id, status)
    _assert_frozen(session, owner_user, payroll_roles, spares)
    # other roles stay movable
    AccountService(session).bind_role(SystemRole.UNDEPOSITED_FUNDS, None, actor=owner_user)


def test_a_tax_payment_freezes_every_payroll_role(
    session, owner_user, payroll_roles, spares, entry_id, chart
):
    session.add(
        TaxPayment(
            payment_date=DAY,
            agency="irs_941",
            bank_account_id=chart["Business Checking"].id,
            amount=D("10.00"),
            status="posted",
            entry_id=entry_id,
        )
    )
    session.flush()
    _assert_frozen(session, owner_user, payroll_roles, spares)


# ---------------------------------------------------------------------------
# Migration data step
# ---------------------------------------------------------------------------


def _roles_after_upgrade(scratch_engine, accounts: list[tuple]) -> dict[str, str]:
    """Upgrade to the previous head, insert ``accounts`` (name, parent, type, active, role)."""
    alembic_runner.upgrade(PRE_REVISION)
    with scratch_engine.begin() as conn:
        ids: dict[str, int] = {}
        for name, parent, type_, active, role in accounts:
            ids[name] = conn.exec_driver_sql(
                "INSERT INTO account (create_date, modify_date, name, parent_id, type, "
                "is_active, system_role) VALUES (now(), now(), %s, %s, %s, %s, %s) RETURNING id",
                (name, ids.get(parent), type_, active, role),
            ).scalar_one()
    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        return dict(
            conn.exec_driver_sql(
                "SELECT system_role, name FROM account WHERE system_role LIKE 'payroll_%%'"
            ).all()
        )


FULL_DEFAULTS = [
    ("Payroll Expenses", None, "expense", True, None),
    ("Wages", "Payroll Expenses", "expense", True, None),
    ("Taxes", "Payroll Expenses", "expense", True, None),
    ("Payroll Liabilities", None, "liability", True, None),
    ("Federal Taxes (941/943/944)", "Payroll Liabilities", "liability", True, None),
    ("GA Income Tax", "Payroll Liabilities", "liability", True, None),
    ("S-Corp Health Insurance", "Payroll Liabilities", "liability", True, None),
    ("Co. HSA", "Payroll Liabilities", "liability", True, None),
    ("FUTA Payable", "Payroll Liabilities", "liability", True, None),
    ("GA SUTA Payable", "Payroll Liabilities", "liability", True, None),
]


def test_migration_binds_the_exact_default_accounts(scratch_engine):
    bound = _roles_after_upgrade(scratch_engine, FULL_DEFAULTS)
    assert bound == {
        "payroll_wages": "Wages",
        "payroll_tax_expense": "Taxes",
        "payroll_federal_payable": "Federal Taxes (941/943/944)",
        "payroll_ga_wh_payable": "GA Income Tax",
        "payroll_futa_payable": "FUTA Payable",
        "payroll_suta_payable": "GA SUTA Payable",
        "payroll_health_payable": "S-Corp Health Insurance",
        "payroll_hsa_payable": "Co. HSA",
    }


def test_migration_binds_nothing_without_the_exact_accounts(scratch_engine):
    accounts = [
        ("Payroll Expenses", None, "expense", True, None),
        ("Wages", "Payroll Expenses", "expense", True, None),  # fine
        ("Taxes", None, "expense", True, None),  # wrong parent (top level)
        ("Payroll Liabilities", None, "liability", True, None),
        ("FUTA Payable", "Payroll Liabilities", "expense", True, None),  # wrong type
        ("GA SUTA Payable", "Payroll Liabilities", "liability", False, None),  # inactive
        ("Co. HSA", "Payroll Liabilities", "liability", True, "due_to_owner"),  # holds a role
    ]
    assert _roles_after_upgrade(scratch_engine, accounts) == {"payroll_wages": "Wages"}


def test_migration_leaves_an_already_bound_role_alone(scratch_engine):
    accounts = [
        *FULL_DEFAULTS[:3],
        ("Mine", None, "expense", True, "payroll_wages"),
    ]
    assert _roles_after_upgrade(scratch_engine, accounts) == {
        "payroll_wages": "Mine",  # not moved to "Wages"
        "payroll_tax_expense": "Taxes",
    }


# ---------------------------------------------------------------------------
# Settings > Ledger page
# ---------------------------------------------------------------------------


def test_settings_page_lists_all_eight_roles_with_labels(owner_client, chart):
    page = owner_client.get("/settings/ledger")
    assert page.status_code == 200
    for role in PAYROLL_ROLES:
        assert ROLE_LABELS[role] in page.text
        assert f'name="role_{role.value}"' in page.text


def test_settings_page_can_rebind_a_payroll_role_before_documents(
    owner_client, session, owner_user, chart
):
    spare = AccountService(session).create(
        actor=owner_user, name="Other wages", type=AccountType.EXPENSE
    )
    response = owner_client.post(
        "/settings/ledger/roles", data={"role_payroll_wages": str(spare.id)}
    )
    assert response.status_code == 200
    assert AccountService(session).account_by_role(SystemRole.PAYROLL_WAGES).id == spare.id


# ---------------------------------------------------------------------------
# Real two-session races
# ---------------------------------------------------------------------------


@pytest.fixture
def race_db(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user = make_user(db)
        wages = Account(
            name="Wages", type=AccountType.EXPENSE, system_role=SystemRole.PAYROLL_WAGES
        )
        spare = Account(name="Other Wages", type=AccountType.EXPENSE)
        cash = Account(name="Cash", type=AccountType.ASSET)
        sales = Account(name="Sales", type=AccountType.INCOME)
        db.add_all([wages, spare, cash, sales])
        db.flush()
        entry = LedgerPostingService(db).post(
            EntryDraft(
                DAY,
                [LineDraft(cash.id, debit=D("1")), LineDraft(sales.id, credit=D("1"))],
                source_type=SourceType.PAYROLL_RUN,
            ),
            actor=user,
        )
        ids = {"user": user.id, "wages": wages.id, "spare": spare.id, "entry": entry.id}
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=5)
    yield engine, ids
    engine.dispose()


def _wait_for_lock_waiter(engine, timeout: float = 10.0) -> None:
    """Block until some backend in the scratch database waits on a row lock."""
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            if waiting:
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError("nothing ever waited on a row lock")


def _missing() -> AppBaseError:
    return AppBaseError(message="role missing")


def _issue_run(engine, ids, *, locked: threading.Event | None, release: threading.Event | None):
    """The stand-in issuer: lock the role account like a document does, then insert a run."""
    with Session(engine) as db:
        account = locked_role_account(db, SystemRole.PAYROLL_WAGES, _missing)
        if locked is not None:
            locked.set()
        if release is not None:
            assert release.wait(timeout=30)
        db.add(
            PayrollRun(
                pay_period="2026-01",
                gross_salary=D("100.00"),
                status="approved",
                pay_date=DAY,
                net_pay_source="owner",
                entry_id=ids["entry"],
            )
        )
        db.commit()
        return account.id


def _rebind(engine, ids, outcome: dict) -> None:
    with Session(engine) as db:
        try:
            AccountService(db).bind_role(
                SystemRole.PAYROLL_WAGES, ids["spare"], actor=db.get(User, ids["user"])
            )
            outcome["rebind"] = "ok"
        except AppBaseError as exc:
            outcome["rebind"] = type(exc).__name__


def _bound_id(engine) -> int:
    with Session(engine) as db:
        return db.exec(
            select(Account.id).where(col(Account.system_role) == SystemRole.PAYROLL_WAGES)
        ).one()


def test_rebind_started_mid_issue_waits_then_is_refused(race_db):
    """The rebind waits for the issuer's lock; once the run commits it sees it and refuses."""
    engine, ids = race_db
    locked, release = threading.Event(), threading.Event()
    issued: dict = {}
    outcome: dict = {}
    issuer = threading.Thread(
        target=lambda: issued.update(id=_issue_run(engine, ids, locked=locked, release=release))
    )
    issuer.start()
    assert locked.wait(timeout=30)
    rebinder = threading.Thread(target=_rebind, args=(engine, ids, outcome))
    rebinder.start()
    _wait_for_lock_waiter(engine)  # the rebind is blocked on the role row, not racing past it
    assert rebinder.is_alive() and not outcome
    release.set()
    for t in (issuer, rebinder):
        t.join(timeout=30)
        assert not t.is_alive(), "a worker is stuck (deadlock?)"
    assert outcome == {"rebind": "InvalidAccountError"}
    assert issued["id"] == ids["wages"] == _bound_id(engine)  # the run is on the bound account
    with Session(engine) as db:
        assert db.exec(select(PayrollRun.id)).all()


def test_issue_after_a_committed_rebind_locks_the_new_role_account(race_db):
    engine, ids = race_db
    outcome: dict = {}
    _rebind(engine, ids, outcome)
    assert outcome == {"rebind": "ok"}
    assert _issue_run(engine, ids, locked=None, release=None) == ids["spare"]
    assert _bound_id(engine) == ids["spare"]
