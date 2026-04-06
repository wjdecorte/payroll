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
"""

import logging
import re
from datetime import datetime

from sqlmodel import Session, select

from payroll import app_settings
from payroll.tax import constants as tax
from payroll.tax.engine import calc_payroll
from payroll.tax.schemas import PayrollCalculation, TaxConstantsSchema

from .exceptions import (
    InvalidPayPeriodError,
    PayrollRunAlreadyExistsError,
    PayrollRunNotFoundError,
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

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.service")

_PAY_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PayrollRunService:
    """All business logic for payroll calculation and history management."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.accounts = app_settings.accounts

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(self, payload: PayrollInput) -> PayrollResult:
        """
        Calculate a payroll run and optionally save it.

        Raises:
            InvalidPayPeriodError: If pay_period is not in YYYY-MM format.
            PayrollRunAlreadyExistsError: If a run for this period already exists
                                          and save_run=True.
        """
        self._validate_pay_period(payload.pay_period)

        if payload.save_run:
            existing = self._find_by_period(payload.pay_period)
            if existing:
                raise PayrollRunAlreadyExistsError(
                    message=f"Payroll run for '{payload.pay_period}' already exists (id={existing.id}). "
                    "Delete it first or use a different pay period."
                )

        # Resolve YTD FICA wages from the most recent saved run if requested
        if payload.use_previous_ytd_fica:
            payload.ytd_fica_wages = self._get_previous_ytd_fica(payload.pay_period)
            logger.info("Resolved YTD FICA from previous run: %.2f", payload.ytd_fica_wages)

        calc = calc_payroll(
            gross_salary=payload.gross_salary,
            health_insurance=payload.health_insurance,
            hsa_contribution=payload.hsa_contribution,
            health_in_income_tax=payload.health_in_income_tax,
            hsa_in_income_tax=payload.hsa_in_income_tax,
            ytd_fica_wages=payload.ytd_fica_wages,
        )

        logger.info(
            "Calculated payroll for period=%s gross=%.2f net=%.2f",
            payload.pay_period,
            payload.gross_salary,
            calc.net_pay,
        )

        saved_run: PayrollRun | None = None
        if payload.save_run:
            saved_run = self._persist(payload, calc)
            logger.info("Saved payroll run id=%d", saved_run.id)

        return self._build_result(payload, calc, saved_run)

    def get_run(self, run_id: int) -> PayrollResult:
        """Retrieve a saved payroll run by ID and rebuild its full result."""
        run = self._get_or_raise(run_id)
        # Re-derive the full result from the saved inputs so journal entries
        # reflect any account-name changes made since the run was saved.
        payload = PayrollInput(
            pay_period=run.pay_period,
            gross_salary=run.gross_salary,
            health_insurance=run.health_insurance,
            hsa_contribution=run.hsa_contribution,
            health_in_income_tax=run.health_in_income_tax,
            hsa_in_income_tax=run.hsa_in_income_tax,
            ytd_fica_wages=run.ytd_fica_before,
            save_run=False,
            notes=run.notes,
        )
        calc = calc_payroll(
            gross_salary=run.gross_salary,
            health_insurance=run.health_insurance,
            hsa_contribution=run.hsa_contribution,
            health_in_income_tax=run.health_in_income_tax,
            hsa_in_income_tax=run.hsa_in_income_tax,
            ytd_fica_wages=run.ytd_fica_before,
        )
        return self._build_result(payload, calc, run)

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

    def delete_run(self, run_id: int) -> None:
        """Delete a payroll run by ID."""
        run = self._get_or_raise(run_id)
        self.session.delete(run)
        self.session.commit()
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
        return self.session.exec(
            select(PayrollRun).where(PayrollRun.pay_period == pay_period)
        ).first()

    def _get_previous_ytd_fica(self, pay_period: str) -> float:
        """Return ytd_fica_after from the most recent saved run before this period.

        Only considers runs in the same calendar year (YTD resets each January).
        Returns 0.0 when no prior run exists.
        """
        year = pay_period[:4]
        run = self.session.exec(
            select(PayrollRun)
            .where(
                PayrollRun.pay_period < pay_period,
                PayrollRun.pay_period >= f"{year}-01",
            )
            .order_by(PayrollRun.pay_period.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()
        return run.ytd_fica_after if run else 0.0

    def _get_or_raise(self, run_id: int) -> PayrollRun:
        run = self.session.get(PayrollRun, run_id)
        if not run:
            raise PayrollRunNotFoundError(message=f"Payroll run with id={run_id} not found")
        return run

    def _persist(self, payload: PayrollInput, calc: PayrollCalculation) -> PayrollRun:
        run = PayrollRun(
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
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def _build_result(
        self,
        payload: PayrollInput,
        calc: PayrollCalculation,
        run: PayrollRun | None,
    ) -> PayrollResult:
        acct = self.accounts
        gross = payload.gross_salary
        health = payload.health_insurance
        hsa = payload.hsa_contribution
        health_in_it = payload.health_in_income_tax

        journal_entries = self._build_journal_entries(acct, calc, gross, health, hsa, health_in_it)
        tax_payment = self._build_tax_payment_summary(calc)

        return PayrollResult(
            id=run.id if run else None,
            pay_period=payload.pay_period,
            gross_salary=gross,
            health_insurance=health,
            hsa_contribution=hsa,
            health_in_income_tax=health_in_it,
            hsa_in_income_tax=payload.hsa_in_income_tax,
            notes=payload.notes,
            saved=run is not None,
            tax_year=tax.TAX_YEAR,
            tax_constants=TaxConstantsSchema(),
            tax_detail=TaxDetail(
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
                ytd_fica_after=calc.ytd_fica_after,
            ),
            journal_entries=journal_entries,
            tax_payment_summary=tax_payment,
            create_date=run.create_date if run else datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # Journal entry builder
    # ------------------------------------------------------------------

    def _build_journal_entries(
        self,
        acct: dict[str, str],
        calc: PayrollCalculation,
        gross: float,
        health: float,
        hsa: float,
        health_in_it: bool,
    ) -> list[JournalEntry]:
        entries: list[JournalEntry] = []

        # ---- Entry 1: Gross payroll wages and withholdings ----
        # The debit to Officer Compensation includes the health insurance W-2
        # addback so the entry represents total W-2 Box 1 wages.
        dr_compensation = gross + (health if health_in_it and health > 0 else 0.0)

        entry1_lines: list[JournalLine] = [
            JournalLine(
                account=acct["officer_compensation"],
                memo=(
                    f"Gross salary ${gross:,.2f}"
                    + (
                        f" + health ins W-2 addback ${health:,.2f}"
                        if health_in_it and health > 0
                        else ""
                    )
                ),
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

        # Health insurance payable offset (cleared when premium paid to insurer)
        if health_in_it and health > 0:
            entry1_lines.append(
                JournalLine(
                    account=acct["health_ins_payable"],
                    memo="Health insurance premium — clear when paid to insurer",
                    credit=health,
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

        # ---- Entry 3: Clear health insurance payable (when premium is paid) ----
        if health > 0:
            if health_in_it:
                entries.append(
                    JournalEntry(
                        title="Entry 3: Pay Health Insurance Premium to Insurer",
                        note=(
                            "Record when you pay the insurance company. "
                            "Clears the Health Insurance Payable from Entry 1."
                        ),
                        lines=[
                            JournalLine(
                                account=acct["health_ins_payable"],
                                memo="Clear health insurance payable",
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
                        title="Entry 3: Health Insurance Premium (Direct Payment)",
                        note=(
                            "Paid directly — not routed through payroll. "
                            "Confirm W-2 Box 1 addback treatment with your CPA."
                        ),
                        lines=[
                            JournalLine(
                                account=acct["health_insurance_exp"],
                                memo="Monthly health insurance premium",
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

        # ---- Entry 4 (or 3): HSA contribution ----
        if hsa > 0:
            entry_num = 4 if health > 0 else 3
            entries.append(
                JournalEntry(
                    title=f"Entry {entry_num}: HSA Employer Contribution",
                    note="Record when funds are transferred to the HSA account.",
                    lines=[
                        JournalLine(
                            account=acct["hsa_expense"],
                            memo="Monthly HSA employer contribution",
                            debit=hsa,
                        ),
                        JournalLine(
                            account=acct["hsa_payable"],
                            memo="Transfer to HSA account",
                            credit=hsa,
                        ),
                    ],
                )
            )

        return entries

    def _build_tax_payment_summary(self, calc: PayrollCalculation) -> TaxPaymentSummary:
        ss_total = round(calc.ss_ee + calc.ss_er, 2)
        med_total = round(calc.total_medicare_ee + calc.medicare_er, 2)
        total_eftps = round(calc.federal_withholding + ss_total + med_total, 2)

        return TaxPaymentSummary(
            federal_income_tax=calc.federal_withholding,
            social_security_total=ss_total,
            medicare_total=med_total,
            total_federal_eftps=total_eftps,
            georgia_income_tax=calc.ga_withholding,
        )
