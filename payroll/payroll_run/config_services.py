from datetime import datetime

from sqlmodel import Session, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role

from .config_models import PayrollConfig
from .config_schemas import PayrollConfigRead, PayrollConfigUpdate


class PayrollConfigService:
    """Read and update the single runtime payroll configuration row."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_config(self) -> PayrollConfigRead:
        config = self._get_or_create()
        return PayrollConfigRead.model_validate(config)

    def update_config(self, payload: PayrollConfigUpdate, *, actor: User) -> PayrollConfigRead:
        """Replace the configuration (``actor`` must be an owner); audits the changed fields."""
        ensure_role(actor, OWNER)
        config = self._get_or_create()
        before: dict[str, object] = {}
        after: dict[str, object] = {}
        for field, value in payload.model_dump().items():
            old = getattr(config, field)
            if old != value:
                before[field], after[field] = old, value
            setattr(config, field, value)
        config.modify_date = datetime.utcnow()
        self.session.add(config)
        # Same transaction as the change (committed together below).
        record_audit(
            self.session,
            action="payroll_config.update",
            user_id=actor.id,
            entity="payroll_config",
            entity_id=str(config.id),
            before=before,
            after=after,
        )
        self.session.commit()
        self.session.refresh(config)
        return PayrollConfigRead.model_validate(config)

    def get_accounts(self) -> dict[str, str]:
        config = self._get_or_create()
        return {
            "officer_compensation": config.acct_officer_compensation,
            "payroll_tax_expense": config.acct_payroll_tax_expense,
            "health_insurance_exp": config.acct_health_insurance_exp,
            "hsa_expense": config.acct_hsa_expense,
            "fed_tax_payable": config.acct_fed_tax_payable,
            "ga_tax_payable": config.acct_ga_tax_payable,
            "ss_payable_ee": config.acct_ss_payable_ee,
            "ss_payable_er": config.acct_ss_payable_er,
            "medicare_payable_ee": config.acct_medicare_payable_ee,
            "medicare_payable_er": config.acct_medicare_payable_er,
            "health_ins_payable": config.acct_health_ins_payable,
            "hsa_payable": config.acct_hsa_payable,
            "checking": config.acct_checking,
        }

    def get_due_date_notes(self) -> tuple[str, str]:
        config = self._get_or_create()
        return config.federal_due_date_note, config.georgia_due_date_note

    def increment_journal_no(self, count: int, *, actor: User) -> PayrollConfigRead:
        """Advance next_journal_no by count and persist (``actor`` must be an owner)."""
        ensure_role(actor, OWNER)
        config = self._get_or_create()
        old = config.next_journal_no
        config.next_journal_no = max(1, old + count)
        self.session.add(config)
        record_audit(
            self.session,
            action="payroll_config.increment_journal_no",
            user_id=actor.id,
            entity="payroll_config",
            entity_id=str(config.id),
            before={"next_journal_no": old},
            after={"next_journal_no": config.next_journal_no},
        )
        self.session.commit()
        self.session.refresh(config)
        return PayrollConfigRead.model_validate(config)

    def _get_or_create(self) -> PayrollConfig:
        config = self.session.exec(select(PayrollConfig).limit(1)).first()
        if config:
            return config

        config = PayrollConfig(**self._default_values())
        self.session.add(config)
        self.session.commit()
        self.session.refresh(config)
        return config

    def _default_values(self) -> dict[str, object]:
        return {
            "acct_officer_compensation": app_settings.acct_officer_compensation,
            "acct_payroll_tax_expense": app_settings.acct_payroll_tax_expense,
            "acct_health_insurance_exp": app_settings.acct_health_insurance_exp,
            "acct_hsa_expense": app_settings.acct_hsa_expense,
            "acct_fed_tax_payable": app_settings.acct_fed_tax_payable,
            "acct_ga_tax_payable": app_settings.acct_ga_tax_payable,
            "acct_ss_payable_ee": app_settings.acct_ss_payable_ee,
            "acct_ss_payable_er": app_settings.acct_ss_payable_er,
            "acct_medicare_payable_ee": app_settings.acct_medicare_payable_ee,
            "acct_medicare_payable_er": app_settings.acct_medicare_payable_er,
            "acct_health_ins_payable": app_settings.acct_health_ins_payable,
            "acct_hsa_payable": app_settings.acct_hsa_payable,
            "acct_checking": app_settings.acct_checking,
            "next_journal_no": 202,
        }
