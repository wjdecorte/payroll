"""
Login pages (SPEC SEC1-SEC6, DESIGN §8.2). HTML only, hidden from OpenAPI.

  GET/POST /login             email + password (step 1)
  GET/POST /login/mfa         TOTP or recovery code (step 2)
  GET/POST /enroll            first login: set up TOTP, show recovery codes once
  POST     /logout            end the session, back to /login
  GET/POST /account/password  change password (signed-in users)

Rules that matter for security:

- No account enumeration: unknown email, wrong password, locked and inactive
  accounts get the same status and body, and each runs one Argon2 verify (a
  dummy one when there is no usable hash to check).
- Lockout: ``is_locked`` is checked before the credential, and attempts made
  while locked are never counted (no compounding). Password, MFA and enrollment
  failures all count; counters reset only once the second factor succeeds.
- The Argon2 password verify runs WITHOUT the user row locked: the row is
  locked only afterwards, to count a failure or finish a success, and
  ``is_locked`` is checked again under the lock (an attempt that raced past the
  threshold is refused and not counted). Holding the lock across the hash would
  queue parallel attempts for a real email (a timing oracle) and let a burst of
  anonymous POSTs pin the connection pool.
- After taking the user row lock, MFA/enroll/password-change re-load their own
  session row (``FOR UPDATE``); if it was revoked meanwhile, the request is
  treated as signed out instead of rotating a deleted session.
- Every ``GET /enroll`` issues a NEW pending secret (audited as
  ``auth.mfa.enroll_started``), so two pending sessions never share one: the
  later one silently invalidates the earlier one's QR code.
- TOTP is checked with the user row locked ``FOR UPDATE`` and committed, so the
  replay guard (``totp_last_step``) holds under concurrent submissions.
- The session id changes at every privilege step: a new session after the
  password, a rotated one after MFA/enrollment and after a password change.
- Every ``next`` goes through ``safe_next_path``.
- A CSRF failure on the three login forms (e.g. a page left open across a login
  in another tab) redirects to the form with an "expired" notice and a fresh
  token instead of a bare 403; see :func:`csrf_error_handler`.

All times come from :func:`_now` so tests can move the clock.
"""

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, select

from payroll import app_settings
from payroll.audit.services import record
from payroll.auth.dependencies import (
    CSRFError,
    csrf_context,
    csrf_cookie_name,
    current_session,
    current_user,
    pending_session_user,
    safe_next_path,
    session_cookie_name,
)
from payroll.auth.models import AuthSession, User, utcnow
from payroll.auth.security import (
    PasswordPolicyError,
    consume_recovery_code,
    hash_password,
    issue_recovery_codes,
    new_totp_secret,
    password_needs_rehash,
    totp_uri,
    validate_password_policy,
    verify_password,
    verify_totp,
)
from payroll.auth.sessions import (
    create_session,
    is_locked,
    lock_user_row,
    register_failure,
    register_success,
    revoke_all,
    revoke_session,
    rotate_session,
)
from payroll.common.crypto import decrypt_secret, encrypt_secret
from payroll.common.dependencies import get_session

logger = logging.getLogger(f"{app_settings.logger_name}.auth")

LOGIN_ERROR = "Invalid email or password."
MFA_ERROR = "Invalid code."
EXPIRED_NOTICE = "Your page expired, please try again."
AUDIT_EMAIL_MAX = 320
# POSTs whose CSRF failure is shown as an "expired" form rather than a 403.
EXPIRED_FORM_PATHS = frozenset({"/login", "/login/mfa", "/enroll"})
LOGOUT_PATH = "/logout"

_HERE = Path(__file__).parent
# Auth pages extend the frontend's base.html, so both directories are searched.
templates = Jinja2Templates(
    directory=[str(_HERE / "templates"), str(_HERE.parent / "frontend" / "templates")],
    context_processors=[csrf_context],
)
templates.env.globals.update(
    app_name=app_settings.app_name,
    app_version=app_settings.app_version,
    build_number=app_settings.build_number,
    build_sha=app_settings.build_sha[:7],
)

router = APIRouter(include_in_schema=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """The current time (naive UTC). Tests monkeypatch this to move the clock."""
    return utcnow()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _lock_for_update(db: Session, user: User) -> User:
    """Lock and refresh the user row for the rest of this transaction."""
    return lock_user_row(db, user)


def _session_id(auth: AuthSession) -> str:
    """The session's primary key without touching the database (the row may be gone)."""
    return sa_inspect(auth).identity[0]


def _live_session(db: Session, session_id: str) -> AuthSession | None:
    """
    Re-load the session row ``FOR UPDATE`` (after the user row: same lock order
    everywhere), or None if it was revoked since the request started.
    """
    return db.exec(
        select(AuthSession)
        .where(AuthSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _signed_out(db: Session) -> RedirectResponse:
    """The request's session disappeared mid-request: release locks, go to /login."""
    db.commit()
    return _redirect("/login")


def _with_next(path: str, next_path: str, **extra: str) -> str:
    return f"{path}?{urlencode({**extra, 'next': next_path})}"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _cookie_kwargs() -> dict:
    return {
        "path": "/",
        "secure": app_settings.session_cookie_secure,
        "httponly": True,
        "samesite": "lax",
    }


def _set_session_cookie(response: Response, raw: str, auth: AuthSession) -> None:
    """HttpOnly, SameSite=Lax, Path=/, Secure per settings; lives as long as the session."""
    max_age = max(0, round((auth.expires_at - utcnow()).total_seconds()))
    response.set_cookie(session_cookie_name(), raw, max_age=max_age, **_cookie_kwargs())


def _adopt_session(request: Request, auth: AuthSession) -> None:
    """Make a page rendered in this request use the new session's CSRF token."""
    request.state.auth_session = auth
    request.state.csrf_token = auth.csrf_token


def _verify_totp_safely(user: User, code: str, now: datetime) -> bool:
    try:
        return verify_totp(user, code, now)
    except ValueError:
        # Undecryptable secret (wrong ENCRYPTION_KEY?): fail closed, tell the operator.
        logger.error("TOTP secret for user %s cannot be decrypted", user.id)
        return False


def _count_failure(db: Session, user: User, now: datetime, ip: str | None) -> None:
    """register_failure, plus an ``auth.lockout`` audit row when it locked the account."""
    before = user.locked_until
    register_failure(db, user, now)
    if user.locked_until is not None and user.locked_until != before:
        record(
            db,
            action="auth.lockout",
            user_id=user.id,
            entity="user",
            entity_id=str(user.id),
            after={"locked_until": user.locked_until, "lock_count": user.lock_count},
            ip=ip,
        )


def _next_from(form_value: str | None, request: Request) -> str:
    """``next`` from the form, else the query string, sanitized."""
    value = form_value if form_value else request.query_params.get("next")
    return safe_next_path(value)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_login(
    request: Request, next_path: str, *, error: str | None = None, expired: bool = False
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next_path": next_path, "error": error, "notice": EXPIRED_NOTICE if expired else None},
        status_code=401 if error else 200,
    )


def _render_mfa(
    request: Request, next_path: str, *, error: str | None = None, expired: bool = False
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "mfa.html",
        {"next_path": next_path, "error": error, "notice": EXPIRED_NOTICE if expired else None},
        status_code=401 if error else 200,
    )


def _render_enroll(
    request: Request,
    user: User,
    secret: str,
    next_path: str,
    *,
    error: str | None = None,
    expired: bool = False,
) -> HTMLResponse:
    qr_svg = segno.make(totp_uri(secret, user.email), error="m").svg_inline(scale=4, border=2)
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    return templates.TemplateResponse(
        request,
        "enroll.html",
        {
            "qr_svg": qr_svg,
            "secret": grouped,
            "next_path": next_path,
            "error": error,
            "notice": EXPIRED_NOTICE if expired else None,
        },
        status_code=401 if error else 200,
    )


def _render_password(
    request: Request, *, error: str | None = None, success: bool = False
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "change_password.html",
        {"error": error, "success": success},
        status_code=400 if error else 200,
    )


# ---------------------------------------------------------------------------
# Step 1: password
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_: str | None = Query(None, alias="next"),
    expired: bool = False,
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    next_path = safe_next_path(next_)
    if auth is not None and auth.mfa_verified:
        user = db.get(User, auth.user_id)
        if user is not None and user.is_active:
            return _redirect(next_path)
    return _render_login(request, next_path, expired=expired)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next_: str | None = Form(None, alias="next"),
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    now = _now()
    ip = _client_ip(request)
    next_path = _next_from(next_, request)
    normalized = email.strip().lower()

    # No row lock yet: the Argon2 verify below must not run while holding it.
    user = db.exec(select(User).where(User.email == normalized)).first()

    # The hash this attempt is judged against; re-checked under the lock below.
    verified_hash = user.password_hash if user is not None else None
    reason: str | None = None
    if user is not None and is_locked(user, now):
        verify_password(password, None)  # same cost as a real attempt; never counted
        reason = "locked"
    elif not verify_password(password, verified_hash):
        reason = "unknown_email" if user is None else "bad_password"
    elif not user.is_active:
        reason = "inactive"

    # An upgraded hash is computed now, outside the lock (Argon2 is slow).
    upgraded_hash = None
    if reason is None and password_needs_rehash(verified_hash):
        upgraded_hash = hash_password(password)

    if reason in (None, "bad_password"):
        # Lock (and refresh) the row only now, to count the failure or finish the
        # login, and re-check what may have changed while this one was hashing:
        # - locked by a parallel attempt: refused and not counted;
        # - password changed (its revoke_all already ran): the old password no
        #   longer counts, so it is an ordinary wrong-password failure;
        # - deactivated.
        user = _lock_for_update(db, user)
        if is_locked(user, now):
            reason = "locked"
        elif reason is None and user.password_hash != verified_hash:
            reason = "bad_password"
        elif reason is None and not user.is_active:
            reason = "inactive"

    if reason is not None:
        if reason == "bad_password":
            _count_failure(db, user, now, ip)
        record(
            db,
            action="auth.login.failure",
            user_id=user.id if user is not None else None,
            after={"email": normalized[:AUDIT_EMAIL_MAX], "reason": reason},
            ip=ip,
        )
        db.commit()
        return _render_login(request, next_path, error=LOGIN_ERROR)

    if upgraded_hash is not None:
        # Safe: under the lock the stored hash is still the one that was verified.
        user.password_hash = upgraded_hash
        db.add(user)
    if auth is not None:
        revoke_session(db, auth.id)  # never carry a pre-login session forward
    raw, new = create_session(db, user, ip=ip, user_agent=_user_agent(request))
    record(db, action="auth.login.password_ok", user_id=user.id, ip=ip)
    db.commit()

    target = "/login/mfa" if user.totp_enabled else "/enroll"
    response = _redirect(_with_next(target, next_path))
    _set_session_cookie(response, raw, new)
    return response


# ---------------------------------------------------------------------------
# Step 2: TOTP or recovery code
# ---------------------------------------------------------------------------


@router.get("/login/mfa", response_class=HTMLResponse)
def mfa_page(
    request: Request,
    next_: str | None = Query(None, alias="next"),
    expired: bool = False,
    user: User = Depends(pending_session_user),
    auth: AuthSession = Depends(current_session),
) -> Response:
    next_path = safe_next_path(next_)
    if auth.mfa_verified:
        return _redirect(next_path)
    if not user.totp_enabled:
        return _redirect(_with_next("/enroll", next_path))
    return _render_mfa(request, next_path, expired=expired)


@router.post("/login/mfa", response_class=HTMLResponse)
def mfa_submit(
    request: Request,
    code: str = Form(""),
    next_: str | None = Form(None, alias="next"),
    user: User = Depends(pending_session_user),
    auth: AuthSession = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    next_path = _next_from(next_, request)
    if auth.mfa_verified:
        return _redirect(next_path)
    if not user.totp_enabled:
        return _redirect(_with_next("/enroll", next_path))
    now = _now()
    ip = _client_ip(request)
    session_id = _session_id(auth)
    user = _lock_for_update(db, user)
    auth = _live_session(db, session_id)
    if auth is None:
        return _signed_out(db)

    method: str | None = None
    locked = is_locked(user, now)
    if not locked:
        if _verify_totp_safely(user, code, now):
            method = "totp"
        elif consume_recovery_code(db, user.id, code):
            method = "recovery_code"

    if method is None:
        if not locked:
            _count_failure(db, user, now, ip)
        record(
            db,
            action="auth.mfa.failure",
            user_id=user.id,
            after={"stage": "login", "locked": locked},
            ip=ip,
        )
        db.commit()
        return _render_mfa(request, next_path, error=MFA_ERROR)

    register_success(db, user)
    raw, new = rotate_session(db, auth, ip=ip, user_agent=_user_agent(request))
    new.mfa_verified = True
    record(db, action="auth.login.success", user_id=user.id, after={"method": method}, ip=ip)
    db.commit()

    response = _redirect(next_path)
    _set_session_cookie(response, raw, new)
    return response


# ---------------------------------------------------------------------------
# First login: TOTP enrollment
# ---------------------------------------------------------------------------


def _issue_enrollment_secret(db: Session, user: User, ip: str | None) -> str | None:
    """
    Store a NEW pending TOTP secret (encrypted; ``totp_enabled`` stays False) and
    return it, or None if the user enrolled meanwhile (checked under the row
    lock: an enabled secret is never overwritten). Each call invalidates any
    QR code shown earlier, so a second pending session cannot silently share
    the first one's secret.
    """
    user = _lock_for_update(db, user)
    if user.totp_enabled:
        db.commit()
        return None
    secret = new_totp_secret()
    user.totp_secret_enc = encrypt_secret(secret)
    user.totp_last_step = None
    db.add(user)
    record(db, action="auth.mfa.enroll_started", user_id=user.id, ip=ip)
    db.commit()
    return secret


def _pending_secret(db: Session, user: User, ip: str | None) -> str | None:
    """The stored pending secret (to re-show after a wrong code), else a new one."""
    if user.totp_secret_enc is not None:
        try:
            return decrypt_secret(user.totp_secret_enc)
        except ValueError:
            logger.warning("Unreadable pending TOTP secret for user %s; issuing a new one", user.id)
    return _issue_enrollment_secret(db, user, ip)


@router.get("/enroll", response_class=HTMLResponse)
def enroll_page(
    request: Request,
    next_: str | None = Query(None, alias="next"),
    expired: bool = False,
    user: User = Depends(pending_session_user),
    db: Session = Depends(get_session),
) -> Response:
    next_path = safe_next_path(next_)
    secret = None if user.totp_enabled else _issue_enrollment_secret(db, user, _client_ip(request))
    if secret is None:
        return _redirect(_with_next("/login/mfa", next_path))
    return _render_enroll(request, user, secret, next_path, expired=expired)


@router.post("/enroll", response_class=HTMLResponse)
def enroll_submit(
    request: Request,
    code: str = Form(""),
    next_: str | None = Form(None, alias="next"),
    user: User = Depends(pending_session_user),
    auth: AuthSession = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    next_path = _next_from(next_, request)
    now = _now()
    ip = _client_ip(request)
    session_id = _session_id(auth)
    user = _lock_for_update(db, user)
    auth = _live_session(db, session_id)
    if auth is None:
        return _signed_out(db)
    # Never let a password-only session replace an existing second factor.
    if user.totp_enabled:
        db.commit()
        return _redirect(_with_next("/login/mfa", next_path))
    if user.totp_secret_enc is None:
        db.commit()
        return _redirect(_with_next("/enroll", next_path))

    locked = is_locked(user, now)
    if locked or not _verify_totp_safely(user, code, now):
        if not locked:
            _count_failure(db, user, now, ip)
        record(
            db,
            action="auth.mfa.failure",
            user_id=user.id,
            after={"stage": "enroll", "locked": locked},
            ip=ip,
        )
        db.commit()
        secret = _pending_secret(db, user, ip)
        if secret is None:
            return _redirect(_with_next("/login/mfa", next_path))
        return _render_enroll(request, user, secret, next_path, error=MFA_ERROR)

    user.totp_enabled = True
    db.add(user)
    codes = issue_recovery_codes(db, user.id)
    register_success(db, user)
    raw, new = rotate_session(db, auth, ip=ip, user_agent=_user_agent(request))
    new.mfa_verified = True
    record(
        db,
        action="auth.mfa.enrolled",
        user_id=user.id,
        entity="user",
        entity_id=str(user.id),
        ip=ip,
    )
    db.commit()

    _adopt_session(request, new)
    # Shown once: only hashes are stored, and the response is Cache-Control: no-store.
    response = templates.TemplateResponse(
        request, "enroll_done.html", {"codes": codes, "next_path": next_path}
    )
    _set_session_cookie(response, raw, new)
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
def logout(
    request: Request,
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    """End the session and go to /login (never renders a page that needs the session)."""
    if auth is not None:
        revoke_session(db, auth.id)
        record(db, action="auth.logout", user_id=auth.user_id, ip=_client_ip(request))
        db.commit()
    response = _redirect("/login")
    response.delete_cookie(session_cookie_name(), **_cookie_kwargs())
    response.delete_cookie(csrf_cookie_name(), **_cookie_kwargs())
    return response


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user: User = Depends(current_user)) -> Response:
    return _render_password(request)


@router.post("/account/password", response_class=HTMLResponse)
def password_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    user: User = Depends(current_user),
    auth: AuthSession = Depends(current_session),
    db: Session = Depends(get_session),
) -> Response:
    now = _now()
    ip = _client_ip(request)
    session_id = _session_id(auth)

    # As on /login: verify first (no row lock held during Argon2), then lock to
    # count a failure, re-checking the lockout under the lock.
    verified_hash = user.password_hash
    locked = is_locked(user, now)
    if locked or not verify_password(current_password, verified_hash):
        # A stolen session must not get unthrottled guesses at the password.
        user = _lock_for_update(db, user)
        if not locked and not is_locked(user, now):
            _count_failure(db, user, now, ip)
        record(db, action="auth.password.change_failed", user_id=user.id, ip=ip)
        db.commit()
        return _render_password(request, error="Current password is incorrect.")

    if new_password != confirm_password:
        return _render_password(request, error="The new passwords do not match.")
    try:
        validate_password_policy(new_password)
    except PasswordPolicyError as exc:
        return _render_password(request, error=exc.message)

    new_hash = hash_password(new_password)  # outside the lock, like the verify
    user = _lock_for_update(db, user)
    auth = _live_session(db, session_id)
    if auth is None or not user.is_active:
        return _signed_out(db)
    if is_locked(user, now) or user.password_hash != verified_hash:
        # Locked meanwhile, or the password changed elsewhere since it was checked:
        # the current password that was verified no longer applies. Not counted.
        db.commit()
        return _render_password(request, error="Current password is incorrect.")
    user.password_hash = new_hash
    db.add(user)
    raw, new = rotate_session(db, auth, ip=ip, user_agent=_user_agent(request))
    db.flush()
    revoke_all(db, user.id, except_id=new.id)
    record(
        db,
        action="auth.password.changed",
        user_id=user.id,
        entity="user",
        entity_id=str(user.id),
        ip=ip,
    )
    db.commit()

    _adopt_session(request, new)
    response = _render_password(request, success=True)
    _set_session_cookie(response, raw, new)
    return response


# ---------------------------------------------------------------------------
# CSRF failures on the login forms
# ---------------------------------------------------------------------------


async def csrf_error_handler(request: Request, exc: CSRFError) -> Response:
    """
    A POST to one of the login forms with a stale/missing CSRF token (typically
    a page opened before logging in elsewhere) is sent back to the form with an
    "expired" notice; the GET renders a fresh token. Every other CSRF failure
    stays a plain 403, except ``POST /logout``, which goes to /login. The body is not read here: ``next`` comes from the query
    string, which the forms include in their ``action``.
    """
    path = request.url.path
    if request.method == "POST" and path == LOGOUT_PATH:
        # Nothing to re-render: go to /login without ending the session (the
        # request may be forged). A live session's GET /login moves on to "/".
        return _redirect("/login")
    if request.method == "POST" and path in EXPIRED_FORM_PATHS:
        next_path = safe_next_path(request.query_params.get("next"))
        return _redirect(_with_next(path, next_path, expired="1"))
    return await http_exception_handler(request, exc)
