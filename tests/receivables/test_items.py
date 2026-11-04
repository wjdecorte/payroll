from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType
from payroll.receivables.exceptions import InvalidItemError, ItemConflictError
from payroll.receivables.items import ItemService


def test_item_needs_active_income_type_account(session, owner_user, chart):
    accounts = AccountService(session)
    service = ItemService(session)
    other_income = accounts.create(actor=owner_user, name="Interest", type=AccountType.OTHER_INCOME)
    service.create(actor=owner_user, name="A", income_account_id=other_income.id)
    service.create(actor=owner_user, name="B", income_account_id=chart["Services Revenue"].id)
    expense = next(a for a in chart.values() if a.type == AccountType.EXPENSE)
    for bad in (chart["Business Checking"], expense):
        with pytest.raises(InvalidItemError):
            service.create(actor=owner_user, name="Bad", income_account_id=bad.id)
    spare = accounts.create(actor=owner_user, name="Old Income", type=AccountType.INCOME)
    accounts.set_active(spare.id, False, actor=owner_user)
    with pytest.raises(InvalidItemError):
        service.create(actor=owner_user, name="Bad", income_account_id=spare.id)
    with pytest.raises(InvalidItemError):
        service.create(actor=owner_user, name="Bad", income_account_id=999_999)


@pytest.mark.parametrize(
    "price",
    [
        Decimal("1.001"),
        Decimal("-0.01"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1000000000000.00"),
        Decimal("1E+20"),
    ],
)
def test_unit_price_rejects_three_decimals_negative_nan_huge(
    session,
    owner_user,
    chart,
    price,
):
    with pytest.raises(InvalidItemError):
        ItemService(session).create(
            actor=owner_user,
            name="X",
            income_account_id=chart["Services Revenue"].id,
            unit_price=price,
        )


def test_unit_price_bounds_and_default(session, owner_user, chart):
    service = ItemService(session)
    acct = chart["Services Revenue"].id
    assert service.create(actor=owner_user, name="Free", income_account_id=acct).unit_price == 0
    top = service.create(
        actor=owner_user,
        name="Top",
        income_account_id=acct,
        unit_price=Decimal("999999999999.99"),
        description="d" * 500,
    )
    assert top.unit_price == Decimal("999999999999.99")
    with pytest.raises(InvalidItemError):
        service.create(actor=owner_user, name="Long", income_account_id=acct, description="d" * 501)
    with pytest.raises(InvalidItemError):
        service.create(actor=owner_user, name=" ", income_account_id=acct)


def test_item_name_unique(session, owner_user, chart):
    service = ItemService(session)
    acct = chart["Services Revenue"].id
    first = service.create(actor=owner_user, name="Consulting", income_account_id=acct)
    with pytest.raises(ItemConflictError):
        service.create(actor=owner_user, name="consulting", income_account_id=acct)
    second = service.create(actor=owner_user, name="Support", income_account_id=acct)
    with pytest.raises(ItemConflictError):
        service.update(
            second.id,
            actor=owner_user,
            name="CONSULTING",
            income_account_id=acct,
            unit_price=Decimal("1"),
            description=None,
        )
    service.update(
        first.id,
        actor=owner_user,
        name="CONSULTING",
        income_account_id=acct,
        unit_price=Decimal("2.50"),
        description="x",
    )
    assert service.get(first.id).unit_price == Decimal("2.50")


def test_owner_only_active_toggle_and_audit(session, owner_user, viewer_user, chart):
    service = ItemService(session)
    acct = chart["Services Revenue"].id
    with pytest.raises(PermissionDeniedError):
        service.create(actor=viewer_user, name="X", income_account_id=acct)
    item = service.create(actor=owner_user, name="X", income_account_id=acct)
    with pytest.raises(PermissionDeniedError):
        service.set_active(item.id, False, actor=viewer_user)
    service.set_active(item.id, False, actor=owner_user)
    assert service.list() == []
    assert len(service.list(include_inactive=True)) == 1
    actions = [
        r.action
        for r in session.exec(
            select(AuditLog).where(AuditLog.entity == "item").order_by(AuditLog.id)
        )
    ]
    assert actions == ["item.create", "item.activate"]
