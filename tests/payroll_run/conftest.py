from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import col, select

from payroll.ledger.accounts import PAYROLL_ROLES, ROLE_TYPES, AccountService
from payroll.ledger.models import Account, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.payroll_run.models import PayrollRun
from tests.ledger.conftest import chart  # noqa: F401  (re-exported fixture)


@pytest.fixture
def draft_run(session) -> PayrollRun:
    """A saved draft run, built directly (the service builds it in later milestones)."""
    run = PayrollRun(pay_period="2026-01", gross_salary="8000.00", fica_wages="8000.00")
    session.add(run)
    session.flush()
    session.refresh(run)
    return run


@pytest.fixture
def payroll_roles(session, owner_user, chart) -> dict[SystemRole, Account]:  # noqa: F811
    """The eight payroll roles bound to accounts made here (not the chart's own)."""
    service = AccountService(session)
    held = select(Account).where(col(Account.system_role).in_(PAYROLL_ROLES))
    for account in session.exec(held).all():
        service.bind_role(SystemRole(account.system_role), None, actor=owner_user)
    bound: dict[SystemRole, Account] = {}
    for role in PAYROLL_ROLES:
        account = service.create(actor=owner_user, name=f"Test {role.value}", type=ROLE_TYPES[role])
        service.bind_role(role, account.id, actor=owner_user)
        bound[role] = account
    return bound


@pytest.fixture
def entries(session, owner_user, chart):  # noqa: F811
    """A factory for journal entries (``entry_id`` / ``void_entry_id`` targets)."""
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id

    def make() -> int:
        draft = EntryDraft(
            date(2026, 1, 15),
            [LineDraft(cash, debit=Decimal("1")), LineDraft(sales, credit=Decimal("1"))],
            memo="x",
            source_type=SourceType.PAYROLL_RUN,
        )
        return LedgerPostingService(session).post(draft, actor=owner_user).id

    return make


@pytest.fixture
def bank(chart):  # noqa: F811
    return chart["Business Checking"]
