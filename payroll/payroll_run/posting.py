"""Approving a draft payroll run posts its journal entry; voiding an approved run reverses it.

Approval posts ONE entry through ``LedgerPostingService`` (source ``payroll_run``, source id
the run id) built from the run's STORED figures, never a recalculation. The stored figures
never change on approve or void; the database freezes them once the run is approved.

Locking order (spec section 6), the same in ``approve`` and ``void``: the eight payroll role
accounts FOR SHARE in id order, the net-pay account FOR SHARE, then every run of the tax
year FOR UPDATE in id order (so two approvals, or an approval and a void, cannot both pass
the chain check), then the period lock FOR SHARE and the per-source entry counter inside
``LedgerPostingService``.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.documents import (
    MAX_MEMO,
    RESERVED_ROLES,
    check_void_date,
    clean_text,
    commit_or_rollback,
    is_int,
    locked_role_account,
    utcnow_naive,
    void_memo,
)
from payroll.ledger.accounts import PAYROLL_ROLES
from payroll.ledger.models import Account, AccountType, SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

from .exceptions import (
    InvalidPayrollRunError,
    PayrollRoleNotBoundError,
    PayrollRunNotFoundError,
    PayrollRunStateError,
)
from .models import PayrollRun

logger = logging.getLogger(f"{app_settings.logger_name}.payroll_run.posting")

ZERO = Decimal("0.00")
NET_PAY_SOURCES = ("account", "owner")
# Stands in for the net-pay account of a draft's preview, which has not chosen one yet.
PREVIEW_NET_PAY = -1
PREVIEW_NET_PAY_LABEL = "Net pay account (chosen when approving)"


@dataclass(frozen=True)
class PostingAccounts:
    """The accounts a run posts to: the eight payroll roles and the net-pay account."""

    roles: Mapping[SystemRole, int]
    net_pay: int


@dataclass(frozen=True)
class PreviewLine:
    account_label: str
    debit: Decimal
    credit: Decimal
    memo: str | None


def _invalid(message: str) -> InvalidPayrollRunError:
    return InvalidPayrollRunError(message=message)


def _unbound(role: SystemRole) -> PayrollRoleNotBoundError:
    return PayrollRoleNotBoundError(
        message=f"The account role {role.value} is not bound; bind it in Settings > Ledger first"
    )


def build_lines(run: PayrollRun, accounts: PostingAccounts) -> list[LineDraft]:
    """The entry lines of spec section 6 for ``run``'s stored figures; zero lines dropped.

    Pure: no session, no database. The caller checks that debits equal credits.
    """
    health_in_tax = run.health_in_income_tax and run.health_insurance > 0
    hsa = run.hsa_contribution
    wages = run.gross_salary
    wages_memo = f"Gross salary ${run.gross_salary:,.2f}"
    if health_in_tax:
        wages += run.health_insurance
        wages_memo += f" + medical insurance W-2 addback ${run.health_insurance:,.2f}"
    if hsa > 0:
        wages += hsa
        wages_memo += f" + HSA contribution ${hsa:,.2f}"

    tax_memo = "Employer Social Security + Medicare"
    if run.futa > 0:
        tax_memo += " + FUTA"
    if run.suta > 0:
        tax_memo += " + Georgia SUTA"
    federal = (
        run.federal_withholding + run.ss_ee + run.total_medicare_ee + run.ss_er + run.medicare_er
    )
    role = accounts.roles
    drafts = [
        LineDraft(role[SystemRole.PAYROLL_WAGES], debit=wages, memo=wages_memo),
        LineDraft(
            role[SystemRole.PAYROLL_TAX_EXPENSE],
            debit=run.total_employer_taxes + run.futa + run.suta,
            memo=tax_memo,
        ),
        LineDraft(
            role[SystemRole.PAYROLL_FEDERAL_PAYABLE],
            credit=federal,
            memo="Federal income tax withheld + Social Security and Medicare "
            "(employee and employer)",
        ),
        LineDraft(
            role[SystemRole.PAYROLL_GA_WH_PAYABLE],
            credit=run.ga_withholding,
            memo="Georgia income tax withheld",
        ),
        LineDraft(
            role[SystemRole.PAYROLL_FUTA_PAYABLE],
            credit=run.futa,
            memo="Federal unemployment tax (FUTA)",
        ),
        LineDraft(
            role[SystemRole.PAYROLL_SUTA_PAYABLE],
            credit=run.suta,
            memo="Georgia unemployment tax (SUTA)",
        ),
        LineDraft(
            role[SystemRole.PAYROLL_HEALTH_PAYABLE],
            credit=run.health_insurance if health_in_tax else ZERO,
            memo="Medical insurance premium - clear when paid to insurer",
        ),
        LineDraft(
            role[SystemRole.PAYROLL_HSA_PAYABLE],
            credit=hsa,
            memo="HSA contribution payable - clear when transferred to HSA",
        ),
        LineDraft(accounts.net_pay, credit=run.net_pay, memo="Net paycheck to officer"),
    ]
    return [line for line in drafts if line.debit or line.credit]


class PayrollPostingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ reads

    def preview(self, run_id: int) -> list[PreviewLine]:
        """The lines approval would post (or did post), with no writes and no locks."""
        run = self._get(run_id)
        if run.status == "imported":
            raise PayrollRunStateError(
                message=f"Payroll run {run.pay_period} is imported history and is never posted"
            )
        roles = self._role_accounts(lock=False, required=True)
        labels = {account.id: account.name for account in roles.values()}
        if run.status == "draft":
            net_pay = PREVIEW_NET_PAY
            labels[net_pay] = PREVIEW_NET_PAY_LABEL
        else:
            account = self._stored_net_pay_account(run)
            if account is None:
                raise PayrollRoleNotBoundError(
                    message="Due to Owner is not bound; bind it in Settings > Ledger first"
                )
            net_pay = account.id  # type: ignore[assignment]
            labels[net_pay] = account.name
        lines = build_lines(
            run,
            PostingAccounts({r: a.id for r, a in roles.items()}, net_pay),  # type: ignore[misc]
        )
        return [PreviewLine(labels[ln.account_id], ln.debit, ln.credit, ln.memo) for ln in lines]

    # -------------------------------------------------------------- mutations

    def approve(
        self,
        run_id: int,
        *,
        actor: User,
        pay_date: date,
        net_pay_source: Literal["account", "owner"],
        net_pay_account_id: int | None,
    ) -> PayrollRun:
        ensure_role(actor, OWNER)

        def work() -> PayrollRun:
            # (1) inputs that need no database access
            if (
                not isinstance(pay_date, date)
                or isinstance(pay_date, datetime)
                or not is_supported_date(pay_date)
            ):
                raise _invalid(f"The pay date must be within {supported_range_text()}")
            if net_pay_source not in NET_PAY_SOURCES:
                raise _invalid("Choose whether net pay goes to a bank account or to Due to Owner")
            if net_pay_source == "account" and not is_int(net_pay_account_id):
                raise _invalid("Choose the bank account net pay is paid from")
            if net_pay_source == "owner" and net_pay_account_id is not None:
                raise _invalid("Net pay to the owner carries no bank account")
            run = self._get(run_id)

            # (2) locks: role accounts, net-pay account, then the tax year's runs
            roles = self._role_accounts(lock=True, required=True)
            net_pay = self._lock_net_pay(net_pay_source, net_pay_account_id)
            run = self._lock_year(run)

            # (3) checks under the locks
            if run.status != "draft":
                raise PayrollRunStateError(
                    message=f"Payroll run {run.pay_period} is {run.status}; "
                    "only a draft can be approved"
                )
            period_start = date(int(run.pay_period[:4]), int(run.pay_period[5:]), 1)
            if pay_date < period_start:
                raise _invalid(
                    f"The pay date cannot be before the pay period ({period_start.isoformat()})"
                )
            earlier_draft = self._year_runs(run, earlier=True, statuses=("draft",))
            if earlier_draft:
                raise PayrollRunStateError(
                    message=f"The {earlier_draft[0].pay_period} payroll run is still a draft; "
                    "approve or delete it first, because its year-to-date wages feed this run"
                )
            self._check_chain_figures(run)
            lines = build_lines(
                run,
                PostingAccounts({r: a.id for r, a in roles.items()}, net_pay.id),  # type: ignore[misc]
            )
            debits = sum((ln.debit for ln in lines), ZERO)
            credits = sum((ln.credit for ln in lines), ZERO)
            if len(lines) < 2:
                raise _invalid("The run has nothing to post")
            if debits != credits:
                raise _invalid(
                    f"The stored figures do not balance (debits {debits:,.2f}, "
                    f"credits {credits:,.2f}); delete the draft and save it again"
                )

            # (4) post, then mark the run approved
            before = self._snapshot(run)
            entry = LedgerPostingService(self.session).post(
                EntryDraft(
                    pay_date,
                    lines,
                    memo=f"Payroll run {run.pay_period}"[:MAX_MEMO],
                    source_type=SourceType.PAYROLL_RUN,
                    source_id=str(run.id),
                ),
                actor=actor,
            )
            run.status = "approved"
            run.pay_date = pay_date
            run.net_pay_source = net_pay_source
            run.net_pay_account_id = net_pay_account_id
            run.entry_id = entry.id
            run.modify_date = utcnow_naive()
            self.session.add(run)
            self.session.flush()
            record_audit(
                self.session,
                action="payroll_run.approve",
                user_id=actor.id,
                entity="payroll_run",
                entity_id=str(run.id),
                before=before,
                after=self._snapshot(run),
            )
            return run

        run = commit_or_rollback(self.session, work)
        logger.info("Approved payroll run id=%s", run.id)
        return run

    def void(
        self,
        run_id: int,
        *,
        actor: User,
        void_date: date,
        memo: str | None = None,
    ) -> PayrollRun:
        ensure_role(actor, OWNER)

        def work() -> PayrollRun:
            clean_memo = clean_text(memo, "Memo", MAX_MEMO, InvalidPayrollRunError)
            run = self._get(run_id)
            seen = (run.status, run.net_pay_account_id)

            # Same locks as approve: role accounts, net-pay account, the tax year's runs.
            self._role_accounts(lock=True, required=False)
            self._stored_net_pay_account(run, lock=True)
            run = self._lock_year(run)
            if (run.status, run.net_pay_account_id) != seen:
                # Changed by another request since the net-pay account to lock was chosen
                # from the earlier read; that may not be the account locked above.
                raise PayrollRunStateError(
                    message=f"Payroll run {run.pay_period} changed while it was being voided; "
                    "try again"
                )

            if run.status != "approved":
                raise PayrollRunStateError(
                    message=f"Payroll run {run.pay_period} is {run.status}; "
                    "only an approved run can be voided"
                )
            check_void_date(
                void_date,
                run.pay_date,  # type: ignore[arg-type]
                "payroll run",
                InvalidPayrollRunError,
            )
            later = self._year_runs(run, earlier=False, statuses=("draft", "approved", "imported"))
            if later:
                nxt = later[0]
                advice = {"draft": "delete it first", "approved": "void the later run first"}.get(
                    nxt.status, "imported history after this period cannot be undone"
                )
                raise PayrollRunStateError(
                    message=f"The {nxt.pay_period} payroll run is {nxt.status} after this one; "
                    f"{advice}"
                )

            before = self._snapshot(run)
            reversal = LedgerPostingService(self.session).void(
                run.entry_id,  # type: ignore[arg-type]
                entry_date=void_date,
                actor=actor,
                memo=void_memo(clean_memo, f"Void of payroll run {run.pay_period}"),
            )
            run.status = "void"
            run.void_date = void_date
            run.void_entry_id = reversal.id
            run.modify_date = utcnow_naive()
            self.session.add(run)
            self.session.flush()
            record_audit(
                self.session,
                action="payroll_run.void",
                user_id=actor.id,
                entity="payroll_run",
                entity_id=str(run.id),
                before=before,
                after=self._snapshot(run),
            )
            return run

        run = commit_or_rollback(self.session, work)
        logger.info("Voided payroll run id=%s", run.id)
        return run

    # ---------------------------------------------------------------- helpers

    def _get(self, run_id: int) -> PayrollRun:
        run = self.session.get(PayrollRun, run_id) if is_int(run_id) else None
        if run is None:
            raise PayrollRunNotFoundError(message=f"Payroll run with id={run_id} not found")
        return run

    def _role_accounts(self, *, lock: bool, required: bool) -> dict[SystemRole, Account]:
        """The accounts bound to the eight payroll roles, FOR SHARE in id order when ``lock``.

        A rebind that commits while this waits can leave a role missing from the result, so
        a missing role is read once more (a fresh statement sees the committed rebind).
        """
        by_role: dict[SystemRole, Account] = {}
        for _attempt in range(2):
            stmt = (
                select(Account)
                .where(col(Account.system_role).in_(PAYROLL_ROLES))
                .order_by(col(Account.id))
                .execution_options(populate_existing=True)
            )
            if lock:
                stmt = stmt.with_for_update(read=True)
            by_role = {SystemRole(a.system_role): a for a in self.session.exec(stmt).all()}
            if len(by_role) == len(PAYROLL_ROLES):
                break
        if required:
            for role in PAYROLL_ROLES:
                if role not in by_role:
                    raise _unbound(role)
        return by_role

    def _lock_net_pay(self, source: str, account_id: int | None) -> Account:
        if source == "owner":
            return locked_role_account(
                self.session,
                SystemRole.DUE_TO_OWNER,
                lambda: _unbound(SystemRole.DUE_TO_OWNER),
            )
        account = self.session.exec(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).first()
        if account is None:
            raise _invalid("The bank account was not found")
        if account.type != AccountType.ASSET or account.system_role in RESERVED_ROLES:
            raise _invalid(f"{account.name} cannot pay net pay; use a bank or cash account")
        if not account.is_active:
            raise _invalid(f"{account.name} is inactive")
        return account

    def _stored_net_pay_account(self, run: PayrollRun, *, lock: bool = False) -> Account | None:
        """The account net pay was posted to (None for an owner run whose role is now unbound)."""
        stmt = select(Account).execution_options(populate_existing=True)
        if run.net_pay_account_id is not None:
            stmt = stmt.where(Account.id == run.net_pay_account_id)
        else:
            stmt = stmt.where(Account.system_role == SystemRole.DUE_TO_OWNER)
        if lock:
            stmt = stmt.with_for_update(read=True)
        return self.session.exec(stmt).first()

    def _lock_year(self, run: PayrollRun) -> PayrollRun:
        """Lock every run of ``run``'s tax year FOR UPDATE in id order; return ``run`` re-read."""
        rows = self.session.exec(
            select(PayrollRun)
            .where(PayrollRun.tax_year == run.tax_year)
            .order_by(col(PayrollRun.id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for row in rows:
            if row.id == run.id:
                return row
        raise PayrollRunNotFoundError(message=f"Payroll run with id={run.id} not found")

    def _check_chain_figures(self, run: PayrollRun) -> None:
        """Year-to-date wages must continue from the latest earlier non-void run.

        With no earlier run the figure is a manual starting point and is accepted.
        """
        earlier = self._year_runs(run, earlier=True, statuses=("draft", "approved", "imported"))
        if earlier and run.ytd_fica_before != earlier[-1].ytd_fica_after:
            raise _invalid(
                f"The year-to-date figures of payroll run {run.pay_period} are out of date "
                f"(the {earlier[-1].pay_period} run ends at {earlier[-1].ytd_fica_after:,.2f}, "
                f"this run starts at {run.ytd_fica_before:,.2f}); "
                "delete it and save it again"
            )

    def _year_runs(
        self, run: PayrollRun, *, earlier: bool, statuses: tuple[str, ...]
    ) -> list[PayrollRun]:
        """Runs of the same tax year before (or after) ``run``'s period, oldest first."""
        period = col(PayrollRun.pay_period)
        return list(
            self.session.exec(
                select(PayrollRun)
                .where(
                    PayrollRun.tax_year == run.tax_year,
                    period < run.pay_period if earlier else period > run.pay_period,
                    col(PayrollRun.status).in_(statuses),
                )
                .order_by(period)
            ).all()
        )

    @staticmethod
    def _snapshot(run: PayrollRun) -> dict[str, Any]:
        return {
            "pay_period": run.pay_period,
            "status": run.status,
            "pay_date": run.pay_date,
            "net_pay": run.net_pay,
            "net_pay_source": run.net_pay_source,
            "net_pay_account_id": run.net_pay_account_id,
            "entry_id": run.entry_id,
            "void_date": run.void_date,
            "void_entry_id": run.void_entry_id,
        }
