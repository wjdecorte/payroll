"""Vendors: create, edit and deactivate. Mutations are owner-only and audited."""

import logging
import unicodedata
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import commit_or_rollback, conflict_safe, utcnow_naive
from payroll.expenses.exceptions import (
    InvalidVendorError,
    VendorConflictError,
    VendorNotFoundError,
)
from payroll.expenses.models import Vendor

logger = logging.getLogger(f"{app_settings.logger_name}.expenses.vendors")

MAX_NAME = 200
MAX_NOTE = 500


def _has_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


class VendorService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, vendor_id: int) -> Vendor:
        vendor = self.session.get(Vendor, vendor_id)
        if vendor is None:
            raise VendorNotFoundError()
        return vendor

    def list(
        self,
        include_inactive: bool = False,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Vendor]:
        stmt = select(Vendor).order_by(func.lower(Vendor.name), col(Vendor.id))
        if not include_inactive:
            stmt = stmt.where(col(Vendor.active).is_(True))
        needle = (query or "").strip()
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(col(Vendor.name).ilike(f"%{escaped}%", escape="\\"))
        return list(self.session.exec(stmt.limit(limit).offset(offset)).all())

    # ------------------------------------------------------------------

    @conflict_safe(VendorConflictError)
    def create(self, *, actor: User, name: str, note: str | None = None) -> Vendor:
        ensure_role(actor, OWNER)

        def work() -> Vendor:
            fields = self._clean(None, name, note)
            vendor = Vendor(**fields)
            self.session.add(vendor)
            self.session.flush()
            record_audit(
                self.session,
                action="vendor.create",
                user_id=actor.id,
                entity="vendor",
                entity_id=str(vendor.id),
                after=self._snapshot(vendor),
            )
            return vendor

        vendor = commit_or_rollback(self.session, work)
        logger.info("Created vendor id=%s", vendor.id)
        return vendor

    @conflict_safe(VendorConflictError)
    def update(self, vendor_id: int, *, actor: User, name: str, note: str | None) -> Vendor:
        ensure_role(actor, OWNER)

        def work() -> Vendor:
            vendor = self.get(vendor_id)
            before = self._snapshot(vendor)
            for key, value in self._clean(vendor, name, note).items():
                setattr(vendor, key, value)
            vendor.modify_date = utcnow_naive()
            self.session.add(vendor)
            self.session.flush()
            record_audit(
                self.session,
                action="vendor.update",
                user_id=actor.id,
                entity="vendor",
                entity_id=str(vendor.id),
                before=before,
                after=self._snapshot(vendor),
            )
            return vendor

        return commit_or_rollback(self.session, work)

    def set_active(self, vendor_id: int, *, actor: User, active: bool) -> Vendor:
        ensure_role(actor, OWNER)

        def work() -> Vendor:
            vendor = self.get(vendor_id)
            before = self._snapshot(vendor)
            vendor.active = active
            vendor.modify_date = utcnow_naive()
            self.session.add(vendor)
            self.session.flush()
            record_audit(
                self.session,
                action="vendor.set_active",
                user_id=actor.id,
                entity="vendor",
                entity_id=str(vendor.id),
                before=before,
                after=self._snapshot(vendor),
            )
            return vendor

        return commit_or_rollback(self.session, work)

    # ------------------------------------------------------------------

    def _clean(self, current: Vendor | None, name: str, note: str | None) -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > MAX_NAME or _has_control_chars(clean_name):
            raise InvalidVendorError(
                message=f"Name is required (at most {MAX_NAME} characters, no control characters)"
            )
        clean_note = (note or "").strip()
        if len(clean_note) > MAX_NOTE or _has_control_chars(clean_note):
            raise InvalidVendorError(
                message=f"Note must be at most {MAX_NOTE} characters, with no control characters"
            )
        stmt = select(Vendor.id).where(func.lower(Vendor.name) == clean_name.lower())
        if current is not None:
            stmt = stmt.where(Vendor.id != current.id)
        if self.session.exec(stmt).first() is not None:
            raise VendorConflictError()
        return {"name": clean_name, "note": clean_note or None}

    @staticmethod
    def _snapshot(vendor: Vendor) -> dict[str, Any]:
        return {"name": vendor.name, "note": vendor.note, "active": vendor.active}
