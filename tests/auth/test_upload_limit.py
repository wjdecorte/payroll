"""The large receipt-upload limit is only for a signed-in owner; nobody else gets a body parsed."""

import pytest
from starlette import formparsers, requests

from payroll import app_settings
from payroll.auth.dependencies import CSRF_HEADER

MB = 1024 * 1024
PATHS = ["/expenses/new", "/expenses/1/receipts"]


@pytest.fixture
def parse_spy(monkeypatch):
    """Records every attempt to parse a form body (and lets it fail loudly)."""
    calls: list[str] = []

    async def form(self, *args, **kwargs):
        calls.append("Request.form")
        raise AssertionError("the request body must not be parsed")

    async def parse(self):
        calls.append("MultiPartParser.parse")
        raise AssertionError("the multipart parser must not run")

    monkeypatch.setattr(requests.Request, "form", form)
    monkeypatch.setattr(formparsers.MultiPartParser, "parse", parse)
    return calls


def big_files():
    return [("receipts", ("big.pdf", b"%PDF-1.4\n" + b"0" * (2 * MB), "application/pdf"))]


@pytest.mark.parametrize("path", PATHS)
def test_anonymous_big_upload_with_a_presession_cookie_is_refused_unparsed(
    anon_client, parse_spy, path
):
    anon_client.headers.pop(CSRF_HEADER)  # cookie only: the token would have to come from the form
    response = anon_client.post(path, files=big_files(), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert parse_spy == []


@pytest.mark.parametrize("path", PATHS)
def test_viewer_big_upload_is_refused_unparsed(viewer_client, parse_spy, path):
    response = viewer_client.post(path, files=big_files())
    assert response.status_code == 403
    assert parse_spy == []


@pytest.mark.parametrize("path", PATHS)
def test_viewer_big_upload_without_the_header_never_gets_the_large_limit(viewer_client, path):
    viewer_client.headers.pop(CSRF_HEADER)
    response = viewer_client.post(path, files=big_files())
    assert response.status_code == 422  # cut off at the ordinary 1 MiB limit


@pytest.mark.parametrize("path", PATHS)
def test_owner_without_the_header_still_gets_the_large_limit(owner_client, path, tmp_path):
    token = owner_client.headers.pop(CSRF_HEADER)
    files = big_files()
    data = {"csrf_token": token}
    response = owner_client.post(path, data=data, files=files, follow_redirects=False)
    assert "too large" not in response.text.lower()  # past the size limit; the form decides
    assert response.status_code in (303, 404, 422)


def test_other_routes_keep_the_small_limit(owner_client):
    response = owner_client.post("/vendors/new", data={"name": "x" * (2 * MB)})
    assert response.status_code == 413


def test_upload_limit_setting_still_caps_the_owner(owner_client, monkeypatch):
    monkeypatch.setattr(app_settings, "max_upload_body_bytes", 3 * MB)
    big = [("receipts", ("big.pdf", b"%PDF-1.4\n" + b"0" * (4 * MB), "application/pdf"))]
    assert owner_client.post("/expenses/new", files=big).status_code == 422
