"""
Authorization on every route (SPEC SEC1, SEC7).

``test_every_route_requires_auth`` enumerates ``app.routes`` (and the routes of
an app built with ``debug_mode`` on, which adds the docs pages), so a route added
later without protection fails it. The only routes an anonymous visitor may use
are the login form, ``/healthcheck`` and the ``/static`` mount (all under
``PUBLIC_PATHS``).

Ordering of the checks on an unsafe request: the app-wide ``verify_csrf``
dependency runs BEFORE any route's auth dependency, so an anonymous POST/PUT/
DELETE without a valid CSRF pair gets 403 (CSRF), and with one gets 401/303
(auth). Both outcomes refuse the request; neither reveals anything.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from starlette.routing import Mount, Route

from payroll import app_settings
from payroll.app import app
from payroll.audit.models import AuditLog
from payroll.auth.dependencies import UNSAFE_METHODS
from payroll.auth.permissions import PUBLIC_PATHS, ROLES, is_public_path, require_role
from payroll.payroll_run.models import PayrollRun
from tests.conftest import add_presession_csrf

PREFIX = app_settings.base_url_prefix
API = f"{PREFIX}/payroll"

# The only endpoints an anonymous visitor may use. Each must lie under PUBLIC_PATHS.
ANONYMOUS_ENDPOINTS = frozenset(
    {("GET", "/login"), ("POST", "/login"), ("GET", f"{PREFIX}/healthcheck")}
)
PUBLIC_MOUNTS = frozenset({"/static"})
# Unsafe endpoints any signed-in (or pending-session) user may call, whatever the
# role. Exact (method, path) matches; every other unsafe route is owner-only.
ROLE_FREE_UNSAFE_ENDPOINTS = frozenset(
    {
        ("POST", "/logout"),
        ("POST", "/account/password"),
        ("POST", "/login"),
        ("POST", "/login/mfa"),
        ("POST", "/enroll"),
    }
)
MIN_OWNER_ONLY_UNSAFE_ENDPOINTS = 26
# The ledger (M1) mutating routes: all owner-only. The sweep test below checks this
# list against the app's real routes, so a new ledger route cannot be left out.
LEDGER = f"{PREFIX}/ledger"
LEDGER_UNSAFE_ENDPOINTS = (
    ("POST", f"{LEDGER}/accounts"),
    ("PATCH", f"{LEDGER}/accounts/1"),
    ("POST", f"{LEDGER}/accounts/1/deactivate"),
    ("POST", f"{LEDGER}/accounts/1/activate"),
    ("PUT", f"{LEDGER}/system-roles/1"),
    ("POST", f"{LEDGER}/journal-entries"),
    ("POST", f"{LEDGER}/journal-entries/1/reverse"),
    ("POST", f"{LEDGER}/journal-entries/1/void"),
    ("PUT", f"{LEDGER}/period-lock"),
    ("POST", "/accounts/new"),
    ("POST", "/accounts/import"),
    ("POST", "/accounts/import/confirm"),
    ("POST", "/accounts/1/edit"),
    ("POST", "/accounts/1/active"),
    ("POST", "/settings/ledger/roles"),
    ("POST", "/settings/ledger/lock"),
    ("POST", "/journal"),
    ("POST", "/journal/1/reverse"),
    ("POST", "/journal/1/void"),
)
# The receivables (M2) mutating page routes: all owner-only.
RECEIVABLES_UNSAFE_ENDPOINTS = (
    ("POST", "/customers/new"),
    ("POST", "/customers/1/edit"),
    ("POST", "/customers/1/active"),
    ("POST", "/items/new"),
    ("POST", "/items/1/edit"),
    ("POST", "/items/1/active"),
    ("POST", "/invoices/new"),
    ("POST", "/invoices/1/edit"),
    ("POST", "/invoices/1/discard"),
    ("POST", "/invoices/1/issue"),
    ("POST", "/invoices/1/void"),
    ("POST", "/payments/new"),
    ("POST", "/payments/1/apply"),
    ("POST", "/payments/1/void"),
    ("POST", "/deposits/new"),
    ("POST", "/deposits/1/void"),
    ("POST", "/settings/receivables/number"),
    ("POST", "/settings/receivables/company"),
)
DOCS_PATHS = (f"{PREFIX}/docs", f"{PREFIX}/redoc", f"{PREFIX}/openapi.json")

_PATH_PARAM = re.compile(r"\{[^}]+\}")

RUN_INPUT = {
    "payPeriod": "2026-01",
    "grossSalary": "8000.00",
    "healthInsurance": "500.00",
    "hsaContribution": "300.00",
    "healthInIncomeTax": True,
    "hsaInIncomeTax": False,
    "ytdFicaWages": "0.00",
    "saveRun": True,
}
HTMX_FORM = {
    "pay_period": "2026-02",
    "gross_salary": "8000",
    "health_insurance": "0",
    "hsa_contribution": "0",
    "ytd_fica_wages": "0",
}
HX = {"HX-Request": "true"}


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _endpoints(application) -> list[tuple[str, str]]:
    """(method, concrete path) for every route; path params get a dummy value."""
    found = []
    for route in application.routes:
        if isinstance(route, Mount):
            continue
        assert isinstance(route, Route), f"unexpected route type {route!r}: add it to this test"
        # A Route without methods (an ASGI-app endpoint) accepts every method:
        # it must not be silently skipped.
        assert route.methods is not None, f"route {route.path} accepts any method"
        path = _PATH_PARAM.sub("1", route.path)
        for method in sorted(route.methods - {"HEAD"}):
            found.append((method, path))
    return found


def _mounts(application) -> list[str]:
    return [route.path for route in application.routes if isinstance(route, Mount)]


def _is_api_or_htmx(path: str) -> bool:
    return path.startswith(PREFIX + "/") or path.startswith("/htmx/")


def _assert_refused_unauthenticated(response, method: str, path: str) -> None:
    where = f"{method} {path}"
    if _is_api_or_htmx(path):
        assert response.status_code == 401, f"{where} -> {response.status_code}"
    else:
        assert response.status_code == 303, f"{where} -> {response.status_code}"
        assert response.headers["location"].startswith("/login"), where


@pytest.fixture(params=["default", "debug"])
def any_app(request, db_override):
    """The production-configured app and a debug-mode app (with docs)."""
    if request.param == "default":
        return app
    return request.getfixturevalue("debug_app")


def _saved_run_id(owner_client) -> int:
    response = owner_client.post(f"{API}/runs/calculate", json=RUN_INPUT)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# Every route needs a signed-in user
# ---------------------------------------------------------------------------


def test_public_paths_constant():
    assert PUBLIC_PATHS == ("/login", "/healthcheck", "/static")
    assert ROLES == ("owner", "accountant", "viewer")
    assert is_public_path("/login")
    assert is_public_path(f"{PREFIX}/healthcheck")
    assert is_public_path("/static/favicon-32.png")
    assert not is_public_path("/healthcheck")  # only under the API prefix
    assert not is_public_path("/loginx")
    assert not is_public_path("/static-evil")
    assert not is_public_path(f"{PREFIX}/info")
    assert not is_public_path("/")


def test_every_route_requires_auth(any_app):
    endpoints = _endpoints(any_app)
    # Not vacuous: the enumeration sees the routes we know about.
    assert len(endpoints) > 20
    for known in [
        ("GET", "/"),
        ("GET", "/admin"),
        ("POST", "/admin"),
        ("POST", "/htmx/calculate"),
        ("DELETE", "/htmx/runs/1"),
        ("GET", f"{PREFIX}/info"),
        ("POST", f"{API}/runs/calculate"),
        ("DELETE", f"{API}/runs/1"),
        ("PUT", f"{API}/config"),
        ("GET", "/account/password"),
        ("GET", "/reports/profit-and-loss"),
        ("GET", f"{LEDGER}/reports/balance-sheet.pdf"),
        *LEDGER_UNSAFE_ENDPOINTS,
        *RECEIVABLES_UNSAFE_ENDPOINTS,
        *ANONYMOUS_ENDPOINTS,
    ]:
        assert known in endpoints, known
    if any_app is not app:
        for path in DOCS_PATHS:
            assert ("GET", path) in endpoints, path

    checked = 0
    with TestClient(any_app, raise_server_exceptions=False) as anon:
        add_presession_csrf(anon)  # so unsafe requests are refused by auth, not CSRF
        for method, path in endpoints:
            if (method, path) in ANONYMOUS_ENDPOINTS:
                assert is_public_path(path), (method, path)
                continue
            headers = HX if path.startswith("/htmx/") else {}
            response = anon.request(method, path, headers=headers, follow_redirects=False)
            _assert_refused_unauthenticated(response, method, path)
            checked += 1

        # Anonymous endpoints really are reachable.
        assert anon.get("/login").status_code == 200
        assert anon.get(f"{PREFIX}/healthcheck").status_code == 200

        # Mounted apps bypass FastAPI dependencies: only public mounts may exist.
        mounts = _mounts(any_app)
        assert mounts, "expected the /static mount"
        for mount in mounts:
            assert mount in PUBLIC_MOUNTS and is_public_path(mount), mount
        assert anon.get("/static/favicon-32.png").status_code == 200
    assert checked > 15


def test_unsafe_requests_without_csrf_are_refused_before_auth(any_app):
    """No CSRF pair at all: the CSRF check (app-wide, runs first) refuses with 403."""
    endpoints = [e for e in _endpoints(any_app) if e[0] in UNSAFE_METHODS]
    assert ("DELETE", f"{API}/runs/1") in endpoints
    with TestClient(any_app, raise_server_exceptions=False) as bare:
        for method, path in endpoints:
            if (method, path) in ANONYMOUS_ENDPOINTS:
                continue
            response = bare.request(method, path, follow_redirects=False)
            where = f"{method} {path} -> {response.status_code}"
            if _is_api_or_htmx(path) or path in ("/admin", "/account/password"):
                assert response.status_code == 403, where
            else:
                # /logout and the MFA/enroll forms turn a CSRF failure into a
                # redirect back to /login or the (expired) form; never a 2xx.
                assert response.status_code in (403, 303), where
                if response.status_code == 303:
                    assert response.headers["location"].startswith(("/login", "/enroll")), where


def _assert_unsafe_routes_owner_only(application, client) -> int:
    """
    Every POST/PUT/PATCH/DELETE route of ``application``, sent by ``client`` (a
    signed-in non-owner with its CSRF token), must be refused with the role 403,
    except the exact ROLE_FREE_UNSAFE_ENDPOINTS. Returns how many were checked.
    """
    unsafe = [e for e in _endpoints(application) if e[0] in UNSAFE_METHODS]
    for allowed in ROLE_FREE_UNSAFE_ENDPOINTS:
        assert allowed in unsafe, f"stale allow-list entry {allowed}"
    checked = 0
    for method, path in unsafe:
        if (method, path) in ROLE_FREE_UNSAFE_ENDPOINTS:
            continue
        headers = HX if path.startswith("/htmx/") else {}
        response = client.request(method, path, headers=headers, follow_redirects=False)
        where = f"{method} {path} -> {response.status_code}"
        assert response.status_code == 403, where
        assert response.json()["errors"][0]["type"] == "PermissionDeniedError", where
        checked += 1
    return checked


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_every_unsafe_route_is_owner_only(request, role_client):
    client = request.getfixturevalue(role_client)
    checked = _assert_unsafe_routes_owner_only(app, client)
    assert checked >= MIN_OWNER_ONLY_UNSAFE_ENDPOINTS


def _pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except OSError:  # WeasyPrint's native libraries (Pango) are not installed
        return False
    return True


def _ledger_unsafe_routes() -> set[tuple[str, str]]:
    ledger_paths = ("/accounts", "/settings/ledger", "/journal", f"{LEDGER}/")
    return {
        (method, path)
        for method, path in _endpoints(app)
        if method in UNSAFE_METHODS and path.startswith(ledger_paths)
    }


def _readable_ledger_pages() -> list[str]:
    """Every parameterless ledger/report GET page an accountant or viewer may open."""
    found = []
    for method, path in _endpoints(app):
        if method != "GET" or path.endswith(("/1", "/1/edit", "/1/register", "/new")):
            continue
        if path.startswith(("/reports", f"{LEDGER}/reports/")) or path in (
            "/accounts",
            "/journal",
            "/settings/ledger",
        ):
            found.append(path)
    return found


def _receivables_unsafe_routes() -> set[tuple[str, str]]:
    prefixes = (
        "/customers",
        "/items",
        "/invoices",
        "/payments",
        "/deposits",
        "/settings/receivables",
    )
    return {
        (method, path)
        for method, path in _endpoints(app)
        if method in UNSAFE_METHODS and path.startswith(prefixes)
    }


def test_receivables_route_list_matches_the_app():
    assert _receivables_unsafe_routes() == set(RECEIVABLES_UNSAFE_ENDPOINTS)


def test_ledger_route_list_matches_the_app():
    assert _ledger_unsafe_routes() == set(LEDGER_UNSAFE_ENDPOINTS)
    pages = _readable_ledger_pages()
    assert "/reports/trial-balance" in pages and f"{LEDGER}/reports/journal.csv" in pages
    assert len(pages) >= 20


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_viewer_and_accountant_can_read_all_reports_but_cannot_mutate_ledger(
    request, role_client, anon_client
):
    client = request.getfixturevalue(role_client)
    for method, path in LEDGER_UNSAFE_ENDPOINTS:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 403, (method, path, response.status_code)
        assert response.json()["errors"][0]["type"] == "PermissionDeniedError", (method, path)
        anonymous = anon_client.request(method, path, follow_redirects=False)
        _assert_refused_unauthenticated(anonymous, method, path)
    window = "?asOf=2026-12-31&start=2026-01-01&end=2026-12-31"  # the required query parameters
    for path in _readable_ledger_pages():
        response = client.get(path + window)
        if path.endswith(".pdf") and not _pdf_available():
            assert response.status_code not in (401, 403), path  # authorized; no Pango here
        else:
            assert response.status_code == 200, (path, response.status_code)
        anonymous = anon_client.get(path + window, follow_redirects=False)
        assert anonymous.status_code in (401, 303), path


def test_unsafe_route_enumeration_catches_a_missing_role_check(session, monkeypatch, viewer_user):
    """A route added with only the reader dependency (owner check forgotten) fails the check."""
    from fastapi import Depends

    from payroll.app import create_app
    from payroll.auth.permissions import require_reader
    from payroll.common.dependencies import get_session
    from tests.conftest import login_as

    application = create_app()

    @application.post("/htmx/forgot-owner-check", dependencies=[Depends(require_reader)])
    async def forgot_owner_check() -> dict:
        return {"mutated": True}

    def override_get_session():
        yield session

    application.dependency_overrides[get_session] = override_get_session
    client = TestClient(application, raise_server_exceptions=False)
    login_as(client, viewer_user)
    with pytest.raises(AssertionError, match="POST /htmx/forgot-owner-check -> 200"):
        _assert_unsafe_routes_owner_only(application, client)


def test_role_is_reread_every_request(owner_client, owner_user, session):
    """Demoting a signed-in owner takes effect on their next request (same session)."""
    url = f"{API}/config/increment-journal-no"
    assert owner_client.post(url).status_code == 200
    owner_user.role = "viewer"
    session.add(owner_user)
    session.flush()
    response = owner_client.post(url)
    assert response.status_code == 403
    assert response.json()["errors"][0]["type"] == "PermissionDeniedError"
    assert owner_client.get(f"{API}/runs").status_code == 200  # still signed in, as a viewer


def test_anonymous_browser_is_sent_to_login(anon_client):
    response = anon_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F"


def test_owner_sees_index_with_user_menu(owner_client, owner_user):
    response = owner_client.get("/")
    assert response.status_code == 200
    assert owner_user.display_name in response.text
    assert 'action="/logout"' in response.text
    assert 'href="/account/password"' in response.text
    assert 'href="/admin"' in response.text


def test_viewer_index_hides_admin_link(viewer_client):
    response = viewer_client.get("/")
    assert response.status_code == 200
    assert 'href="/admin"' not in response.text
    assert 'action="/logout"' in response.text


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_viewer_cannot_mutate(request, role_client, owner_client, session):
    run_id = _saved_run_id(owner_client)
    audit_before = len(session.exec(select(AuditLog)).all())
    client = request.getfixturevalue(role_client)

    attempts = [
        client.post(f"{API}/runs/calculate", json={**RUN_INPUT, "payPeriod": "2026-02"}),
        client.delete(f"{API}/runs/{run_id}"),
        client.put(f"{API}/config", json={}),
        client.post(f"{API}/config/increment-journal-no"),
        client.post("/admin", data={"next_journal_no": "999"}),
        client.get("/admin"),
        client.post("/htmx/calculate", data=HTMX_FORM, headers=HX),
        client.post("/htmx/save-run", data=HTMX_FORM, headers=HX),
        client.delete(f"/htmx/runs/{run_id}", headers=HX),
        client.post("/htmx/increment-journal-no", headers=HX),
    ]
    for response in attempts:
        assert response.status_code == 403, (response.request.method, response.request.url)
        assert response.json()["errors"][0]["type"] == "PermissionDeniedError"

    assert session.get(PayrollRun, run_id) is not None
    assert len(session.exec(select(PayrollRun)).all()) == 1
    assert len(session.exec(select(AuditLog)).all()) == audit_before


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client", "owner_client"])
def test_viewer_can_read_history(request, role_client, owner_client):
    run_id = _saved_run_id(owner_client)
    client = request.getfixturevalue(role_client)

    for path in [
        f"{API}/runs",
        f"{API}/runs/{run_id}",
        f"{API}/config",
        f"{API}/tax-constants",
        f"{PREFIX}/info",
        "/",
        "/htmx/history",
        f"/htmx/runs/{run_id}",
    ]:
        response = client.get(path, headers=HX if path.startswith("/htmx/") else {})
        assert response.status_code == 200, (path, response.status_code)
    assert client.get(f"{API}/runs").json()[0]["id"] == run_id


def test_owner_can_mutate(owner_client, owner_user, session):
    run_id = _saved_run_id(owner_client)
    assert owner_client.get("/admin").status_code == 200
    assert owner_client.post(f"{API}/config/increment-journal-no").status_code == 200
    config = owner_client.get(f"{API}/config").json()
    response = owner_client.put(f"{API}/config", json={**config, "defaultGrossSalary": "7777.00"})
    assert response.status_code == 200
    for action in ("payroll_config.increment_journal_no", "payroll_config.update"):
        (row,) = session.exec(select(AuditLog).where(AuditLog.action == action)).all()
        assert row.user_id == owner_user.id
    assert owner_client.delete(f"{API}/runs/{run_id}").status_code == 204
    assert session.get(PayrollRun, run_id) is None


def test_route_save_and_delete_record_actor(owner_client, owner_user, session):
    run_id = _saved_run_id(owner_client)
    assert owner_client.delete(f"{API}/runs/{run_id}").status_code == 204
    rows = session.exec(
        select(AuditLog).where(AuditLog.action.in_(["payroll_run.save", "payroll_run.delete"]))
    ).all()
    assert sorted(r.action for r in rows) == ["payroll_run.delete", "payroll_run.save"]
    assert {r.user_id for r in rows} == {owner_user.id}


def test_inactive_user_session_is_unauthenticated(owner_client, owner_user, session):
    owner_user.is_active = False
    session.add(owner_user)
    session.flush()
    assert owner_client.get(f"{API}/runs").status_code == 401


# ---------------------------------------------------------------------------
# API docs
# ---------------------------------------------------------------------------


def test_docs_disabled_when_not_debug(owner_client):
    assert app_settings.debug_mode is False
    for path in DOCS_PATHS:
        assert owner_client.get(path).status_code == 404, path


def test_docs_require_auth_when_debug(debug_app, viewer_user):
    from tests.conftest import login_as

    with TestClient(debug_app, raise_server_exceptions=False) as anon:
        for path in DOCS_PATHS:
            assert anon.get(path).status_code == 401, path

    with TestClient(debug_app, raise_server_exceptions=False) as signed_in:
        login_as(signed_in, viewer_user)
        for path in DOCS_PATHS:
            assert signed_in.get(path).status_code == 200, path
        spec = signed_in.get(f"{PREFIX}/openapi.json").json()
        assert f"{API}/runs" in spec["paths"]
        docs = signed_in.get(f"{PREFIX}/docs")
        assert f"{PREFIX}/openapi.json" in docs.text
        # The docs pages keep their looser (CDN) CSP; the JSON keeps the strict one.
        assert "https://cdn.jsdelivr.net" in docs.headers["Content-Security-Policy"]
        assert (
            "https://cdn.jsdelivr.net"
            not in (signed_in.get(f"{PREFIX}/openapi.json").headers["Content-Security-Policy"])
        )


def test_require_role_rejects_unknown_or_missing_roles():
    with pytest.raises(ValueError):
        require_role("admin")
    with pytest.raises(ValueError):
        require_role()
