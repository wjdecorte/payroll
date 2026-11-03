"""
Hardening items carried from the Task 9 part A review: CSRF short-circuit, 500
security headers, rotate/create_session details, bulk delete of expired
sessions, the user-row lock guard, ``__Host-`` cookie names, and bare-client
CSRF checks on the real routes.
"""

import warnings
from datetime import timedelta

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from payroll import app_settings
from payroll.app import app, create_app
from payroll.auth import dependencies as deps
from payroll.auth import sessions as auth_sessions
from payroll.auth.models import AuthSession, User, utcnow
from payroll.common.dependencies import get_session
from tests.auth.test_headers import _assert_common
from tests.conftest import make_user

PREFIX = app_settings.base_url_prefix


# ---------------------------------------------------------------------------
# (a) verify_csrf: no expected token -> 403 before the body is parsed
# ---------------------------------------------------------------------------


def test_verify_csrf_rejects_before_reading_body_without_expected_token(monkeypatch):
    calls = []

    async def spy(request):
        calls.append(request)
        return "whatever"

    monkeypatch.setattr(deps, "_supplied_token", spy)
    bare = TestClient(app, raise_server_exceptions=False)
    response = bare.post("/htmx/calculate", data={"csrf_token": "x" * 10})
    assert response.status_code == 403
    assert calls == []


# ---------------------------------------------------------------------------
# (b) safe_next_path: only printable ASCII
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/∕evil.example",  # DIVISION SLASH
        "/／evil.example",  # FULLWIDTH SOLIDUS
        "/\u0085evil",  # NEL
        "/ evil",  # LINE SEPARATOR
        "/　evil",  # IDEOGRAPHIC SPACE
        "/café",
    ],
)
def test_safe_next_path_rejects_non_ascii(value):
    assert deps.safe_next_path(value) == "/"


def test_safe_next_path_keeps_printable_ascii():
    value = "/a-b_c.d~e?x=1&y=%2F#frag !$'()*+,;="
    assert deps.safe_next_path(value) == value


# ---------------------------------------------------------------------------
# (c) 500 responses carry the security headers
# ---------------------------------------------------------------------------


def test_unhandled_500_has_security_headers(session):
    application = create_app()
    router = APIRouter()

    @router.get("/_t/boom")
    def boom() -> dict:
        raise RuntimeError("boom")

    application.include_router(router)
    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/_t/boom")
    assert response.status_code == 500
    _assert_common(response)
    assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# (d) rotate_session keeps expires_at unless asked not to; missing user
# ---------------------------------------------------------------------------


def test_rotate_session_keeps_expiry_by_default(session):
    user = make_user(session)
    _, old = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    old.expires_at = utcnow() + timedelta(hours=1)
    session.flush()
    expiry = old.expires_at
    _, new = auth_sessions.rotate_session(session, old, ip=None, user_agent=None)
    assert new.expires_at == expiry


def test_rotate_session_fresh_expiry_for_new_login(session):
    user = make_user(session)
    _, old = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    old.expires_at = utcnow() + timedelta(hours=1)
    session.flush()
    _, new = auth_sessions.rotate_session(session, old, ip=None, user_agent=None, keep_expiry=False)
    assert new.expires_at > utcnow() + timedelta(hours=app_settings.session_absolute_hours - 1)


def test_rotate_session_missing_user_raises_cleanly(session):
    user = make_user(session)
    _, old = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    session.flush()
    old_id = old.id
    # A session whose user row is gone (simulated without the FK cascade).
    session.expunge(old)
    ghost = AuthSession(**{**old.model_dump(), "user_id": 999_999_999})
    with pytest.raises(auth_sessions.SessionUserMissingError):
        auth_sessions.rotate_session(session, ghost, ip=None, user_agent=None)
    assert session.exec(select(AuthSession).where(AuthSession.id == old_id)).first() is not None


# ---------------------------------------------------------------------------
# (e) create_session truncates ip
# ---------------------------------------------------------------------------


def test_create_session_truncates_ip(session):
    user = make_user(session)
    _, auth = auth_sessions.create_session(session, user, ip="1" * 100, user_agent=None)
    session.flush()
    assert auth.ip == "1" * 45


# ---------------------------------------------------------------------------
# (f) bare clients cannot POST to the real routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/htmx/calculate", {"data": {"grossSalary": "1"}}),
        (f"{PREFIX}/payroll/runs/calculate", {"json": {}}),
    ],
)
def test_bare_client_post_is_403(path, kwargs):
    response = TestClient(app, raise_server_exceptions=False).post(path, **kwargs)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# (g) load_session deletes expired rows with a bulk DELETE
# ---------------------------------------------------------------------------


def test_load_session_expired_delete_does_not_warn_when_row_already_gone(session, monkeypatch):
    user = make_user(session)
    raw, auth = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    session.flush()
    sid = auth.id
    real = auth_sessions._is_expired

    def expired_and_gone(row, now):
        # A parallel request deleted the same expired row first.
        session.execute(text("DELETE FROM auth_session WHERE id = :id"), {"id": sid})
        real(row, now)
        return True

    monkeypatch.setattr(auth_sessions, "_is_expired", expired_and_gone)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert auth_sessions.load_session(session, raw) is None
        session.flush()
    assert session.exec(select(AuthSession).where(AuthSession.id == sid)).first() is None


def test_load_session_expired_row_is_deleted(session):
    user = make_user(session)
    raw, auth = auth_sessions.create_session(session, user, ip=None, user_agent=None)
    auth.last_seen_at -= timedelta(minutes=31)
    session.flush()
    sid = auth.id
    assert auth_sessions.load_session(session, raw) is None
    session.expire_all()
    assert session.exec(select(AuthSession).where(AuthSession.id == sid)).first() is None


# ---------------------------------------------------------------------------
# (h) the row lock refuses a user from another db session
# ---------------------------------------------------------------------------


def test_lock_user_row_requires_attached_user(session, engine):
    user = make_user(session)
    detached = User(**user.model_dump())
    with pytest.raises(ValueError):
        auth_sessions.lock_user_row(session, detached)
    with Session(engine) as other, pytest.raises(ValueError):
        auth_sessions.lock_user_row(other, user)
    assert auth_sessions.lock_user_row(session, user) is user


# ---------------------------------------------------------------------------
# __Host- cookie names
# ---------------------------------------------------------------------------


def test_cookie_names_follow_secure_setting(monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)
    assert deps.session_cookie_name() == f"__Host-{app_settings.session_cookie_name}"
    assert deps.csrf_cookie_name() == "__Host-dci_csrf"
    monkeypatch.setattr(app_settings, "session_cookie_secure", False)
    assert deps.session_cookie_name() == app_settings.session_cookie_name
    assert deps.csrf_cookie_name() == "dci_csrf"


@pytest.mark.parametrize("secure", [True, False])
def test_presession_cookie_name_and_attributes(session, monkeypatch, secure):
    monkeypatch.setattr(app_settings, "session_cookie_secure", secure)
    application = create_app()
    application.dependency_overrides[get_session] = lambda: session
    response = TestClient(application, base_url="https://testserver").get("/login")
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    name = "__Host-dci_csrf" if secure else "dci_csrf"
    assert set_cookie.startswith(f"{name}=")
    lowered = set_cookie.lower()
    assert "path=/" in lowered
    assert "domain" not in lowered
    assert ("secure" in lowered) is secure
