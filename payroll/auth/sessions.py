"""
Server-side login sessions and login lockout (SPEC SEC4/SEC6, DESIGN §8.2).

- The cookie carries a random token (``secrets.token_urlsafe(32)``); only its
  sha256 hex is stored, as ``AuthSession.id``, so a database leak yields no
  usable cookies.
- A session ends after ``session_idle_minutes`` without a request (measured from
  ``last_seen_at``) or ``session_absolute_hours`` after creation (``expires_at``),
  whichever comes first. Expired rows are deleted when they are next presented.
- Lockout: every ``max_failed_logins`` consecutive failures lock the account for
  ``lockout_minutes * 2**lock_count`` minutes (15, 30, 60, ...). A successful login
  resets all counters, so the next lockout starts again at the base duration.

Every function takes an injectable ``now`` (naive UTC) where time matters and
none of them commit: the caller owns the transaction.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import Session, select

from payroll import app_settings
from payroll.auth.models import AuthSession, User, utcnow

USER_AGENT_MAX = 512
IP_MAX = 45  # AuthSession.ip column width (an IPv6 text address fits)
# load_session refreshes last_seen_at only when it is older than this, so a burst
# of requests (page + HTMX partials + static) does not write the row every time.
# The idle timeout is therefore enforced with at most this much slack.
LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)


def hash_token(raw_token: str) -> str:
    """The stored session id for a raw cookie token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _is_expired(auth: AuthSession, now: datetime) -> bool:
    idle_deadline = auth.last_seen_at + timedelta(minutes=app_settings.session_idle_minutes)
    return now >= idle_deadline or now >= auth.expires_at


class SessionUserMissingError(LookupError):
    """The session's user row no longer exists."""


def _new_session(
    db: Session,
    user_id: int,
    *,
    ip: str | None,
    user_agent: str | None,
    expires_at: datetime | None = None,
) -> tuple[str, AuthSession]:
    now = utcnow()
    raw = secrets.token_urlsafe(32)
    auth = AuthSession(
        id=hash_token(raw),
        user_id=user_id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires_at or now + timedelta(hours=app_settings.session_absolute_hours),
        ip=ip[:IP_MAX] if ip else None,
        user_agent=user_agent[:USER_AGENT_MAX] if user_agent else None,
        mfa_verified=False,
        csrf_token=secrets.token_urlsafe(32),
    )
    db.add(auth)
    return raw, auth


def create_session(
    db: Session, user: User, *, ip: str | None, user_agent: str | None
) -> tuple[str, AuthSession]:
    """
    Start a session for ``user`` with a full absolute lifetime. Returns
    ``(raw_token, row)``; the raw token is only ever available here and must go
    straight into the cookie. ``mfa_verified`` starts False: the caller flips it
    after the second factor. ``ip`` and ``user_agent`` are truncated to fit.
    """
    return _new_session(db, user.id, ip=ip, user_agent=user_agent)


def load_session(
    db: Session, raw_token: str | None, now: datetime | None = None
) -> AuthSession | None:
    """
    The live session for ``raw_token``, or None when it is unknown or expired
    (idle or absolute). An expired row is deleted with a bulk ``DELETE`` (not an
    ORM instance delete), so parallel requests presenting the same expired cookie
    do not trip a "0 rows matched" warning. ``last_seen_at`` is refreshed only
    when older than :data:`LAST_SEEN_WRITE_INTERVAL` to avoid a write per request.
    """
    if not raw_token:
        return None
    now = now or utcnow()
    auth = db.exec(select(AuthSession).where(AuthSession.id == hash_token(raw_token))).first()
    if auth is None:
        return None
    if _is_expired(auth, now):
        db.exec(delete(AuthSession).where(AuthSession.id == auth.id))
        return None
    if now - auth.last_seen_at > LAST_SEEN_WRITE_INTERVAL:
        auth.last_seen_at = now
        db.add(auth)
    return auth


def revoke_session(db: Session, session_id: str) -> None:
    """End one session (logout)."""
    db.exec(delete(AuthSession).where(AuthSession.id == session_id))


def revoke_all(db: Session, user_id: int, *, except_id: str | None = None) -> None:
    """
    End every session of ``user_id`` (password change, MFA reset, deactivation),
    optionally keeping the one with id ``except_id`` (the caller's own).
    """
    stmt = delete(AuthSession).where(AuthSession.user_id == user_id)
    if except_id is not None:
        stmt = stmt.where(AuthSession.id != except_id)
    db.exec(stmt)


def rotate_session(
    db: Session,
    old: AuthSession,
    *,
    ip: str | None,
    user_agent: str | None,
    keep_expiry: bool = True,
) -> tuple[str, AuthSession]:
    """
    Replace ``old`` with a new token and a new CSRF token for the same user,
    keeping ``mfa_verified`` (the caller may then raise it). The old row is
    deleted, so the old cookie stops working immediately (session fixation).

    By default the absolute expiry carries over: rotating never extends a
    session's lifetime. Pass ``keep_expiry=False`` only when the rotation is part
    of a fresh login, which starts a full ``session_absolute_hours`` again.

    Raises :class:`SessionUserMissingError` (and leaves ``old`` untouched) if the
    session's user row no longer exists. Does not commit.
    """
    if db.get(User, old.user_id) is None:
        raise SessionUserMissingError(f"user {old.user_id} of the session no longer exists")
    mfa_verified = old.mfa_verified
    expires_at = old.expires_at if keep_expiry else None
    db.exec(delete(AuthSession).where(AuthSession.id == old.id))
    raw, auth = _new_session(db, old.user_id, ip=ip, user_agent=user_agent, expires_at=expires_at)
    auth.mfa_verified = mfa_verified
    return raw, auth


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------


def lock_user_row(db: Session, user: User) -> User:
    """
    ``SELECT ... FOR UPDATE`` the user's row and refresh ``user`` from it, so
    concurrent logins for the same account update the counters (and the TOTP
    replay step) one at a time from the committed values (no lost updates). The
    lock is held until the caller's transaction ends. Returns ``user``.

    ``user`` must be attached to ``db``: the refresh works through ``db``'s
    identity map, so a detached object or one from another session would keep
    stale values while the lock is taken on a different snapshot. Raises
    ``ValueError`` in that case.
    """
    if Session.object_session(user) is not db:
        raise ValueError("lock_user_row: user is not attached to the given db session")
    locked = db.exec(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    return locked


def is_locked(user: User, now: datetime | None = None) -> bool:
    """
    True while ``user.locked_until`` is in the future.

    Callers must check this BEFORE verifying the password and must not call
    :func:`register_failure` for an attempt made while the account is locked
    (otherwise lockouts compound and the doubling escalates without bound).
    """
    if user.locked_until is None:
        return False
    return (now or utcnow()) < user.locked_until


def register_failure(db: Session, user: User, now: datetime | None = None) -> None:
    """
    Count a failed login. On reaching ``max_failed_logins`` the account locks for
    ``lockout_minutes * 2**lock_count`` minutes, ``lock_count`` grows by one and the
    failure counter restarts.

    Self-serializing: the user's row is locked (``FOR UPDATE``) and re-read before
    counting, so parallel wrong-password requests are all counted. Call it only
    for attempts made while :func:`is_locked` was False (checked before the
    password). Does not commit: the caller commits, which releases the row lock.
    """
    now = now or utcnow()
    lock_user_row(db, user)
    user.failed_logins += 1
    if user.failed_logins >= app_settings.max_failed_logins:
        minutes = app_settings.lockout_minutes * 2**user.lock_count
        user.locked_until = now + timedelta(minutes=minutes)
        user.lock_count += 1
        user.failed_logins = 0
    db.add(user)
    db.flush()


def register_success(db: Session, user: User) -> None:
    """
    Reset the lockout state after a successful login. ``lock_count`` is reset as
    well (the doubling only escalates across consecutive lockouts with no
    successful login in between).

    Locks the user's row first like :func:`register_failure`, so a reset cannot
    interleave with a concurrent failure. Only call it for a login that passed
    the :func:`is_locked` check made before the password. Does not commit: the
    caller commits.
    """
    lock_user_row(db, user)
    user.failed_logins = 0
    user.locked_until = None
    user.lock_count = 0
    db.add(user)
    db.flush()
