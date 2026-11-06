from datetime import date

import pytest

from payroll.expenses.models import Vendor
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SystemRole
from tests.ledger.conftest import chart  # noqa: F401  (re-exported fixture)

JAN = date(2026, 1, 15)


@pytest.fixture
def vendor(session) -> Vendor:
    row = Vendor(name="Office Depot")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def expense_account(chart, owner_user, session) -> Account:  # noqa: F811
    return AccountService(session).create(
        actor=owner_user, name="Office Supplies Test", type=AccountType.EXPENSE
    )


@pytest.fixture
def card(chart, owner_user, session) -> Account:  # noqa: F811
    """A credit card: a liability account an expense can be paid from."""
    return AccountService(session).create(
        actor=owner_user, name="Business Visa", type=AccountType.LIABILITY
    )


@pytest.fixture
def owner_role(chart, session) -> Account:  # noqa: F811
    """The Due to Owner account (the system role is bound by the default chart)."""
    account = AccountService(session).account_by_role(SystemRole.DUE_TO_OWNER)
    assert account is not None
    return account
