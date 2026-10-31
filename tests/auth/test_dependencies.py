"""
Tests for the auth dependencies: current_user / pending_session_user (401 vs 303),
safe_next_path and CSRF verification. Uses a fresh app with a small test-only
router so the real routers stay open until Task 10 adds role checks.
"""

from datetime import timedelta

import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlmodel import select

from payroll import app_settings
from payroll.app import create_app
from payroll.auth import dependencies as deps
from payroll.auth.dependencies import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER,
    current_user,
    new_presession_csrf_token,
    pending_session_user,
    safe_next_path,
)
from payroll.auth.models import AuthSession, User
from payroll.common.dependencies import get_session
from tests.conftest import login_as, make_user

PREFIX = app_settings.base_url_prefix

_router = APIRouter()


@_router.get(f"{PREFIX}/_t/me")
def api_me(user: User = Depends(current_user)) -> dict:
    return {"email": user.email}


@_router.get("/_t/page", response_class=HTMLResponse)
def page(user: User = Depends(current_user)) -> str:
    return f"<p>hello {user.email}</p>"


@_router.get("/_t/pending")
def pending(user: User = Depends(pending_session_user)) -> dict:
    return {"email": user.email}


@_router.post("/_t/echo")
async def echo(request: Request) -> dict:
    form = await request.form()
    return {"ok": True, "fields": sorted(form.keys())}


@_router.post(f"{PREFIX}/_t/echo")
def api_echo() -> dict:
    return {"ok": True}


@_router.put("/_t/echo")
@_router.patch("/_t/echo")
@_router.delete("/_t/echo")
def other_unsafe() -> dict:
    return {"ok": True}


@_router.get("/_t/csrf-page", response_class=HTMLResponse)
def csrf_page(request: Request) -> str:
    return f"<p>{deps.csrf_token_for(request)}</p>"


@pytest.fixture(name="tapp")
def tapp_fixture(session):
    application = create_app()
    application.include_router(_router)

    def override_get_session():
        yield session

    application.dependency_overrides[get_session] = override_get_session
    return application


@pytest.fixture(name="tclient")
def tclient_fixture(tapp):
    """A client with no CSRF defaults and no cookies."""
    return TestClient(tapp, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# current_user: 401 vs 303
# ---------------------------------------------------------------------------


def test_api_path_without_session_is_401_json(tclient):
    response = tclient.get(f"{PREFIX}/_t/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_htmx_request_without_session_is_401(tclient):
    response = tclient.get("/_t/page", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["HX-Redirect"].startswith("/login")


def test_html_page_without_session_redirects_to_login_with_next(tclient):
    response = tclient.get("/_t/page?x=1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F_t%2Fpage%3Fx%3D1"


def test_authenticated_session_reaches_route(tclient, session):
    user = make_user(session, email="a@example.com")
    login_as(tclient, user)
    assert tclient.get(f"{PREFIX}/_t/me").json() == {"email": "a@example.com"}
    assert "hello a@example.com" in tclient.get("/_t/page").text


def test_unknown_cookie_is_unauthenticated(tclient):
    tclient.cookies.set(app_settings.session_cookie_name, "garbage")
    assert tclient.get(f"{PREFIX}/_t/me").status_code == 401


def test_pending_mfa_session_is_unauthenticated_except_pending_routes(tclient, session):
    user = make_user(session)
    login_as(tclient, user, mfa_verified=False)
    assert tclient.get(f"{PREFIX}/_t/me").status_code == 401
    assert tclient.get("/_t/page", follow_redirects=False).status_code == 303
    response = tclient.get("/_t/pending")
    assert response.status_code == 200
    assert response.json() == {"email": user.email}


def test_pending_route_requires_some_session(tclient):
    assert tclient.get("/_t/pending", follow_redirects=False).status_code == 303


def test_inactive_user_rejected(tclient, session):
    user = make_user(session)
    login_as(tclient, user)
    user.is_active = False
    session.add(user)
    session.flush()
    assert tclient.get(f"{PREFIX}/_t/me").status_code == 401
    assert tclient.get("/_t/pending", follow_redirects=False).status_code == 303


def test_expired_session_is_unauthenticated(tclient, session):
    user = make_user(session)
    login_as(tclient, user)
    auth = session.exec(select(AuthSession).where(AuthSession.user_id == user.id)).one()
    auth.last_seen_at -= timedelta(minutes=31)
    session.add(auth)
    session.flush()
    assert tclient.get(f"{PREFIX}/_t/me").status_code == 401


# ---------------------------------------------------------------------------
# safe_next_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ok?x=1", "/ok?x=1"),
        ("/", "/"),
        ("/admin", "/admin"),
        ("/a/b#frag", "/a/b#frag"),
        ("/%2F%2Fevil.example", "/%2F%2Fevil.example"),  # encoded: still a local path
        ("https://evil.example", "/"),
        ("http://evil.example/x", "/"),
        ("//evil.example", "/"),
        ("//evil.example/path", "/"),
        ("///evil.example", "/"),
        ("/\\evil", "/"),
        ("\\\\evil", "/"),
        ("/\\/evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("JavaScript:alert(1)", "/"),
        ("data:text/html,x", "/"),
        ("evil.example", "/"),
        ("relative/path", "/"),
        ("", "/"),
        (None, "/"),
        (" /admin", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("/\r\nSet-Cookie:x=1", "/"),
        ("/\x00", "/"),
        ("/\x7f", "/"),
        ("/" + "a" * 3000, "/"),
    ],
)
def test_safe_next_path(value, expected):
    assert safe_next_path(value) == expected


def test_safe_next_path_custom_default():
    assert safe_next_path("//evil", default="/home") == "/home"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_safe_methods_exempt(tclient):
    assert tclient.get("/_t/csrf-page").status_code == 200
    assert tclient.head("/_t/csrf-page").status_code in (200, 405)
    assert tclient.options("/_t/echo").status_code != 403


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_unsafe_methods_without_token_rejected(tclient, method):
    response = tclient.request(method.upper(), "/_t/echo")
    assert response.status_code == 403


def test_api_post_without_token_rejected(tclient):
    assert tclient.post(f"{PREFIX}/_t/echo", json={}).status_code == 403


def test_session_csrf_header_right_and_wrong(tclient, session):
    user = make_user(session)
    token = login_as(tclient, user)
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: token}).status_code == 200
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: token + "x"}).status_code == 403
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: ""}).status_code == 403
    for method in ("PUT", "PATCH", "DELETE"):
        assert tclient.request(method, "/_t/echo", headers={CSRF_HEADER: token}).status_code == 200


def test_session_csrf_form_field(tclient, session):
    user = make_user(session)
    token = login_as(tclient, user)
    del tclient.headers[CSRF_HEADER]
    ok = tclient.post("/_t/echo", data={"csrf_token": token, "a": "1"})
    assert ok.status_code == 200
    assert ok.json()["fields"] == ["a", "csrf_token"]  # handler can still read the form
    assert tclient.post("/_t/echo", data={"csrf_token": "nope"}).status_code == 403
    assert tclient.post("/_t/echo", data={"a": "1"}).status_code == 403


def test_session_csrf_multipart_form_field(tclient, session):
    user = make_user(session)
    token = login_as(tclient, user)
    del tclient.headers[CSRF_HEADER]
    response = tclient.post(
        "/_t/echo", data={"csrf_token": token}, files={"f": ("a.txt", b"x", "text/plain")}
    )
    assert response.status_code == 200


def test_session_token_required_even_with_valid_presession_cookie(tclient, session):
    """With a live session the double-submit cookie is not an alternative."""
    user = make_user(session)
    login_as(tclient, user)
    pre = new_presession_csrf_token()
    tclient.cookies.set(CSRF_COOKIE_NAME, pre)
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: pre}).status_code == 403


def test_presession_double_submit(tclient):
    token = new_presession_csrf_token()
    tclient.cookies.set(CSRF_COOKIE_NAME, token)
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: token}).status_code == 200
    assert tclient.post("/_t/echo", data={"csrf_token": token}).status_code == 200
    # Header must match the cookie.
    other = new_presession_csrf_token()
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: other}).status_code == 403
    # No cookie at all.
    tclient.cookies.clear()
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: token}).status_code == 403


def test_presession_cookie_must_be_signed(tclient):
    forged = "attacker-chosen.0000"
    tclient.cookies.set(CSRF_COOKIE_NAME, forged)
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: forged}).status_code == 403
    unsigned = "plainvalue"
    tclient.cookies.set(CSRF_COOKIE_NAME, unsigned)
    assert tclient.post("/_t/echo", headers={CSRF_HEADER: unsigned}).status_code == 403


def test_page_without_session_issues_presession_cookie(tapp, monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", False)
    client = TestClient(tapp)
    response = client.get("/_t/csrf-page")
    token = response.text.removeprefix("<p>").removesuffix("</p>")
    assert token
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{CSRF_COOKIE_NAME}=")
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    # The browser now has the cookie; the rendered token passes the double-submit.
    assert client.post("/_t/echo", headers={CSRF_HEADER: token}).status_code == 200
    # A second page view reuses the same cookie instead of minting a new one.
    again = client.get("/_t/csrf-page")
    assert "set-cookie" not in again.headers
    assert token in again.text


def test_presession_cookie_secure_flag_follows_setting(tapp, monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)
    response = TestClient(tapp).get("/_t/csrf-page")
    assert "secure" in response.headers["set-cookie"].lower()


def test_csrf_token_for_uses_session_token(tclient, session):
    user = make_user(session)
    token = login_as(tclient, user)
    response = tclient.get("/_t/csrf-page")
    assert response.text == f"<p>{token}</p>"
    assert "set-cookie" not in response.headers
