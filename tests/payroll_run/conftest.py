import pytest

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
