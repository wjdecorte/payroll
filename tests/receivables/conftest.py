from datetime import date
from decimal import Decimal

import pytest

from payroll.ledger.models import Account
from payroll.receivables.models import Customer, Item
from tests.ledger.conftest import chart  # noqa: F401  (re-exported fixture)

JAN = date(2026, 1, 15)


@pytest.fixture
def customer(session) -> Customer:
    row = Customer(name="Acme Corp", email="ap@acme.example")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def item(session, chart) -> Item:  # noqa: F811
    row = Item(
        name="Consulting",
        unit_price=Decimal("150.00"),
        income_account_id=chart["Services Revenue"].id,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def bank(chart) -> Account:  # noqa: F811
    return chart["Business Checking"]
