"""Items (products and services): each posts to one income account."""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import conflict_safe, utcnow_naive
from payroll.ledger.models import Account, AccountType
from payroll.receivables.exceptions import (
    InvalidItemError,
    ItemConflictError,
    ItemNotFoundError,
)
from payroll.receivables.models import Item

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.items")

MAX_NAME = 200
MAX_DESCRIPTION = 500
MAX_PRICE = Decimal("999999999999.99")
CENT = Decimal("0.01")
INCOME_TYPES = (AccountType.INCOME, AccountType.OTHER_INCOME)


def check_unit_price(value: Decimal) -> Decimal:
    """A finite Decimal from 0 to the maximum with at most two decimal places."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidItemError(message="Unit price must be an amount like 150.00")
    if value < 0 or value > MAX_PRICE:
        raise InvalidItemError(message="Unit price must be between 0.00 and 999,999,999,999.99")
    if value != value.quantize(CENT):
        raise InvalidItemError(message="Unit price can have at most 2 decimal places")
    return value.quantize(CENT)


class ItemService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, item_id: int) -> Item:
        item = self.session.get(Item, item_id)
        if item is None:
            raise ItemNotFoundError()
        return item

    def list(self, include_inactive: bool = False) -> list[Item]:
        stmt = select(Item).order_by(func.lower(Item.name), col(Item.id))
        if not include_inactive:
            stmt = stmt.where(col(Item.is_active).is_(True))
        return list(self.session.exec(stmt).all())

    # ------------------------------------------------------------------

    @conflict_safe(ItemConflictError)
    def create(
        self,
        *,
        actor: User,
        name: str,
        income_account_id: int,
        unit_price: Decimal = Decimal("0"),
        description: str | None = None,
    ) -> Item:
        ensure_role(actor, OWNER)
        fields = self._clean(None, name, income_account_id, unit_price, description)
        item = Item(**fields)
        self.session.add(item)
        self.session.flush()
        record_audit(
            self.session,
            action="item.create",
            user_id=actor.id,
            entity="item",
            entity_id=str(item.id),
            after=self._snapshot(item),
        )
        self.session.commit()
        logger.info("Created item id=%s", item.id)
        return item

    @conflict_safe(ItemConflictError)
    def update(
        self,
        item_id: int,
        *,
        actor: User,
        name: str,
        income_account_id: int,
        unit_price: Decimal,
        description: str | None,
    ) -> Item:
        ensure_role(actor, OWNER)
        item = self.get(item_id)
        before = self._snapshot(item)
        fields = self._clean(item, name, income_account_id, unit_price, description)
        for key, value in fields.items():
            setattr(item, key, value)
        item.modify_date = utcnow_naive()
        self.session.add(item)
        self.session.flush()
        record_audit(
            self.session,
            action="item.update",
            user_id=actor.id,
            entity="item",
            entity_id=str(item.id),
            before=before,
            after=self._snapshot(item),
        )
        self.session.commit()
        return item

    def set_active(self, item_id: int, active: bool, *, actor: User) -> Item:
        ensure_role(actor, OWNER)
        item = self.get(item_id)
        before = self._snapshot(item)
        item.is_active = active
        item.modify_date = utcnow_naive()
        self.session.add(item)
        self.session.flush()
        record_audit(
            self.session,
            action="item.activate",
            user_id=actor.id,
            entity="item",
            entity_id=str(item.id),
            before=before,
            after=self._snapshot(item),
        )
        self.session.commit()
        return item

    # ------------------------------------------------------------------

    def _clean(
        self,
        current: Item | None,
        name: str,
        income_account_id: int,
        unit_price: Decimal,
        description: str | None,
    ) -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > MAX_NAME:
            raise InvalidItemError(message=f"Name is required (at most {MAX_NAME} characters)")
        clean_description = (description or "").strip()
        if len(clean_description) > MAX_DESCRIPTION:
            raise InvalidItemError(
                message=f"Description must be at most {MAX_DESCRIPTION} characters"
            )
        price = check_unit_price(unit_price)
        account = (
            self.session.get(Account, income_account_id)
            if isinstance(income_account_id, int)
            else None
        )
        if account is None or not account.is_active or account.type not in INCOME_TYPES:
            raise InvalidItemError(message="Choose an active income account")
        stmt = select(Item.id).where(func.lower(Item.name) == clean_name.lower())
        if current is not None:
            stmt = stmt.where(Item.id != current.id)
        if self.session.exec(stmt).first() is not None:
            raise ItemConflictError()
        return {
            "name": clean_name,
            "description": clean_description or None,
            "unit_price": price,
            "income_account_id": account.id,
        }

    @staticmethod
    def _snapshot(item: Item) -> dict[str, Any]:
        return {
            "name": item.name,
            "description": item.description,
            "unit_price": str(item.unit_price),
            "income_account_id": item.income_account_id,
            "is_active": item.is_active,
        }
