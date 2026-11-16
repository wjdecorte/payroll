"""
Payroll settings: FUTA/SUTA rates and wage bases per tax year, and the singleton of
deposit due-date rules per agency (M4 design spec §4).

Every change is owner-only, audited and committed through ``commit_or_rollback``.
"""

import logging
from decimal import Decimal
from typing import Any

from sqlmodel import Session, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import commit_or_rollback, is_int
from payroll.payroll_run.exceptions import InvalidPayrollSettingsError, PayrollSettingsMissingError
from payroll.payroll_run.ledger_models import PayrollDepositRules, PayrollSettings
from payroll.tax.unemployment import UnemploymentRates

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.settings")

MIN_TAX_YEAR = 2000
MAX_TAX_YEAR = 2100
MAX_RATE = Decimal("0.2")
MAX_WAGE_BASE = Decimal("1000000")
_RATE_STEP = Decimal("0.000001")
_CENT = Decimal("0.01")

CADENCES = frozenset({"monthly", "quarterly"})
AGENCIES = ("irs_941", "ga_dor", "futa", "ga_dol")
DEPOSIT_FIELDS = frozenset(
    f"{agency}_{suffix}" for agency in AGENCIES for suffix in ("cadence", "due_day")
)


def _checked_rate(value: object, label: str) -> Decimal:
    """0 <= rate <= 0.2 with at most six decimal places; finite and in range before quantize."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidPayrollSettingsError(message=f"{label} must be a number")
    if value < 0 or value > MAX_RATE:
        raise InvalidPayrollSettingsError(message=f"{label} must be between 0 and 0.2")
    if value != value.quantize(_RATE_STEP):
        raise InvalidPayrollSettingsError(message=f"{label} must have at most six decimal places")
    return value.quantize(_RATE_STEP)


def _checked_wage_base(value: object, label: str) -> Decimal:
    """0 < base <= 1,000,000 with at most two decimal places."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidPayrollSettingsError(message=f"{label} must be a number")
    if value <= 0 or value > MAX_WAGE_BASE:
        raise InvalidPayrollSettingsError(message=f"{label} must be above 0 and at most 1,000,000")
    if value != value.quantize(_CENT):
        raise InvalidPayrollSettingsError(message=f"{label} must have at most two decimal places")
    return value.quantize(_CENT)


def _checked_deposit_field(name: str, value: object) -> str | int:
    label = name.replace("_", " ")
    if name.endswith("_cadence"):
        if not isinstance(value, str) or value not in CADENCES:
            raise InvalidPayrollSettingsError(message=f"The {label} must be monthly or quarterly")
        return value
    if not is_int(value) or not 0 <= value <= 31:  # type: ignore[operator]
        raise InvalidPayrollSettingsError(
            message=f"The {label} must be a whole number from 1 to 31, or 0 for the end of the month"
        )
    return value  # type: ignore[return-value]


class PayrollSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # --- unemployment rates per tax year ---

    def get_year(self, tax_year: int) -> PayrollSettings | None:
        return self.session.exec(
            select(PayrollSettings).where(PayrollSettings.tax_year == tax_year)
        ).first()

    def require_year(self, tax_year: int) -> PayrollSettings:
        row = self.get_year(tax_year)
        if row is None:
            raise PayrollSettingsMissingError(
                message=f"Payroll settings for {tax_year} are missing — "
                "enter the FUTA and SUTA rates in Settings > Payroll"
            )
        return row

    def rates(self, tax_year: int) -> UnemploymentRates:
        row = self.require_year(tax_year)
        return UnemploymentRates(
            futa_rate=row.futa_rate,
            futa_wage_base=row.futa_wage_base,
            suta_rate=row.suta_rate,
            suta_wage_base=row.suta_wage_base,
        )

    def set_year(
        self,
        *,
        actor: User,
        tax_year: int,
        futa_rate: Decimal,
        futa_wage_base: Decimal,
        suta_rate: Decimal,
        suta_wage_base: Decimal,
    ) -> PayrollSettings:
        ensure_role(actor, OWNER)
        if not is_int(tax_year) or not MIN_TAX_YEAR <= tax_year <= MAX_TAX_YEAR:
            raise InvalidPayrollSettingsError(
                message=f"The tax year must be between {MIN_TAX_YEAR} and {MAX_TAX_YEAR}"
            )
        values = {
            "futa_rate": _checked_rate(futa_rate, "The FUTA rate"),
            "futa_wage_base": _checked_wage_base(futa_wage_base, "The FUTA wage base"),
            "suta_rate": _checked_rate(suta_rate, "The SUTA rate"),
            "suta_wage_base": _checked_wage_base(suta_wage_base, "The SUTA wage base"),
        }

        def work() -> PayrollSettings:
            row = self.session.exec(
                select(PayrollSettings)
                .where(PayrollSettings.tax_year == tax_year)
                .with_for_update()
            ).first()
            before = None if row is None else self._year_snapshot(row)
            if row is None:
                row = PayrollSettings(tax_year=tax_year, **values)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            self.session.add(row)
            self.session.flush()
            record_audit(
                self.session,
                action="payroll.settings_year",
                user_id=actor.id,
                entity="payroll_settings",
                entity_id=str(tax_year),
                before=before,
                after=self._year_snapshot(row),
            )
            return row

        return commit_or_rollback(self.session, work)

    # --- deposit due-date rules (singleton) ---

    def deposit_rules(self) -> PayrollDepositRules:
        """The singleton row; created with the model defaults when absent."""
        row = self.session.get(PayrollDepositRules, 1)
        if row is None:
            row = PayrollDepositRules()
            self.session.add(row)
            self.session.flush()
        return row

    def set_deposit_rules(self, *, actor: User, **fields: Any) -> PayrollDepositRules:
        ensure_role(actor, OWNER)
        unknown = set(fields) - DEPOSIT_FIELDS
        if unknown:
            raise InvalidPayrollSettingsError(
                message=f"Unknown deposit rule field: {', '.join(sorted(unknown))}"
            )
        if not fields:
            raise InvalidPayrollSettingsError(message="No deposit rule changes were given")
        clean = {name: _checked_deposit_field(name, value) for name, value in fields.items()}

        def work() -> PayrollDepositRules:
            self.deposit_rules()
            row = self.session.exec(
                select(PayrollDepositRules).where(PayrollDepositRules.id == 1).with_for_update()
            ).one()
            before = self._rules_snapshot(row)
            for key, value in clean.items():
                setattr(row, key, value)
            self.session.add(row)
            self.session.flush()
            record_audit(
                self.session,
                action="payroll.deposit_rules",
                user_id=actor.id,
                entity="payroll_deposit_rules",
                entity_id="1",
                before=before,
                after=self._rules_snapshot(row),
            )
            return row

        return commit_or_rollback(self.session, work)

    @staticmethod
    def _year_snapshot(row: PayrollSettings) -> dict[str, Any]:
        return {
            "tax_year": row.tax_year,
            "futa_rate": str(row.futa_rate),
            "futa_wage_base": str(row.futa_wage_base),
            "suta_rate": str(row.suta_rate),
            "suta_wage_base": str(row.suta_wage_base),
        }

    @staticmethod
    def _rules_snapshot(row: PayrollDepositRules) -> dict[str, Any]:
        return {name: getattr(row, name) for name in sorted(DEPOSIT_FIELDS)}
