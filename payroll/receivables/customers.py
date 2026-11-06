"""Customers: create, edit and deactivate. Mutations are owner-only and audited."""

import logging
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import conflict_safe, utcnow_naive
from payroll.receivables.exceptions import (
    CustomerConflictError,
    CustomerNotFoundError,
    InvalidCustomerError,
)
from payroll.receivables.models import Customer

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.customers")

MAX_NAME = 200
MAX_EMAIL = 200
MAX_PHONE = 50
MAX_ADDRESS = 500
MAX_TERMS_DAYS = 365


def _optional(value: str | None, label: str, limit: int) -> str | None:
    text = (value or "").strip()
    if len(text) > limit:
        raise InvalidCustomerError(message=f"{label} must be at most {limit} characters")
    return text or None


class CustomerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, customer_id: int) -> Customer:
        customer = self.session.get(Customer, customer_id)
        if customer is None:
            raise CustomerNotFoundError()
        return customer

    def list(self, include_inactive: bool = False) -> list[Customer]:
        stmt = select(Customer).order_by(func.lower(Customer.name), col(Customer.id))
        if not include_inactive:
            stmt = stmt.where(col(Customer.is_active).is_(True))
        return list(self.session.exec(stmt).all())

    # ------------------------------------------------------------------

    @conflict_safe(CustomerConflictError)
    def create(
        self,
        *,
        actor: User,
        name: str,
        email: str | None = None,
        phone: str | None = None,
        billing_address: str | None = None,
        terms_days: int = 30,
    ) -> Customer:
        ensure_role(actor, OWNER)
        fields = self._clean(None, name, email, phone, billing_address, terms_days)
        customer = Customer(**fields)
        self.session.add(customer)
        self.session.flush()
        record_audit(
            self.session,
            action="customer.create",
            user_id=actor.id,
            entity="customer",
            entity_id=str(customer.id),
            after=self._snapshot(customer),
        )
        self.session.commit()
        logger.info("Created customer id=%s", customer.id)
        return customer

    @conflict_safe(CustomerConflictError)
    def update(
        self,
        customer_id: int,
        *,
        actor: User,
        name: str,
        email: str | None,
        phone: str | None,
        billing_address: str | None,
        terms_days: int,
    ) -> Customer:
        ensure_role(actor, OWNER)
        customer = self.get(customer_id)
        before = self._snapshot(customer)
        fields = self._clean(customer, name, email, phone, billing_address, terms_days)
        for key, value in fields.items():
            setattr(customer, key, value)
        customer.modify_date = utcnow_naive()
        self.session.add(customer)
        self.session.flush()
        record_audit(
            self.session,
            action="customer.update",
            user_id=actor.id,
            entity="customer",
            entity_id=str(customer.id),
            before=before,
            after=self._snapshot(customer),
        )
        self.session.commit()
        return customer

    def set_active(self, customer_id: int, active: bool, *, actor: User) -> Customer:
        ensure_role(actor, OWNER)
        customer = self.get(customer_id)
        before = self._snapshot(customer)
        customer.is_active = active
        customer.modify_date = utcnow_naive()
        self.session.add(customer)
        self.session.flush()
        record_audit(
            self.session,
            action="customer.activate",
            user_id=actor.id,
            entity="customer",
            entity_id=str(customer.id),
            before=before,
            after=self._snapshot(customer),
        )
        self.session.commit()
        return customer

    # ------------------------------------------------------------------

    def _clean(
        self,
        current: Customer | None,
        name: str,
        email: str | None,
        phone: str | None,
        billing_address: str | None,
        terms_days: int,
    ) -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > MAX_NAME:
            raise InvalidCustomerError(message=f"Name is required (at most {MAX_NAME} characters)")
        clean_email = _optional(email, "Email", MAX_EMAIL)
        if clean_email is not None and (
            clean_email.count("@") != 1 or any(ch.isspace() for ch in clean_email)
        ):
            raise InvalidCustomerError(message="Enter a valid email address")
        if clean_email is not None and (clean_email.startswith("@") or clean_email.endswith("@")):
            raise InvalidCustomerError(message="Enter a valid email address")
        if (
            not isinstance(terms_days, int)
            or isinstance(terms_days, bool)
            or not 0 <= terms_days <= MAX_TERMS_DAYS
        ):
            raise InvalidCustomerError(
                message=f"Terms must be a whole number of days from 0 to {MAX_TERMS_DAYS}"
            )
        stmt = select(Customer.id).where(func.lower(Customer.name) == clean_name.lower())
        if current is not None:
            stmt = stmt.where(Customer.id != current.id)
        if self.session.exec(stmt).first() is not None:
            raise CustomerConflictError()
        return {
            "name": clean_name,
            "email": clean_email,
            "phone": _optional(phone, "Phone", MAX_PHONE),
            "billing_address": _optional(billing_address, "Billing address", MAX_ADDRESS),
            "terms_days": terms_days,
        }

    @staticmethod
    def _snapshot(customer: Customer) -> dict[str, Any]:
        return {
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "billing_address": customer.billing_address,
            "terms_days": customer.terms_days,
            "is_active": customer.is_active,
        }
