"""Receivables settings: the next invoice number and the company block on invoices."""

import logging
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.receivables.exceptions import InvalidInvoiceError, InvoiceNumberError
from payroll.receivables.models import Invoice, ReceivablesSettings

logger = logging.getLogger(f"{app_settings.logger_name}.receivables.settings")

MAX_INVOICE_NUMBER = 99_999_999
MAX_TEXT = 500


class ReceivablesSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> ReceivablesSettings:
        row = self.session.get(ReceivablesSettings, 1)
        if row is None:
            row = ReceivablesSettings()
            self.session.add(row)
            self.session.flush()
        return row

    def _lock(self) -> ReceivablesSettings:
        self.get()
        return self.session.exec(
            select(ReceivablesSettings).where(ReceivablesSettings.id == 1).with_for_update()
        ).one()

    def set_next_invoice_number(self, n: int, *, actor: User) -> ReceivablesSettings:
        ensure_role(actor, OWNER)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_INVOICE_NUMBER:
            raise InvoiceNumberError(
                message=f"The next invoice number must be a whole number from 1 to {MAX_INVOICE_NUMBER:,}"
            )
        try:
            row = self._lock()
            highest = self.session.exec(select(func.max(Invoice.number))).one()
            if highest is not None and n <= highest:
                raise InvoiceNumberError(
                    message=f"The next invoice number must be higher than {highest}, "
                    "the highest number already used"
                )
            before = {"next_invoice_no": row.next_invoice_no}
            row.next_invoice_no = n
            self.session.add(row)
            self.session.flush()
            record_audit(
                self.session,
                action="receivables.next_number",
                user_id=actor.id,
                entity="receivables_settings",
                entity_id="1",
                before=before,
                after={"next_invoice_no": n},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return row

    def update_company(
        self,
        *,
        actor: User,
        company_name: str | None,
        company_address: str | None,
        remit_to: str | None,
        invoice_footer: str | None,
    ) -> ReceivablesSettings:
        ensure_role(actor, OWNER)
        values = {
            "company_name": company_name,
            "company_address": company_address,
            "remit_to": remit_to,
            "invoice_footer": invoice_footer,
        }
        clean: dict[str, str | None] = {}
        for key, raw in values.items():
            text = (raw or "").strip()
            if len(text) > MAX_TEXT:
                raise InvalidInvoiceError(
                    message=f"{key.replace('_', ' ').capitalize()} must be at most {MAX_TEXT} characters"
                )
            clean[key] = text or None
        try:
            row = self._lock()
            before = self._snapshot(row)
            for key, value in clean.items():
                setattr(row, key, value)
            self.session.add(row)
            self.session.flush()
            record_audit(
                self.session,
                action="receivables.company",
                user_id=actor.id,
                entity="receivables_settings",
                entity_id="1",
                before=before,
                after=self._snapshot(row),
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return row

    @staticmethod
    def _snapshot(row: ReceivablesSettings) -> dict[str, Any]:
        return {
            "company_name": row.company_name,
            "company_address": row.company_address,
            "remit_to": row.remit_to,
            "invoice_footer": row.invoice_footer,
        }
