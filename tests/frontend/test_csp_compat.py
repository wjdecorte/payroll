"""
The UI must work under ``script-src 'self'``: no inline JS, no inline event
handlers, no third-party script/style URLs, and every HTMX request carries the
CSRF token.
"""

import json
from html.parser import HTMLParser

import pytest

from payroll.auth.dependencies import CSRF_HEADER
from tests.conftest import login_as, make_user
from tests.frontend.test_pages import FORM


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = _Collector()
    parser.feed(html)
    return parser.tags


def _assert_csp_safe(html: str) -> None:
    tags = _tags(html)
    for tag, attrs in tags:
        handlers = [name for name in attrs if name.startswith("on")]
        assert not handlers, f"inline event handler on <{tag}>: {handlers}"
        for name in ("src", "href"):
            url = attrs.get(name)
            if url and tag in ("script", "link"):
                assert url.startswith("/") and not url.startswith("//"), f"external {url}"
        if tag == "script":
            assert attrs.get("src") or attrs.get("type") == "application/json", (
                f"inline <script> {attrs}"
            )
    # Belt and braces for things HTMLParser might treat oddly.
    lowered = html.lower()
    assert "onclick=" not in lowered
    assert "onsubmit=" not in lowered
    assert "javascript:" not in lowered


@pytest.fixture(name="authed")
def authed_fixture(client, session):
    token = login_as(client, make_user(session))
    return client, token


def test_index_page_is_csp_safe_and_sends_csrf_header(authed):
    client, token = authed
    response = client.get("/")
    assert response.status_code == 200
    _assert_csp_safe(response.text)
    body = next(attrs for tag, attrs in _tags(response.text) if tag == "body")
    assert json.loads(body["hx-headers"]) == {CSRF_HEADER: token}
    scripts = [a["src"] for t, a in _tags(response.text) if t == "script" and a.get("src")]
    assert "/static/vendor/htmx-2.0.3/htmx.min.js" in scripts
    assert "/static/vendor/bootstrap-5.3.3/js/bootstrap.bundle.min.js" in scripts
    assert "/static/js/app.js" in scripts


def test_results_partial_is_csp_safe(authed):
    client, _ = authed
    response = client.post("/htmx/calculate", data=FORM)
    assert response.status_code == 200
    assert 'data-action="download-qbo-csv"' in response.text
    _assert_csp_safe(response.text)


def test_history_partial_is_csp_safe(authed):
    client, _ = authed
    assert client.post("/htmx/save-run", data=FORM).status_code == 200
    response = client.get("/htmx/history")
    assert "hx-delete" in response.text
    _assert_csp_safe(response.text)


def test_admin_page_is_csp_safe_and_form_has_csrf_field(authed):
    client, token = authed
    response = client.get("/admin")
    assert response.status_code == 200
    _assert_csp_safe(response.text)
    hidden = [a for t, a in _tags(response.text) if t == "input" and a.get("name") == "csrf_token"]
    assert hidden and hidden[0]["value"] == token


def test_admin_form_post_with_field_only(authed):
    """A plain (non-HTMX) form post passes CSRF via the hidden field alone."""
    client, token = authed
    del client.headers[CSRF_HEADER]
    page = client.get("/admin").text
    hidden = {
        a["name"]: a["value"] for t, a in _tags(page) if t == "input" and a.get("type") == "hidden"
    }
    assert hidden["csrf_token"] == token
    # Past CSRF, the incomplete form reaches the handler and fails validation (422).
    data = {"csrf_token": token, "default_gross_salary": "1"}
    assert client.post("/admin", data=data).status_code == 422
    data["csrf_token"] = "wrong"
    assert client.post("/admin", data=data).status_code == 403


def test_index_without_session_still_renders_a_token(client):
    response = client.get("/")
    assert response.status_code == 200
    body = next(attrs for tag, attrs in _tags(response.text) if tag == "body")
    assert json.loads(body["hx-headers"])[CSRF_HEADER]


@pytest.mark.parametrize(
    "path",
    [
        "/static/vendor/htmx-2.0.3/htmx.min.js",
        "/static/vendor/bootstrap-5.3.3/css/bootstrap.min.css",
        "/static/vendor/bootstrap-5.3.3/js/bootstrap.bundle.min.js",
        "/static/vendor/bootstrap-icons-1.11.3/bootstrap-icons.min.css",
        "/static/vendor/bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2",
        "/static/js/app.js",
        "/static/js/qbo.js",
    ],
)
def test_vendored_and_app_assets_served(client, path):
    assert client.get(path).status_code == 200
