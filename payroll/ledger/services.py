"""
The posting gateway: the only code that writes journal entries and lines.

``LedgerPostingService`` validates and posts entries, reverses and voids them. It
never commits: callers (``JournalService`` here, the document services later) own
the transaction, so a document and its entry commit or roll back together.

Gap-free numbering: the per-``source_type`` counter row is locked ``FOR UPDATE``
and incremented in the posting transaction, after every validation step, so a
rejected post never touches it and a rolled-back post releases its number. The
period lock row is read ``FOR SHARE``, so a post racing a lock-date change waits
for it and then sees the new date.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, col, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.dates import is_supported_date, supported_range_text
from payroll.common.money import CENT
from payroll.ledger.accounts import utcnow_naive
from payroll.ledger.exceptions import (
    AlreadyReversedError,
    EntryNotFoundError,
    InactiveAccountError,
    InvalidEntryError,
    PeriodLockedError,
    UnbalancedEntryError,
)
from payroll.ledger.models import (
    Account,
    EntryCounter,
    JournalEntry,
    JournalLine,
    PeriodLock,
    SourceType,
)

logger = logging.getLogger(f"{app_settings.logger_name}.ledger.services")

ZERO = Decimal("0")
# Largest amount a NUMERIC(14, 2) column holds.
MAX_AMOUNT = Decimal("999999999999.99")
MEMO_MAX = 500  # journal_entry.memo / journal_line.memo
SOURCE_ID_MAX = 100  # journal_entry.source_id


@dataclass(frozen=True)
class LineDraft:
    account_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: str | None = None


@dataclass(frozen=True)
class EntryDraft:
    entry_date: date
    lines: list[LineDraft] = field(default_factory=list)
    memo: str | None = None
    source_type: SourceType = SourceType.MANUAL
    source_id: str | None = None


def _check_amount(value: Any, where: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise InvalidEntryError(message=f"{where}: amounts must be Decimal values")
    if not value.is_finite():
        raise InvalidEntryError(message=f"{where}: amounts must be finite numbers")
    if value < 0:
        raise InvalidEntryError(message=f"{where}: amounts cannot be negative")
    if value > MAX_AMOUNT:
        raise InvalidEntryError(message=f"{where}: amount is too large (max {MAX_AMOUNT:,})")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise InvalidEntryError(message=f"{where}: amounts have at most 2 decimal places")
    return value.quantize(CENT)


def _check_text(value: str | None, limit: int, what: str) -> None:
    if value is not None and len(value) > limit:
        raise InvalidEntryError(message=f"The {what} is longer than {limit} characters")


class LedgerPostingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post(self, draft: EntryDraft, *, actor: User) -> JournalEntry:
        """Validate and post ``draft``; flushes and audits but does not commit."""
        return self._post(draft, actor=actor)

    def reverse(
        self, entry_id: int, *, entry_date: date, actor: User, memo: str | None = None
    ) -> JournalEntry:
        """Post the mirror image of an entry (debits and credits swapped), linked to it."""
        return self._reverse(entry_id, entry_date=entry_date, actor=actor, memo=memo)[1]

    def void(
        self, entry_id: int, *, entry_date: date, actor: User, memo: str | None = None
    ) -> JournalEntry:
        """Reverse an entry and mark it ``void``; returns the reversing entry."""
        original, reversal = self._reverse(
            entry_id, entry_date=entry_date, actor=actor, memo=memo, voiding=True
        )
        # status (and modify_date) are the only header columns the database lets change.
        original.status = "void"
        original.modify_date = utcnow_naive()
        self.session.add(original)
        self.session.flush()
        record_audit(
            self.session,
            action="ledger.void",
            user_id=actor.id,
            entity="journal_entry",
            entity_id=str(original.id),
            before={"status": "posted"},
            after={"status": "void", "reversal_entry_id": reversal.id},
        )
        logger.info("Voided journal entry id=%s (reversal id=%s)", original.id, reversal.id)
        return reversal

    @staticmethod
    def reversal_memo(entry: JournalEntry) -> str:
        return f"Reversal of {SourceType(entry.source_type).value} #{entry.entry_no}"

    def _reverse(
        self,
        entry_id: int,
        *,
        entry_date: date,
        actor: User,
        memo: str | None,
        voiding: bool = False,
    ) -> tuple[JournalEntry, JournalEntry]:
        original = self._lock_entry(entry_id)
        if voiding:
            # A void erases the entry from its own period, so the lock covers the original's
            # date too (a plain reversal dated after the lock is the correction path).
            # Same lock order as _post: entry row first, then the lock row FOR SHARE.
            lock_date = self.session.exec(
                select(PeriodLock.lock_date).where(PeriodLock.id == 1).with_for_update(read=True)
            ).first()
            if lock_date is not None and original.entry_date <= lock_date:
                if original.source_type == SourceType.MANUAL:
                    advice = "Reverse it with a date after the lock instead."
                else:
                    # A reversal dated after the lock would leave the document open while its
                    # entry is reversed (ledger/subledger split); void the document itself.
                    advice = (
                        f"To void this {original.source_type}, move the lock back "
                        "(Settings > Ledger), void the document, then lock again."
                    )
                raise PeriodLockedError(
                    message=f"The books are locked through {lock_date.isoformat()}; an entry "
                    f"dated in that period cannot be voided. {advice}"
                )
        lines = self.session.exec(
            select(JournalLine)
            .where(JournalLine.entry_id == original.id)
            .order_by(col(JournalLine.line_no))
        ).all()
        draft = EntryDraft(
            entry_date=entry_date,
            lines=[
                LineDraft(ln.account_id, debit=ln.credit, credit=ln.debit, memo=ln.memo)
                for ln in lines
            ],
            memo=memo if memo is not None else self.reversal_memo(original),
            source_type=SourceType(original.source_type),
            source_id=original.source_id,
        )
        return original, self._post(draft, actor=actor, reverses_entry_id=original.id)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _post(
        self, draft: EntryDraft, *, actor: User, reverses_entry_id: int | None = None
    ) -> JournalEntry:
        # (0) a date the reports can handle
        if not is_supported_date(draft.entry_date):
            raise InvalidEntryError(
                message=f"The entry date must be within {supported_range_text()}"
            )

        # (1) amounts and text
        try:
            source_type = SourceType(draft.source_type)
        except ValueError as exc:
            raise InvalidEntryError(message=f"Unknown source type {draft.source_type!r}") from exc
        _check_text(draft.memo, MEMO_MAX, "memo")
        _check_text(draft.source_id, SOURCE_ID_MAX, "source id")
        amounts: list[tuple[Decimal, Decimal]] = []
        for n, line in enumerate(draft.lines, start=1):
            where = f"Line {n}"
            debit = _check_amount(line.debit, where)
            credit = _check_amount(line.credit, where)
            if (debit > 0) == (credit > 0):
                raise InvalidEntryError(
                    message=f"{where}: exactly one of debit or credit must be greater than zero"
                )
            _check_text(line.memo, MEMO_MAX, f"line {n} memo")
            amounts.append((debit, credit))

        # (2) shape and balance
        if len(amounts) < 2:
            raise UnbalancedEntryError(message="A journal entry needs at least 2 lines")
        total_debit = sum((d for d, _ in amounts), ZERO)
        total_credit = sum((c for _, c in amounts), ZERO)
        if total_debit != total_credit:
            raise UnbalancedEntryError(
                message=f"Debits {total_debit:.2f} do not equal credits {total_credit:.2f}"
            )

        # (3) accounts exist and are active
        self._check_accounts({line.account_id for line in draft.lines})

        # (4) period lock
        lock_date = self.session.exec(
            select(PeriodLock.lock_date).where(PeriodLock.id == 1).with_for_update(read=True)
        ).one_or_none()
        if lock_date is not None and draft.entry_date <= lock_date:
            raise PeriodLockedError(
                message=f"The books are locked through {lock_date.isoformat()}; "
                f"{draft.entry_date.isoformat()} is in a locked period"
            )

        # (5) number
        entry_no = self._next_number(source_type)

        # (6) entry and lines
        entry = JournalEntry(
            entry_no=entry_no,
            source_type=source_type,
            source_id=draft.source_id,
            entry_date=draft.entry_date,
            memo=draft.memo,
            reverses_entry_id=reverses_entry_id,
            posted_by=actor.id,
        )
        self.session.add(entry)
        self.session.flush()
        self.session.add_all(
            [
                JournalLine(
                    entry_id=entry.id,
                    line_no=n,
                    account_id=line.account_id,
                    debit=debit,
                    credit=credit,
                    memo=line.memo,
                )
                for n, (line, (debit, credit)) in enumerate(
                    zip(draft.lines, amounts, strict=True), start=1
                )
            ]
        )
        self.session.flush()

        # (7) audit
        record_audit(
            self.session,
            action="ledger.post",
            user_id=actor.id,
            entity="journal_entry",
            entity_id=str(entry.id),
            after={
                "entry_no": entry_no,
                "source_type": source_type,
                "source_id": draft.source_id,
                "entry_date": draft.entry_date,
                "total_debit": total_debit,
                "total_credit": total_credit,
                "line_count": len(amounts),
                "reverses_entry_id": reverses_entry_id,
            },
        )
        logger.info("Posted journal entry id=%s %s #%s", entry.id, source_type.value, entry_no)
        return entry

    def _check_accounts(self, account_ids: set[int]) -> None:
        # FOR SHARE: an account cannot be deactivated or edited while a post uses it.
        accounts = {
            a.id: a
            for a in self.session.exec(
                select(Account)
                .where(col(Account.id).in_(account_ids))
                .order_by(col(Account.id))
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            ).all()
        }
        missing = sorted(account_ids - accounts.keys())
        if missing:
            raise InvalidEntryError(message=f"Unknown account id(s): {missing}")
        inactive = sorted(a.name for a in accounts.values() if not a.is_active)
        if inactive:
            raise InactiveAccountError(
                message=f"Inactive accounts cannot be posted to: {', '.join(inactive)}"
            )

    def _next_number(self, source_type: SourceType) -> int:
        self.session.execute(
            pg_insert(EntryCounter)
            .values(source_type=source_type.value, last_no=0)
            .on_conflict_do_nothing(index_elements=["source_type"])
        )
        last_no = self.session.exec(
            select(EntryCounter.last_no)
            .where(EntryCounter.source_type == source_type.value)
            .with_for_update()
        ).one()
        entry_no = last_no + 1
        self.session.execute(
            update(EntryCounter)
            .where(col(EntryCounter.source_type) == source_type.value)
            .values(last_no=entry_no)
            .execution_options(synchronize_session=False)
        )
        return entry_no

    def _lock_entry(self, entry_id: int) -> JournalEntry:
        """Load an entry ``FOR UPDATE`` (serializes reversals) and check it is reversible."""
        entry = self.session.exec(
            select(JournalEntry)
            .where(JournalEntry.id == entry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if entry is None:
            raise EntryNotFoundError()
        reversed_by = self.session.exec(
            select(JournalEntry.id).where(JournalEntry.reverses_entry_id == entry_id)
        ).first()
        if reversed_by is not None or entry.status != "posted":
            raise AlreadyReversedError(
                message=f"{SourceType(entry.source_type).value} #{entry.entry_no} "
                "has already been reversed or voided"
            )
        if entry.reverses_entry_id is not None:
            original_status = self.session.exec(
                select(JournalEntry.status).where(JournalEntry.id == entry.reverses_entry_id)
            ).one()
            if original_status == "void":
                raise AlreadyReversedError(
                    message=f"This entry is the reversal created by voiding #{entry.reverses_entry_id}"
                    "; post a new entry instead"
                )
        return entry


class JournalService:
    """Manual journal entries (owner only; each mutation commits) and journal reads."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.posting = LedgerPostingService(session)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, entry_id: int) -> JournalEntry:
        entry = self.session.get(JournalEntry, entry_id)
        if entry is None:
            raise EntryNotFoundError()
        return entry

    def list(
        self,
        start: date | None = None,
        end: date | None = None,
        source_type: SourceType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JournalEntry]:
        """Entries newest first (by entry date, then id)."""
        stmt = select(JournalEntry)
        if start is not None:
            stmt = stmt.where(JournalEntry.entry_date >= start)
        if end is not None:
            stmt = stmt.where(JournalEntry.entry_date <= end)
        if source_type is not None:
            stmt = stmt.where(JournalEntry.source_type == source_type)
        stmt = stmt.order_by(col(JournalEntry.entry_date).desc(), col(JournalEntry.id).desc())
        return list(self.session.exec(stmt.offset(offset).limit(limit)).all())

    def lines(self, entry_ids: Iterable[int]) -> dict[int, list[tuple[JournalLine, Account]]]:
        """Each entry's lines in order, with their accounts."""
        stmt = (
            select(JournalLine, Account)
            .join(Account, col(Account.id) == JournalLine.account_id)
            .where(col(JournalLine.entry_id).in_(list(entry_ids)))
            .order_by(col(JournalLine.entry_id), col(JournalLine.line_no))
        )
        result: dict[int, list[tuple[JournalLine, Account]]] = {}
        for line, account in self.session.exec(stmt).all():
            result.setdefault(line.entry_id, []).append((line, account))
        return result

    def reversed_by(self, entry_ids: Iterable[int]) -> dict[int, int]:
        """Original entry id -> the id of the entry that reverses it."""
        stmt = select(JournalEntry.reverses_entry_id, JournalEntry.id).where(
            col(JournalEntry.reverses_entry_id).in_(list(entry_ids))
        )
        return {orig: rev for orig, rev in self.session.exec(stmt).all() if orig is not None}

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create_manual(self, draft: EntryDraft, *, actor: User) -> JournalEntry:
        ensure_role(actor, OWNER)
        if draft.source_type != SourceType.MANUAL:
            raise InvalidEntryError(message="Only manual entries can be created here")
        return self._commit(lambda: self.posting.post(draft, actor=actor))

    def reverse_manual(
        self, entry_id: int, *, entry_date: date, actor: User, memo: str | None = None
    ) -> JournalEntry:
        ensure_role(actor, OWNER)
        self._check_manual(entry_id)
        return self._commit(
            lambda: self.posting.reverse(entry_id, entry_date=entry_date, actor=actor, memo=memo)
        )

    def void_manual(
        self, entry_id: int, *, entry_date: date, actor: User, memo: str | None = None
    ) -> JournalEntry:
        ensure_role(actor, OWNER)
        self._check_manual(entry_id)
        return self._commit(
            lambda: self.posting.void(entry_id, entry_date=entry_date, actor=actor, memo=memo)
        )

    def _commit(self, work: Callable[[], JournalEntry]) -> JournalEntry:
        """Run ``work`` and commit; on any error roll back at once.

        Posting takes row locks (FOR UPDATE on the entry and counter, FOR SHARE on
        accounts and the period lock). The routes are ``async def`` running this
        session on the event-loop thread, and the request session is only closed
        after the exception handlers run, so a lock left held by a failed call could
        block a second request's synchronous query and freeze the loop.
        """
        try:
            result = work()
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return result

    def _check_manual(self, entry_id: int) -> None:
        # Entries created by documents (invoices, payroll runs, ...) are reversed or
        # voided through their document, so the document and ledger stay in step.
        entry = self.get(entry_id)
        if entry.source_type != SourceType.MANUAL:
            raise InvalidEntryError(
                message=f"This {SourceType(entry.source_type).value} entry can only be "
                "changed from its source document"
            )
