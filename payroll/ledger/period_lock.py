"""The period lock: no journal entry may be dated on or before the lock date.

Changing it needs the owner's password again. The Argon2 verify (which goes through
the shared concurrency semaphore) runs first, before any row lock is taken; only
then is the single ``period_lock`` row locked ``FOR UPDATE``, changed and audited.
The password is never logged, audited or stored.
"""

import logging
from collections.abc import Callable
from datetime import date

from sqlmodel import Session, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.auth.security import verify_password
from payroll.ledger.accounts import utcnow_naive
from payroll.ledger.exceptions import LockPasswordError
from payroll.ledger.models import PeriodLock

logger = logging.getLogger(f"{app_settings.logger_name}.ledger.period_lock")


class PeriodLockService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> date | None:
        return self.session.exec(select(PeriodLock.lock_date).where(PeriodLock.id == 1)).first()

    def set(self, lock_date: date | None, *, password: str, actor: User) -> None:
        """Move (or clear, with ``None``) the lock date after re-checking the password."""
        ensure_role(actor, OWNER)
        self._commit(lambda: self._set(lock_date, password=password, actor=actor))

    def _set(self, lock_date: date | None, *, password: str, actor: User) -> None:
        # Verify first: no write has happened and no row lock is held while Argon2 runs.
        if not verify_password(password, actor.password_hash):
            record_audit(
                self.session,
                action="ledger.lock.denied",
                user_id=actor.id,
                entity="period_lock",
                entity_id="1",
                after={"requested_lock_date": lock_date},
            )
            self.session.commit()  # the denial is kept even though the request fails
            logger.warning("Period lock change denied for user id=%s: wrong password", actor.id)
            raise LockPasswordError()

        row = self.session.exec(
            select(PeriodLock)
            .where(PeriodLock.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        before = row.lock_date
        row.lock_date = lock_date
        row.set_by = actor.id
        row.set_at = utcnow_naive()
        self.session.add(row)
        self.session.flush()
        record_audit(
            self.session,
            action="ledger.lock.set",
            user_id=actor.id,
            entity="period_lock",
            entity_id="1",
            before={"lock_date": before},
            after={"lock_date": lock_date},
        )
        self.session.commit()
        logger.info("Period lock moved from %s to %s by user id=%s", before, lock_date, actor.id)

    def _commit(self, work: Callable[[], None]) -> None:
        """Run ``work``; on any error roll back at once so the row lock cannot outlive it.

        The routes are ``async def`` on a sync session, so a lock left held by a
        failed call could block the next request's query and freeze the event loop.
        """
        try:
            work()
        except Exception:
            self.session.rollback()
            raise
