import pytest
from sqlmodel import col, select

from payroll.ledger.accounts import PAYROLL_ROLES, ROLE_TYPES, AccountService
from payroll.ledger.models import Account, SystemRole
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
