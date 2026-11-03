import pytest

from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import Account


@pytest.fixture
def chart(session, owner_user) -> dict[str, Account]:
    """The seeded default chart, keyed by full name (``Parent:Child``)."""
    seed_default_chart(session, actor=owner_user)
    return {node.full_name: node.account for node in AccountService(session).tree()}
