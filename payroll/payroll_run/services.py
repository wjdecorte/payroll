"""
PayrollRunService
=================
Orchestrates payroll calculation, journal-entry generation, and database persistence.

Flow for a calculation request:
  1. Validate and parse inputs (PayrollInput)
  2. Call the pure tax engine (calc_payroll)
  3. Build QuickBooks journal entries
  4. Optionally persist a PayrollRun record
  5. Return a PayrollResult

All money is exact ``Decimal``. A saved run is always rebuilt from its STORED
amounts, never recalculated: the stored figures are what was booked in
QuickBooks, and a recalculation can differ (e.g. rounding-rule changes).
"""

import logging
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import commit_or_rollback
from payroll.tax import constants as tax
from payroll.tax.engine import calc_payroll
from payroll.tax.schemas import PayrollCalculation, TaxConstantsSchema
from payroll.tax.unemployment import UnemploymentResult, calc_unemployment

from .config_services import PayrollConfigService
from .exceptions import (
    InvalidPayPeriodError,
    InvalidPayrollRunError,
    PayrollRunAlreadyExistsError,
    PayrollRunNotFoundError,
    PayrollRunStateError,
)
from .models import PayrollRun
from .schemas import (
    JournalEntry,
    JournalLine,
    PayrollInput,
    PayrollResult,
    PayrollRunSummary,
    TaxDetail,
    TaxPaymentSummary,
)
from .settings_service import PayrollSettingsService

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.service")

_PAY_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# Runs whose year-to-date wages feed the next run (a void run no longer counts).
_CHAIN_STATUSES = ("approved", "imported", "draft")


class PayrollRunService:
    """All business logic for payroll calculation and history management."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.config_service = PayrollConfigService(session)
        self.settings_service = PayrollSettingsService(session)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(self, payload: PayrollInput, *, actor: User) -> PayrollResult:
        """
        Calculate a payroll run and optionally save it. Saving needs an ``owner``
        ``actor`` (recorded as the audit row's ``user_id``); a calculation that is
        not saved changes nothing and is open to any role.

        Raises:
            PermissionDeniedError: If save_run=True and ``actor`` is not an owner.
            InvalidPayPeriodError: If pay_period is not in YYYY-MM format.
            PayrollRunAlreadyExistsError: If a non-void run for this period already
                                          exists and save_run=True.
            PayrollSettingsMissingError: If save_run=True and the tax year has no
                                         payroll (FUTA / SUTA) settings.
        """
        if payload.save_run:
            ensure_role(actor, OWNER)

        self._validate_pay_period(payload.pay_period)

        if payload.save_run:
            if payload.pay_period[:4] != str(tax.TAX_YEAR):
                raise InvalidPayrollRunError(
                    message=f"Pay period {payload.pay_period} is not in {tax.TAX_YEAR}; "
                    f"this app calculates and saves tax year {tax.TAX_YEAR} payroll runs only"
                )
            existing = self._find_by_period(payload.pay_period)
            if existing:
                raise PayrollRunAlreadyExistsError(
                    message=f"Payroll run for '{payload.pay_period}' already exists (id={existing.id}). "
                    "Delete it first or use a different pay period."
                )
            later = self.session.exec(
                select(PayrollRun)
                .where(
                    PayrollRun.tax_year == tax.TAX_YEAR,
                    col(PayrollRun.pay_period).like(f"{tax.TAX_YEAR}-%"),
                    PayrollRun.pay_period > payload.pay_period,
                    PayrollRun.status != "void",
                )
                .order_by(col(PayrollRun.pay_period))
            ).first()
            if later:
                raise PayrollRunStateError(
                    message=f"The {later.pay_period} payroll run ({later.status}) is later than "
                    f"'{payload.pay_period}', so its year-to-date wages would no longer match. "
                    "Delete or void the later runs first."
                )

        # Resolve YTD FICA wages from the most recent saved run if requested
        if payload.use_previous_ytd_fica:
            payload.ytd_fica_wages = self._get_previous_ytd_fica(payload.pay_period)
            logger.info("Resolved YTD FICA from previous run: %s", payload.ytd_fica_wages)

        # Saving needs the year's FUTA / SUTA settings; a calculation that is not saved
        # reports them only when they exist.
        settings = (
            self.settings_service.require_year(tax.TAX_YEAR)
            if payload.save_run
            else self.settings_service.get_year(tax.TAX_YEAR)
        )

        calc = calc_payroll(
            gross_salary=payload.gross_salary,
            health_insurance=payload.health_insurance,
            hsa_contribution=payload.hsa_contribution,
            health_in_income_tax=payload.health_in_income_tax,
            hsa_in_income_tax=payload.hsa_in_income_tax,
            ytd_fica_wages=payload.ytd_fica_wages,
        )

        logger.info(
            "Calculated payroll for period=%s gross=%s net=%s",
            payload.pay_period,
            payload.gross_salary,
            calc.net_pay,
        )

        # Unemployment wages are the run's FICA wages, so they follow the same year-to-date chain.
        unemployment = None
        if settings is not None:
            unemployment = calc_unemployment(
                payload.ytd_fica_wages,
                calc.ytd_fica_after,
                self.settings_service.rates(tax.TAX_YEAR),
            )

        saved_run: PayrollRun | None = None
        if payload.save_run:
            assert unemployment is not None and settings is not None  # require_year above
            saved_run = self._persist(payload, calc, unemployment, settings.suta_rate, actor)
            logger.info("Saved payroll run id=%d", saved_run.id)

        tax_detail = self._tax_detail_from_calc(calc, payload.ytd_fica_wages)
        return self._build_result(
            payload,
            tax_detail,
            saved_run,
            unemployment,
            settings.suta_rate if settings else None,
        )

    def get_run(self, run_id: int) -> PayrollResult:
        """Retrieve a saved payroll run by ID and rebuild its full result.

        Every amount comes from the stored columns — the run is never
        recalculated. Journal entries are rebuilt from those stored amounts so
        they reflect any account-name changes made since the run was saved.
        """
        run = self._get_or_raise(run_id)
        stored = UnemploymentResult(
            futa_wages=run.futa_wages, futa=run.futa, suta_wages=run.suta_wages, suta=run.suta
        )
        return self._build_result(run, self._tax_detail_from_run(run), run, stored, run.suta_rate)

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[PayrollRunSummary]:
        """Return a paginated list of saved payroll runs, newest first."""
        statement = (
            select(PayrollRun)
            .order_by(PayrollRun.pay_period.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        )
        runs = self.session.exec(statement).all()
        return [PayrollRunSummary.model_validate(r) for r in runs]

    def delete_run(self, run_id: int, *, actor: User) -> None:
        """
        Delete a draft payroll run by ID. ``actor`` must be an owner (checked first).

        Approved, void and imported runs are history and are kept, and a draft is kept while
        a later non-void run of the tax year exists, because that run's year-to-date wages
        follow from it (``PayrollRunStateError``).
        """
        ensure_role(actor, OWNER)

        def work() -> None:
            # Row locks, in id order like approve and void (the tax year's runs): an approval
            # cannot change the status, and no run can be saved or approved, between the
            # checks and the delete.
            rows = self.session.exec(
                select(PayrollRun)
                .where(
                    PayrollRun.tax_year
                    == select(PayrollRun.tax_year).where(PayrollRun.id == run_id).scalar_subquery()
                )
                .order_by(col(PayrollRun.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            run = next((r for r in rows if r.id == run_id), None)
            if run is None:
                raise PayrollRunNotFoundError(message=f"Payroll run with id={run_id} not found")
            if run.status != "draft":
                raise PayrollRunStateError(
                    message=f"Payroll run {run_id} is {run.status} — only a draft can be deleted"
                )
            later = sorted(
                (
                    r
                    for r in rows
                    if r.status != "void"
                    and r.pay_period > run.pay_period
                    and r.pay_period.startswith(f"{run.tax_year}-")
                ),
                key=lambda r: r.pay_period,
            )
            if later:
                raise PayrollRunStateError(
                    message=f"The {later[0].pay_period} payroll run ({later[0].status}) is later "
                    f"than {run.pay_period}, and its year-to-date wages follow from this one. "
                    "Delete or void the later runs first."
                )
            # Audited in the same transaction as the delete.
            record_audit(
                self.session,
                action="payroll_run.delete",
                user_id=actor.id,
                entity="payroll_run",
                entity_id=str(run.id),
                before=self._audit_snapshot(run),
            )
            self.session.delete(run)

        commit_or_rollback(self.session, work)
        logger.info("Deleted payroll run id=%d", run_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate_pay_period(self, pay_period: str) -> None:
        if not _PAY_PERIOD_RE.match(pay_period):
            raise InvalidPayPeriodError(
                message=f"Invalid pay period '{pay_period}' — expected YYYY-MM (e.g. '2026-01')"
            )

    def _find_by_period(self, pay_period: str) -> PayrollRun | None:
        """The non-void run for ``pay_period`` (a void run frees its period)."""
        return self.session.exec(
            select(PayrollRun).where(
                PayrollRun.pay_period == pay_period, PayrollRun.status != "void"
            )
        ).first()

    def _get_previous_ytd_fica(self, pay_period: str) -> Decimal:
        """Return ytd_fica_after from the most recent saved run before this period.

        Only considers runs in the same calendar year (YTD resets each January) that
        are approved, imported or draft; a void run does not count.
        Returns 0.00 when no prior run exists.
        """
        year = pay_period[:4]
        run = self.session.exec(
            select(PayrollRun)
            .where(
                PayrollRun.pay_period < pay_period,
                PayrollRun.pay_period >= f"{year}-01",
                col(PayrollRun.status).in_(_CHAIN_STATUSES),
            )
            .order_by(PayrollRun.pay_period.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()
        return run.ytd_fica_after if run else Decimal("0.00")

    def _get_or_raise(self, run_id: int) -> PayrollRun:
        run = self.session.get(PayrollRun, run_id)
        if not run:
            raise PayrollRunNotFoundError(message=f"Payroll run with id={run_id} not found")
        return run

    def _persist(
        self,
        payload: PayrollInput,
        calc: PayrollCalculation,
        unemployment: UnemploymentResult,
        suta_rate: Decimal,
        actor: User,
    ) -> PayrollRun:
        run = PayrollRun(
            status="draft",
            pay_period=payload.pay_period,
            gross_salary=payload.gross_salary,
            health_insurance=payload.health_insurance,
            hsa_contribution=payload.hsa_contribution,
            health_in_income_tax=payload.health_in_income_tax,
            hsa_in_income_tax=payload.hsa_in_income_tax,
            ytd_fica_before=payload.ytd_fica_wages,
            notes=payload.notes,
            tax_year=tax.TAX_YEAR,
            # Calculated
            fica_wages=calc.fica_wages,
            income_tax_wages=calc.income_tax_wages,
            ss_taxable_wages=calc.ss_taxable_wages,
            ss_ee=calc.ss_ee,
            ss_er=calc.ss_er,
            medicare_ee=calc.medicare_ee,
            additional_medicare=calc.additional_medicare,
            total_medicare_ee=calc.total_medicare_ee,
            medicare_er=calc.medicare_er,
            federal_withholding=calc.federal_withholding,
            ga_withholding=calc.ga_withholding,
            total_employee_deductions=calc.total_employee_deductions,
            net_pay=calc.net_pay,
            total_employer_taxes=calc.total_employer_taxes,
            total_employer_cost=calc.total_employer_cost,
            ytd_fica_after=calc.ytd_fica_after,
            # FUTA / SUTA, with the SUTA rate used
            futa_wages=unemployment.futa_wages,
            futa=unemployment.futa,
            suta_wages=unemployment.suta_wages,
            suta=unemployment.suta,
            suta_rate=suta_rate,
        )

        def work() -> None:
            self.session.add(run)
            try:
                self.session.flush()  # assign run.id for the audit row
            except IntegrityError as exc:
                # Another save for this period won the race past the existence check.
                if "uq_payroll_run_period_active" not in str(exc.orig):
                    raise
                raise PayrollRunAlreadyExistsError(
                    message=f"Payroll run for '{payload.pay_period}' already exists. "
                    "Delete it first or use a different pay period."
                ) from exc
            # Audited in the same transaction as the save.
            record_audit(
                self.session,
                action="payroll_run.save",
                user_id=actor.id,
                entity="payroll_run",
                entity_id=str(run.id),
                after=self._audit_snapshot(run),
            )

        commit_or_rollback(self.session, work)
        self.session.refresh(run)
        return run

    @staticmethod
    def _audit_snapshot(run: PayrollRun) -> dict[str, object]:
        """The key fields of a run recorded in the audit log."""
        return {
            "pay_period": run.pay_period,
            "status": run.status,
            "gross_salary": run.gross_salary,
            "net_pay": run.net_pay,
            "futa": run.futa,
            "suta": run.suta,
        }

    @staticmethod
    def _tax_detail_from_calc(calc: PayrollCalculation, ytd_fica_before: Decimal) -> TaxDetail:
        return TaxDetail(
            fica_wages=calc.fica_wages,
            income_tax_wages=calc.income_tax_wages,
            annual_income_tax_wages=calc.annual_income_tax_wages,
            ss_taxable_wages=calc.ss_taxable_wages,
            ss_ee=calc.ss_ee,
            ss_er=calc.ss_er,
            ss_cap_hit=calc.ss_cap_hit,
            medicare_ee=calc.medicare_ee,
            additional_medicare=calc.additional_medicare,
            total_medicare_ee=calc.total_medicare_ee,
            medicare_er=calc.medicare_er,
            annual_fed_tax=calc.annual_fed_tax,
            federal_withholding=calc.federal_withholding,
            ga_annual_taxable=calc.ga_annual_taxable,
            annual_ga_tax=calc.annual_ga_tax,
            ga_withholding=calc.ga_withholding,
            total_employee_deductions=calc.total_employee_deductions,
            net_pay=calc.net_pay,
            total_employer_taxes=calc.total_employer_taxes,
            total_employer_cost=calc.total_employer_cost,
            ytd_fica_before=ytd_fica_before,
            ytd_fica_after=calc.ytd_fica_after,
        )

    @staticmethod
    def _tax_detail_from_run(run: PayrollRun) -> TaxDetail:
        """Build the tax detail from a saved run's stored amounts (no recalculation).

        ``annual_income_tax_wages`` and ``ss_cap_hit`` are derived directly from
        stored columns; the annual tax projections are not stored, so they are None.
        """
        return TaxDetail(
            fica_wages=run.fica_wages,
            income_tax_wages=run.income_tax_wages,
            annual_income_tax_wages=run.income_tax_wages * tax.PAY_PERIODS_PER_YEAR,
            ss_taxable_wages=run.ss_taxable_wages,
            ss_ee=run.ss_ee,
            ss_er=run.ss_er,
            ss_cap_hit=(run.ytd_fica_before + run.fica_wages) >= tax.SS_WAGE_BASE,
            medicare_ee=run.medicare_ee,
            additional_medicare=run.additional_medicare,
            total_medicare_ee=run.total_medicare_ee,
            medicare_er=run.medicare_er,
            federal_withholding=run.federal_withholding,
            ga_withholding=run.ga_withholding,
            total_employee_deductions=run.total_employee_deductions,
            net_pay=run.net_pay,
            total_employer_taxes=run.total_employer_taxes,
            total_employer_cost=run.total_employer_cost,
            ytd_fica_before=run.ytd_fica_before,
            ytd_fica_after=run.ytd_fica_after,
        )

    def _build_result(
        self,
        source: PayrollInput | PayrollRun,
        td: TaxDetail,
        run: PayrollRun | None,
        unemployment: UnemploymentResult | None,
        suta_rate: Decimal | None,
    ) -> PayrollResult:
        """Assemble the API result from the inputs (payload or saved run) and amounts."""
        acct = self.config_service.get_accounts()
        gross = source.gross_salary
        health = source.health_insurance
        hsa = source.hsa_contribution
        health_in_it = source.health_in_income_tax

        journal_entries = self._build_journal_entries(acct, td, gross, health, hsa, health_in_it)
        tax_payment = self._build_tax_payment_summary(td)

        return PayrollResult(
            id=run.id if run else None,
            pay_period=source.pay_period,
            gross_salary=gross,
            health_insurance=health,
            hsa_contribution=hsa,
            health_in_income_tax=health_in_it,
            hsa_in_income_tax=source.hsa_in_income_tax,
            notes=source.notes,
            saved=run is not None,
            tax_year=run.tax_year if run else tax.TAX_YEAR,
            tax_constants=TaxConstantsSchema(),
            tax_detail=td,
            journal_entries=journal_entries,
            tax_payment_summary=tax_payment,
            create_date=run.create_date if run else datetime.utcnow(),
            status=run.status if run else None,
            pay_date=run.pay_date if run else None,
            entry_id=run.entry_id if run else None,
            futa_wages=unemployment.futa_wages if unemployment else None,
            futa=unemployment.futa if unemployment else None,
            suta_wages=unemployment.suta_wages if unemployment else None,
            suta=unemployment.suta if unemployment else None,
            suta_rate=suta_rate,
        )

    # ------------------------------------------------------------------
    # Journal entry builder
    # ------------------------------------------------------------------

    def _build_journal_entries(
        self,
        acct: dict[str, str],
        calc: TaxDetail,
        gross: Decimal,
        health: Decimal,
        hsa: Decimal,
        health_in_it: bool,
    ) -> list[JournalEntry]:
        entries: list[JournalEntry] = []

        # ---- Entry 1: Gross payroll wages and withholdings ----
        # DR Officer Compensation = gross + health W-2 addback (if in income tax) + HSA.
        # HSA is always included here because it flows through payroll as officer comp.
        dr_compensation = gross
        if health_in_it and health > 0:
            dr_compensation += health
        if hsa > 0:
            dr_compensation += hsa

        comp_memo = f"Gross salary ${gross:,.2f}"
        if health_in_it and health > 0:
            comp_memo += f" + medical insurance W-2 addback ${health:,.2f}"
        if hsa > 0:
            comp_memo += f" + HSA contribution ${hsa:,.2f}"

        entry1_lines: list[JournalLine] = [
            JournalLine(
                account=acct["officer_compensation"],
                memo=comp_memo,
                debit=dr_compensation,
            ),
            JournalLine(
                account=acct["fed_tax_payable"],
                memo="Federal income tax withheld",
                credit=calc.federal_withholding,
            ),
            JournalLine(
                account=acct["ga_tax_payable"],
                memo="Georgia income tax withheld",
                credit=calc.ga_withholding,
            ),
            JournalLine(
                account=acct["ss_payable_ee"],
                memo=f"Employee Social Security 6.2% on ${calc.ss_taxable_wages:,.2f}",
                credit=calc.ss_ee,
            ),
            JournalLine(
                account=acct["medicare_payable_ee"],
                memo="Employee Medicare 1.45%"
                + (" + 0.9% additional" if calc.additional_medicare > 0 else ""),
                credit=calc.total_medicare_ee,
            ),
        ]

        # Medical insurance payable (cleared in Entry 3 when premium paid to insurer)
        if health_in_it and health > 0:
            entry1_lines.append(
                JournalLine(
                    account=acct["health_ins_payable"],
                    memo="Medical insurance premium - clear when paid to insurer",
                    credit=health,
                )
            )

        # HSA payable (cleared in Entry 4 when funds transferred to HSA account)
        if hsa > 0:
            entry1_lines.append(
                JournalLine(
                    account=acct["hsa_payable"],
                    memo="HSA contribution payable - clear when transferred to HSA",
                    credit=hsa,
                )
            )

        entry1_lines.append(
            JournalLine(
                account=acct["checking"],
                memo="Net paycheck to officer",
                credit=calc.net_pay,
            )
        )

        entries.append(
            JournalEntry(
                title="Entry 1: Record Monthly Payroll — Wages & Withholdings",
                note="Record on the last day of the pay period.",
                lines=entry1_lines,
            )
        )

        # ---- Entry 2: Employer payroll taxes ----
        entries.append(
            JournalEntry(
                title="Entry 2: Employer Payroll Taxes",
                note="Record on the same date as Entry 1.",
                lines=[
                    JournalLine(
                        account=acct["payroll_tax_expense"],
                        memo="Employer Social Security + Medicare",
                        debit=calc.total_employer_taxes,
                    ),
                    JournalLine(
                        account=acct["ss_payable_er"],
                        memo="Employer Social Security 6.2%",
                        credit=calc.ss_er,
                    ),
                    JournalLine(
                        account=acct["medicare_payable_er"],
                        memo="Employer Medicare 1.45%",
                        credit=calc.medicare_er,
                    ),
                ],
            )
        )

        # Track entry number dynamically (entries 1 and 2 are always present)
        entry_num = 3

        # ---- Entry 3: Clear medical insurance payable (when premium is paid) ----
        if health > 0:
            if health_in_it:
                entries.append(
                    JournalEntry(
                        title=f"Entry {entry_num}: Pay Medical Insurance Premium to Insurer",
                        note=(
                            "Record when you pay the insurance company. "
                            "Clears the medical insurance payable from Entry 1."
                        ),
                        lines=[
                            JournalLine(
                                account=acct["health_ins_payable"],
                                memo="Clear medical insurance payable",
                                debit=health,
                            ),
                            JournalLine(
                                account=acct["checking"],
                                memo="Payment to insurance company",
                                credit=health,
                            ),
                        ],
                    )
                )
            else:
                entries.append(
                    JournalEntry(
                        title=f"Entry {entry_num}: Medical Insurance Premium (Direct Payment)",
                        note=(
                            "Paid directly — not routed through payroll. "
                            "Confirm W-2 Box 1 addback treatment with your CPA."
                        ),
                        lines=[
                            JournalLine(
                                account=acct["health_insurance_exp"],
                                memo="Monthly medical insurance premium",
                                debit=health,
                            ),
                            JournalLine(
                                account=acct["checking"],
                                memo="Payment to insurance company",
                                credit=health,
                            ),
                        ],
                    )
                )
            entry_num += 1

        # ---- Entry 3/4: Clear HSA payable when funds transferred to HSA ----
        if hsa > 0:
            entries.append(
                JournalEntry(
                    title=f"Entry {entry_num}: HSA Employer Contribution",
                    note="Record when you transfer funds to the HSA account. Clears the HSA payable from Entry 1.",
                    lines=[
                        JournalLine(
                            account=acct["hsa_payable"],
                            memo="Clear HSA contribution payable",
                            debit=hsa,
                        ),
                        JournalLine(
                            account=acct["checking"],
                            memo="Transfer to HSA account",
                            credit=hsa,
                        ),
                    ],
                )
            )
            entry_num += 1

        # ---- Federal Tax Payment (EFTPS) ----
        fed_total = (
            calc.federal_withholding
            + calc.ss_ee
            + calc.ss_er
            + calc.total_medicare_ee
            + calc.medicare_er
        )
        entries.append(
            JournalEntry(
                title=f"Entry {entry_num}: Federal Tax Payment (EFTPS)",
                note="Record when you make the EFTPS deposit. Clears all federal tax payable accounts.",
                lines=[
                    JournalLine(
                        account=acct["fed_tax_payable"],
                        memo="Federal income tax withheld",
                        debit=calc.federal_withholding,
                    ),
                    JournalLine(
                        account=acct["ss_payable_ee"],
                        memo="Employee Social Security 6.2%",
                        debit=calc.ss_ee,
                    ),
                    JournalLine(
                        account=acct["ss_payable_er"],
                        memo="Employer Social Security 6.2%",
                        debit=calc.ss_er,
                    ),
                    JournalLine(
                        account=acct["medicare_payable_ee"],
                        memo="Employee Medicare 1.45%"
                        + (" + 0.9% additional" if calc.additional_medicare > 0 else ""),
                        debit=calc.total_medicare_ee,
                    ),
                    JournalLine(
                        account=acct["medicare_payable_er"],
                        memo="Employer Medicare 1.45%",
                        debit=calc.medicare_er,
                    ),
                    JournalLine(
                        account=acct["checking"],
                        memo="EFTPS federal tax deposit",
                        credit=fed_total,
                    ),
                ],
            )
        )
        entry_num += 1

        # ---- Georgia Tax Payment ----
        entries.append(
            JournalEntry(
                title=f"Entry {entry_num}: Georgia Tax Payment",
                note="Record when you pay via the Georgia Tax Center. Clears the GA withholding payable.",
                lines=[
                    JournalLine(
                        account=acct["ga_tax_payable"],
                        memo="Georgia income tax withheld",
                        debit=calc.ga_withholding,
                    ),
                    JournalLine(
                        account=acct["checking"],
                        memo="Payment to Georgia Department of Revenue",
                        credit=calc.ga_withholding,
                    ),
                ],
            )
        )

        return self._drop_zero_amounts(entries)

    @staticmethod
    def _drop_zero_amounts(entries: list[JournalEntry]) -> list[JournalEntry]:
        """Remove zero-amount lines and any entry left with nothing to post, then renumber."""
        kept: list[JournalEntry] = []
        for entry in entries:
            lines = [ln for ln in entry.lines if ln.debit or ln.credit]
            if not lines:
                continue
            title = entry.title.split(": ", 1)[-1]
            kept.append(
                entry.model_copy(
                    update={"title": f"Entry {len(kept) + 1}: {title}", "lines": lines}
                )
            )
        return kept

    def _build_tax_payment_summary(self, calc: TaxDetail) -> TaxPaymentSummary:
        ss_total = calc.ss_ee + calc.ss_er
        med_total = calc.total_medicare_ee + calc.medicare_er
        total_eftps = calc.federal_withholding + ss_total + med_total

        federal_due_note, georgia_due_note = self.config_service.get_due_date_notes()
        return TaxPaymentSummary(
            federal_income_tax=calc.federal_withholding,
            social_security_total=ss_total,
            medicare_total=med_total,
            total_federal_eftps=total_eftps,
            georgia_income_tax=calc.ga_withholding,
            federal_due_date_note=federal_due_note,
            georgia_due_date_note=georgia_due_note,
        )
