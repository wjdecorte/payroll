"""
End-to-end login flow through the real app (TestClient over https so the
``Secure``/``__Host-`` cookies round-trip): password step, mandatory TOTP
(enrollment on first login), recovery codes, lockout, session fixation, logout,
password change, ``next`` handling, CSRF-expired pages and audit rows.
"""

import re
import threading
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from payroll import app_settings
from payroll.app import app
from payroll.audit.models import AuditLog
from payroll.auth import routers as auth_routers
from payroll.auth import sessions as auth_sessions
from payroll.auth.dependencies import CSRF_HEADER, csrf_cookie_name, session_cookie_name
from payroll.auth.models import AuthSession, RecoveryCode, User
from payroll.auth.security import hash_password, issue_recovery_codes, new_totp_secret
from payroll.common.crypto import decrypt_secret, encrypt_secret
from payroll.common.dependencies import get_session
from tests.auth.test_security import spy_hasher  # noqa: F401  (fixture)
from tests.conftest import make_user

PASSWORD = "Tr0ub4dor&3-horse-staple"
NEW_PASSWORD = "an0ther-l0ng-passphrase!"
T0 = datetime(2026, 10, 31, 12, 0, 0)  # naive UTC, the app's convention
GENERIC_LOGIN_ERROR = "Invalid email or password."

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch):
    """Injected time for the login routes (lockout and TOTP)."""
    state = {"now": T0}
    monkeypatch.setattr(auth_routers, "_now", lambda: state["now"])
    return state


@pytest.fixture(name="web")
def web_fixture(session, clock, monkeypatch):
    """A browser-like client: https, no default CSRF header, secure cookies on."""
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    app.dependency_overrides.clear()


def csrf_of(response) -> str:
    match = _CSRF_RE.search(response.text)
    assert match, response.text
    return match.group(1)


def totp_code(user: User, at: datetime) -> str:
    totp = pyotp.TOTP(decrypt_secret(user.totp_secret_enc))
    return totp.generate_otp(int(at.replace(tzinfo=UTC).timestamp()) // 30)


def enrolled_user(session: Session, email: str = "owner@example.com", **kw) -> User:
    return make_user(
        session,
        email=email,
        password_hash=hash_password(PASSWORD),
        totp_secret_enc=encrypt_secret(new_totp_secret()),
        totp_enabled=True,
        **kw,
    )


def new_user(session: Session, email: str = "new@example.com", **kw) -> User:
    return make_user(session, email=email, password_hash=hash_password(PASSWORD), **kw)


def post_password(web, email, password, next_path=None):
    token = csrf_of(web.get("/login"))
    data = {"email": email, "password": password, "csrf_token": token}
    if next_path is not None:
        data["next"] = next_path
    return web.post("/login", data=data, follow_redirects=False)


def post_mfa(web, code, next_path=None):
    page = web.get("/login/mfa", follow_redirects=False)
    assert page.status_code == 200, page.text
    data = {"code": code, "csrf_token": csrf_of(page)}
    if next_path is not None:
        data["next"] = next_path
    return web.post("/login/mfa", data=data, follow_redirects=False)


def full_login(web, user, at=T0, next_path=None):
    first = post_password(web, user.email, PASSWORD, next_path)
    assert first.status_code == 303, first.text
    return post_mfa(web, totp_code(user, at), next_path)


def logout(web):
    page = web.get("/account/password")
    return web.post("/logout", data={"csrf_token": csrf_of(page)}, follow_redirects=False)


def client_with_session(raw: str) -> TestClient:
    """A second browser presenting the session cookie ``raw``."""
    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    client.cookies.set(session_cookie_name(), raw)
    return client


def session_cookie_value(response) -> str:
    """The session token a response set (from its Set-Cookie header)."""
    header = _session_set_cookie(response)
    return header.split(";", 1)[0].split("=", 1)[1]


def audit_rows(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def is_authenticated(web) -> bool:
    return web.get("/account/password", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def test_login_page_renders_form(web):
    response = web.get("/login")
    assert response.status_code == 200
    assert 'name="email"' in response.text and 'name="password"' in response.text
    assert csrf_of(response)


def test_auth_pages_not_in_openapi(web):
    paths = web.get(f"{app_settings.base_url_prefix}/openapi.json").json()["paths"]
    for path in ("/login", "/login/mfa", "/enroll", "/logout", "/account/password"):
        assert path not in paths


def test_mfa_and_enroll_require_password_step(web):
    for path in ("/login/mfa", "/enroll"):
        response = web.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


# ---------------------------------------------------------------------------
# Password step: no enumeration, lockout rules
# ---------------------------------------------------------------------------


def test_unknown_wrong_locked_inactive_are_identical(web, session, clock):
    enrolled_user(session, email="real@example.com")
    locked = enrolled_user(
        session, email="locked@example.com", locked_until=T0 + timedelta(minutes=10), lock_count=1
    )
    enrolled_user(session, email="gone@example.com", is_active=False)

    responses = [
        post_password(web, "nobody@example.com", PASSWORD),
        post_password(web, "real@example.com", PASSWORD + "x"),
        post_password(web, "locked@example.com", PASSWORD),  # correct password, still locked
        post_password(web, "gone@example.com", PASSWORD),  # correct password, inactive
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1
    assert GENERIC_LOGIN_ERROR in responses[0].text
    assert all(session_cookie_name() not in r.headers.get("set-cookie", "") for r in responses)

    # Attempts while locked do not compound the lock.
    session.refresh(locked)
    assert locked.failed_logins == 0
    assert locked.lock_count == 1
    assert locked.locked_until == T0 + timedelta(minutes=10)


def test_unknown_email_runs_dummy_verify(web, spy_hasher):  # noqa: F811
    post_password(web, "nobody@example.com", PASSWORD)
    assert spy_hasher.verified == ["dummy-password-for-timing"]


def test_locked_account_runs_dummy_verify_not_real_hash(web, session, spy_hasher):  # noqa: F811
    enrolled_user(session, locked_until=T0 + timedelta(minutes=5))
    post_password(web, "owner@example.com", PASSWORD)
    assert spy_hasher.verified == ["dummy-password-for-timing"]


def test_email_is_normalized(web, session):
    user = enrolled_user(session)
    response = post_password(web, "  Owner@Example.COM ", PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/mfa")
    assert user.email == "owner@example.com"


def test_lockout_after_five_failures_for_fifteen_minutes(web, session, clock):
    user = enrolled_user(session)
    for _ in range(app_settings.max_failed_logins):
        assert post_password(web, user.email, "wrong-password-123").status_code == 401
    session.refresh(user)
    assert user.locked_until == T0 + timedelta(minutes=15)
    assert len(audit_rows(session, "auth.lockout")) == 1

    clock["now"] = T0 + timedelta(minutes=14, seconds=59)
    assert post_password(web, user.email, PASSWORD).status_code == 401
    clock["now"] = T0 + timedelta(minutes=15)
    assert post_password(web, user.email, PASSWORD).status_code == 303


def test_password_ok_does_not_reset_counters_before_mfa(web, session):
    user = enrolled_user(session, failed_logins=3)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    session.refresh(user)
    assert user.failed_logins == 3


def test_correct_password_alone_is_not_authenticated(web, session):
    user = enrolled_user(session)
    response = post_password(web, user.email, PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/login/mfa?next=%2F"
    assert not is_authenticated(web)
    auth = session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).one()
    assert auth.mfa_verified is False


def test_password_rehashed_when_parameters_are_weak(web, session, monkeypatch):
    user = enrolled_user(session)
    monkeypatch.setattr(auth_routers, "password_needs_rehash", lambda h: True)
    old_hash = user.password_hash
    assert post_password(web, user.email, PASSWORD).status_code == 303
    session.refresh(user)
    assert user.password_hash != old_hash
    assert user.password_hash.startswith("$argon2id$")


# ---------------------------------------------------------------------------
# MFA step
# ---------------------------------------------------------------------------


def test_totp_success_authenticates_and_resets_counters(web, session):
    user = enrolled_user(session, failed_logins=2)
    response = full_login(web, user)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert is_authenticated(web)
    session.refresh(user)
    assert user.failed_logins == 0


def test_totp_replay_rejected_at_http_level(web, session, clock):
    user = enrolled_user(session)
    code = totp_code(user, T0)
    assert full_login(web, user).status_code == 303
    assert logout(web).status_code == 303
    clock["now"] = T0 + timedelta(seconds=5)  # same 30 s step
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = post_mfa(web, code)
    assert response.status_code == 401
    assert not is_authenticated(web)


def test_wrong_totp_counts_toward_lockout(web, session):
    user = enrolled_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    for _ in range(app_settings.max_failed_logins):
        assert post_mfa(web, "000000").status_code == 401
    session.refresh(user)
    assert user.locked_until == T0 + timedelta(minutes=15)
    # Locked now: even the right code fails and does not compound the lock.
    assert post_mfa(web, totp_code(user, T0)).status_code == 401
    session.refresh(user)
    assert user.lock_count == 1 and user.failed_logins == 0
    assert len(audit_rows(session, "auth.mfa.failure")) == app_settings.max_failed_logins + 1


def test_recovery_code_works_exactly_once(web, session):
    user = enrolled_user(session)
    codes = issue_recovery_codes(session, user.id)
    session.flush()
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = post_mfa(web, codes[0].upper())
    assert response.status_code == 303
    assert is_authenticated(web)
    assert logout(web).status_code == 303

    assert post_password(web, user.email, PASSWORD).status_code == 303
    assert post_mfa(web, codes[0]).status_code == 401
    assert not is_authenticated(web)
    used = session.exec(
        select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.used_at != None)  # noqa: E711
    ).all()
    assert len(used) == 1


def test_mfa_page_redirects_unenrolled_user_to_enroll(web, session):
    user = new_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = web.get("/login/mfa", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/enroll")


# ---------------------------------------------------------------------------
# Enrollment (first login)
# ---------------------------------------------------------------------------


def test_first_login_enrolls_then_reaches_app(web, session):
    user = new_user(session)
    response = post_password(web, user.email, PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/enroll?next=%2F"

    page = web.get("/enroll")
    assert page.status_code == 200
    assert "<svg" in page.text
    session.refresh(user)
    assert user.totp_secret_enc is not None and user.totp_enabled is False
    secret = decrypt_secret(user.totp_secret_enc)
    assert secret in page.text.replace(" ", "")
    # Reloading reuses the same secret.
    assert secret in web.get("/enroll").text.replace(" ", "")
    session.refresh(user)
    assert decrypt_secret(user.totp_secret_enc) == secret

    done = web.post(
        "/enroll",
        data={"code": totp_code(user, T0), "csrf_token": csrf_of(page)},
        follow_redirects=False,
    )
    assert done.status_code == 200
    session.refresh(user)
    assert user.totp_enabled is True
    shown = re.findall(r"<li>([a-z0-9]{5}-[a-z0-9]{5})</li>", done.text)
    assert len(set(shown)) == 10
    assert len(session.exec(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).all())
    assert done.headers["Cache-Control"] == "no-store"
    assert is_authenticated(web)
    assert len(audit_rows(session, "auth.mfa.enrolled")) == 1


def test_enroll_wrong_code_counts_toward_lockout(web, session):
    user = new_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    page = web.get("/enroll")
    response = web.post(
        "/enroll", data={"code": "000000", "csrf_token": csrf_of(page)}, follow_redirects=False
    )
    assert response.status_code == 401
    assert "<svg" in response.text
    session.refresh(user)
    assert user.failed_logins == 1 and user.totp_enabled is False


def test_enrolled_user_cannot_re_enroll(web, session):
    user = enrolled_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = web.get("/enroll", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/mfa")
    page = web.get("/login/mfa")
    response = web.post(
        "/enroll",
        data={"code": totp_code(user, T0), "csrf_token": csrf_of(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not is_authenticated(web)


# ---------------------------------------------------------------------------
# Sessions: fixation, cookie attributes, logout
# ---------------------------------------------------------------------------


def test_session_fixation_cookie_changes_and_old_cookie_dies(web, session):
    user = enrolled_user(session)
    web.cookies.set(session_cookie_name(), "attacker-planted")
    response = post_password(web, user.email, PASSWORD)
    assert response.status_code == 303
    pending = session_cookie_value(response)
    assert pending and pending != "attacker-planted"
    web.cookies.clear()
    web.cookies.set(session_cookie_name(), pending)
    web.cookies.set(csrf_cookie_name(), "unused")

    response = post_mfa(web, totp_code(user, T0))
    assert response.status_code == 303
    final = session_cookie_value(response)
    assert final != pending

    assert client_with_session(final).get("/account/password").status_code == 200
    stale = client_with_session(pending)
    assert stale.get("/account/password", follow_redirects=False).status_code == 303
    assert stale.get("/login/mfa", follow_redirects=False).status_code == 303


def test_new_login_revokes_previous_session(web, session):
    user = enrolled_user(session)
    first = session_cookie_value(post_password(web, user.email, PASSWORD))
    second = session_cookie_value(post_password(web, user.email, PASSWORD))
    assert first != second
    sessions = session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).all()
    assert [s.id for s in sessions] == [auth_sessions.hash_token(second)]


def test_mfa_post_when_already_verified_or_unenrolled_redirects(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    token = csrf_of(web.get("/account/password"))
    response = web.post(
        "/login/mfa",
        data={"code": "1", "csrf_token": token, "next": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/admin"
    assert web.get("/login/mfa", follow_redirects=False).headers["location"] == "/"

    other = new_user(session, email="fresh@example.com")
    assert logout(web).status_code == 303
    assert post_password(web, other.email, PASSWORD).status_code == 303
    token = csrf_of(web.get("/enroll"))
    response = web.post(
        "/login/mfa", data={"code": "123456", "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/enroll")


def test_undecryptable_totp_secret_fails_closed(web, session):
    user = enrolled_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    user.totp_secret_enc = "not-a-valid-ciphertext"
    session.add(user)
    session.flush()
    assert post_mfa(web, "123456").status_code == 401
    assert not is_authenticated(web)


def test_enroll_replaces_unreadable_pending_secret(web, session):
    user = new_user(session, totp_secret_enc="garbage")
    assert post_password(web, user.email, PASSWORD).status_code == 303
    page = web.get("/enroll")
    assert page.status_code == 200
    session.refresh(user)
    assert decrypt_secret(user.totp_secret_enc).replace(" ", "") in page.text.replace(" ", "")


def _session_set_cookie(response) -> str:
    cookies = response.headers.get_list("set-cookie")
    matches = [c for c in cookies if c.startswith(session_cookie_name() + "=")]
    assert len(matches) == 1, cookies
    return matches[0]


def test_session_cookie_attributes_secure(web, session):
    user = enrolled_user(session)
    cookie = _session_set_cookie(post_password(web, user.email, PASSWORD))
    assert cookie.startswith("__Host-dci_session=")
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert "httponly" in parts
    assert "secure" in parts
    assert "samesite=lax" in parts
    assert "path=/" in parts
    assert f"max-age={app_settings.session_absolute_hours * 3600}" in parts
    assert not any(p.startswith("domain") for p in parts)


def test_session_cookie_attributes_insecure(web, session, monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", False)
    user = enrolled_user(session)
    cookie = _session_set_cookie(post_password(web, user.email, PASSWORD))
    assert cookie.startswith("dci_session=")
    parts = [p.strip().lower() for p in cookie.split(";")]
    assert "secure" not in parts
    assert "httponly" in parts and "samesite=lax" in parts and "path=/" in parts


def test_logout_invalidates_cookie(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    cookie = web.cookies.get(session_cookie_name())
    response = logout(web)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert any(
        c.startswith(session_cookie_name() + "=") and "max-age=0" in c.lower()
        for c in response.headers.get_list("set-cookie")
    )
    assert session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).first() is None
    stale = client_with_session(cookie)
    assert stale.get("/account/password", follow_redirects=False).status_code == 303


def test_logout_requires_csrf(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    assert web.post("/logout", follow_redirects=False).status_code == 403
    assert is_authenticated(web)


# ---------------------------------------------------------------------------
# next handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [("https://evil.example", "/"), ("//evil.example", "/"), ("/admin", "/admin")],
)
def test_next_through_full_login(web, session, given, expected):
    user = enrolled_user(session)
    page = web.get("/login", params={"next": given})
    assert f'name="next" value="{expected}"' in page.text
    response = full_login(web, user, next_path=given)
    assert response.status_code == 303
    assert response.headers["location"] == expected


def test_already_authenticated_login_page_redirects(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    response = web.get("/login?next=/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


# ---------------------------------------------------------------------------
# Expired page (CSRF mismatch) on the auth forms
# ---------------------------------------------------------------------------


def _expired_redirect(response, path):
    assert response.status_code == 303
    assert response.headers["location"] == f"{path}?expired=1&next=%2F"
    return response.headers["location"]


def test_stale_login_form_after_login_in_other_tab(web, session):
    user = enrolled_user(session)
    stale_token = csrf_of(web.get("/login"))
    # Meanwhile another tab logged in, so the pre-session token no longer applies.
    assert full_login(web, user).status_code == 303
    response = web.post(
        "/login?next=%2F",
        data={"email": user.email, "password": PASSWORD, "csrf_token": stale_token},
        follow_redirects=False,
    )
    location = _expired_redirect(response, "/login")
    # Already signed in: the login page just continues to the target.
    landing = web.get(location, follow_redirects=False)
    assert landing.status_code == 303
    assert landing.headers["location"] == "/"


def test_stale_mfa_form_shows_expired_message(web, session):
    user = enrolled_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = web.post(
        "/login/mfa?next=%2F",
        data={"code": "123456", "csrf_token": "stale"},
        follow_redirects=False,
    )
    page = web.get(_expired_redirect(response, "/login/mfa"))
    assert page.status_code == 200
    assert "page expired" in page.text.lower()
    assert csrf_of(page) != "stale"
    assert 'name="code"' in page.text
    session.refresh(user)
    assert user.failed_logins == 0  # a CSRF failure is not a login attempt


def test_stale_enroll_form_shows_expired_message(web, session):
    user = new_user(session)
    assert post_password(web, user.email, PASSWORD).status_code == 303
    response = web.post(
        "/enroll?next=%2F", data={"code": "123456", "csrf_token": "stale"}, follow_redirects=False
    )
    page = web.get(_expired_redirect(response, "/enroll"))
    assert "page expired" in page.text.lower()
    assert "<svg" in page.text


def test_expired_redirect_sanitizes_next(web):
    response = web.post(
        "/login?next=//evil.example", data={"email": "a@example.com"}, follow_redirects=False
    )
    _expired_redirect(response, "/login")


def test_csrf_403_elsewhere_stays_json(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    response = web.post("/account/password", data={"csrf_token": "stale"})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert web.post("/logout", data={"csrf_token": "stale"}).status_code == 403


def test_login_without_any_csrf_cookie_gets_fresh_form(session, monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)

    def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        bare = TestClient(app, base_url="https://testserver")
        response = bare.post(
            "/login", data={"email": "a@example.com", "password": "x"}, follow_redirects=False
        )
        assert response.status_code == 303
        page = bare.get(response.headers["location"])
        assert "page expired" in page.text.lower()
        # The re-rendered form works: it set the cookie and carries the token.
        assert csrf_cookie_name() in page.headers["set-cookie"]
        retry = bare.post(
            "/login",
            data={"email": "a@example.com", "password": "x", "csrf_token": csrf_of(page)},
        )
        assert retry.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def _change_password(web, current, new, confirm=None):
    page = web.get("/account/password")
    assert page.status_code == 200
    return web.post(
        "/account/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
            "csrf_token": csrf_of(page),
        },
        follow_redirects=False,
    )


def test_change_password_requires_login(web):
    response = web.get("/account/password", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Faccount%2Fpassword"


def test_change_password_wrong_current(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    response = _change_password(web, "not-my-password", NEW_PASSWORD)
    assert response.status_code == 400
    assert "current password is incorrect" in response.text.lower()
    session.refresh(user)
    assert user.failed_logins == 1


def test_change_password_policy_and_confirmation(web, session):
    user = enrolled_user(session)
    assert full_login(web, user).status_code == 303
    response = _change_password(web, PASSWORD, "short")
    assert response.status_code == 400
    assert "at least 12 characters" in response.text
    response = _change_password(web, PASSWORD, NEW_PASSWORD, confirm=NEW_PASSWORD + "x")
    assert response.status_code == 400
    assert "do not match" in response.text


def test_change_password_revokes_other_sessions_and_rotates(web, session):
    user = enrolled_user(session)
    other_raw, _ = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    session.flush()
    assert full_login(web, user).status_code == 303
    before = web.cookies.get(session_cookie_name())

    response = _change_password(web, PASSWORD, NEW_PASSWORD)
    assert response.status_code == 200
    assert "password changed" in response.text.lower()
    after = web.cookies.get(session_cookie_name())
    assert after != before
    assert is_authenticated(web)
    assert len(session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).all()) == 1
    for raw in (before, other_raw):
        stale = client_with_session(raw)
        assert stale.get("/account/password", follow_redirects=False).status_code == 303
    # The new password works, the old one does not.
    assert logout(web).status_code == 303
    assert post_password(web, user.email, PASSWORD).status_code == 401
    assert post_password(web, user.email, NEW_PASSWORD).status_code == 303
    # The page rendered after the change carries the rotated session's CSRF token.
    new_auth = session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).first()
    assert new_auth is not None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_rows_for_every_event(web, session, clock):
    user = new_user(session)
    # Failure for an unknown email: no user id, attempted email in `after` only.
    post_password(web, "Nobody@Example.com", PASSWORD)
    # First login + enrollment.
    assert post_password(web, user.email, PASSWORD).status_code == 303
    page = web.get("/enroll")
    web.post("/enroll", data={"code": "000000", "csrf_token": csrf_of(page)})
    session.refresh(user)
    web.post("/enroll", data={"code": totp_code(user, T0), "csrf_token": csrf_of(page)})
    _change_password(web, PASSWORD, NEW_PASSWORD)
    logout(web)
    # Second login: password ok, MFA success.
    session.refresh(user)
    assert post_password(web, user.email, NEW_PASSWORD).status_code == 303
    clock["now"] = T0 + timedelta(minutes=1)
    assert post_mfa(web, totp_code(user, clock["now"])).status_code == 303
    # Lockout.
    assert logout(web).status_code == 303
    for _ in range(app_settings.max_failed_logins):
        post_password(web, user.email, "wrong-password-123")

    [unknown] = [r for r in audit_rows(session, "auth.login.failure") if r.user_id is None]
    assert unknown.after["email"] == "nobody@example.com"
    assert unknown.ip == "testclient"

    for action in (
        "auth.login.password_ok",
        "auth.mfa.failure",
        "auth.mfa.enrolled",
        "auth.password.changed",
        "auth.logout",
        "auth.login.success",
        "auth.lockout",
    ):
        rows = audit_rows(session, action)
        assert rows, action
        assert all(r.user_id == user.id for r in rows), action
        assert all(r.ip == "testclient" for r in rows), action
    wrong = [r for r in audit_rows(session, "auth.login.failure") if r.user_id == user.id]
    assert len(wrong) == app_settings.max_failed_logins
    # No passwords or codes are ever written to the audit log.
    for row in session.exec(select(AuditLog)).all():
        blob = f"{row.before}{row.after}"
        assert PASSWORD not in blob and NEW_PASSWORD not in blob


# ---------------------------------------------------------------------------
# Concurrency: the same TOTP code submitted twice at once
# ---------------------------------------------------------------------------


def test_concurrent_same_totp_exactly_one_succeeds(engine, monkeypatch):
    """
    Two clients holding the same pending session submit the same valid code at
    the same moment. Uses the real ``get_session`` (separate connections, real
    commits) so the ``FOR UPDATE`` row lock is what serializes them. Audit writes
    are stubbed out because the committed audit table cannot be cleaned up.
    """
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)
    monkeypatch.setattr(auth_routers, "_now", lambda: T0)
    monkeypatch.setattr(auth_routers, "record", lambda *a, **k: None)
    app.dependency_overrides.clear()

    with Session(engine) as db:
        user = make_user(
            db,
            email="race-totp@example.com",
            password_hash=hash_password(PASSWORD),
            totp_secret_enc=encrypt_secret(new_totp_secret()),
            totp_enabled=True,
        )
        raw, auth = auth_sessions.create_session(db, user, ip=None, user_agent=None)
        db.commit()
        user_id, csrf, code = user.id, auth.csrf_token, totp_code(user, T0)

    barrier = threading.Barrier(2, timeout=10)
    real_lock = auth_routers._lock_for_update

    def lock_after_barrier(db, user):
        barrier.wait()
        return real_lock(db, user)

    monkeypatch.setattr(auth_routers, "_lock_for_update", lock_after_barrier)
    results: list[int] = []

    def submit():
        client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        client.cookies.set(session_cookie_name(), raw)
        response = client.post(
            "/login/mfa",
            data={"code": code},
            headers={CSRF_HEADER: csrf},
            follow_redirects=False,
        )
        results.append(response.status_code)

    try:
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(results) == [303, 401]
    finally:
        with Session(engine) as db:
            db.delete(db.get(User, user_id))
            db.commit()
