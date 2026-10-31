"""Security response headers (DESIGN §8.3)."""

import pytest

from payroll import app_settings
from payroll.auth.middleware import CONTENT_SECURITY_POLICY

PREFIX = app_settings.base_url_prefix

EXPECTED_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _assert_common(response):
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_csp_constant_is_exact():
    assert CONTENT_SECURITY_POLICY == EXPECTED_CSP


@pytest.mark.parametrize("path", ["/", f"{PREFIX}/healthcheck", f"{PREFIX}/info"])
def test_headers_on_pages_and_api(client, path):
    response = client.get(path)
    assert response.status_code == 200
    _assert_common(response)
    assert response.headers["Cache-Control"] == "no-store"


def test_headers_on_api_post_and_errors(client):
    response = client.post(f"{PREFIX}/payroll/runs/calculate", json={})
    assert response.status_code == 422
    _assert_common(response)
    assert response.headers["Cache-Control"] == "no-store"
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    _assert_common(response)


def test_static_not_no_store(client):
    response = client.get("/static/favicon-32.png")
    assert response.status_code == 200
    _assert_common(response)
    assert "no-store" not in response.headers.get("Cache-Control", "")


def test_hsts_only_when_secure(client, monkeypatch):
    monkeypatch.setattr(app_settings, "session_cookie_secure", True)
    assert client.get("/").headers["Strict-Transport-Security"] == "max-age=31536000"
    monkeypatch.setattr(app_settings, "session_cookie_secure", False)
    assert "Strict-Transport-Security" not in client.get("/").headers


def test_docs_pages_get_their_own_policy(client):
    response = client.get(f"{PREFIX}/docs")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "https://cdn.jsdelivr.net" in csp
    # The OpenAPI JSON itself keeps the strict policy.
    assert client.get(f"{PREFIX}/openapi.json").headers["Content-Security-Policy"] == EXPECTED_CSP
