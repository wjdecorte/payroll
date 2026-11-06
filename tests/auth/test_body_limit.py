"""Request body size limit (BodySizeLimitMiddleware): 413 before any body parsing."""

import asyncio

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from payroll import app_settings
from payroll.app import create_app
from payroll.auth.middleware import BodySizeLimitMiddleware

LIMIT = 100


@pytest.fixture(autouse=True)
def small_limit(monkeypatch):
    monkeypatch.setattr(app_settings, "max_request_body_bytes", LIMIT)


def test_default_limit_is_one_mib():
    assert type(app_settings).model_fields["max_request_body_bytes"].default == 1_048_576


@pytest.fixture(name="app_and_calls")
def app_and_calls_fixture():
    calls: list[int] = []
    application = create_app()
    router = APIRouter()

    @router.post("/_t/sink")
    async def sink(request: Request) -> dict:
        body = await request.body()
        calls.append(len(body))
        return {"n": len(body)}

    application.include_router(router)
    return application, calls


def test_oversized_content_length_is_413_without_route(app_and_calls):
    application, calls = app_and_calls
    client = TestClient(application, raise_server_exceptions=False)
    # Bare client: CSRF would 403 if the route stack were reached; 413 comes first.
    response = client.post("/_t/sink", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert calls == []
    assert response.headers["X-Frame-Options"] == "DENY"  # still gets security headers


def test_login_form_oversized_is_413(app_and_calls):
    application, _ = app_and_calls
    response = TestClient(application, raise_server_exceptions=False).post(
        "/login", data={"email": "a" * 500}
    )
    assert response.status_code == 413


def test_normal_requests_pass(app_and_calls):
    application, _ = app_and_calls
    client = TestClient(application, raise_server_exceptions=False)
    assert client.get("/login").status_code == 200
    assert client.get("/static/favicon-32.png").status_code == 200
    # Within the limit the request reaches the CSRF check (403), not 413.
    assert client.post("/_t/sink", content=b"x" * LIMIT).status_code == 403


def test_invalid_content_length_is_400():
    async def inner(scope, receive, send):  # pragma: no cover - must not run
        raise AssertionError("inner app reached")

    sent = _run(BodySizeLimitMiddleware(inner), [b"abc"], content_length=b"12x")
    assert sent[0]["status"] == 400


# ---------------------------------------------------------------------------
# Streamed bodies (no or lying Content-Length), driven at the ASGI level
# ---------------------------------------------------------------------------


def _run(app, chunks, content_length=None):
    headers = [(b"content-type", b"application/octet-stream")]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {"type": "http", "method": "POST", "path": "/_t/sink", "headers": headers}
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


async def _reading_app(scope, receive, send):
    total = 0
    more = True
    while more:
        message = await receive()
        total += len(message.get("body", b""))
        more = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


async def _swallowing_app(scope, receive, send):
    """Like FastAPI: turns any error while reading the body into a 400."""
    try:
        while (await receive()).get("more_body", False):
            pass
    except Exception:
        await send({"type": "http.response.start", "status": 400, "headers": []})
        await send({"type": "http.response.body", "body": b"parse error"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_streamed_oversize_without_content_length_is_413():
    sent = _run(BodySizeLimitMiddleware(_reading_app), [b"x" * 60, b"x" * 60])
    assert sent[0]["status"] == 413


def test_lying_content_length_is_413():
    sent = _run(BodySizeLimitMiddleware(_reading_app), [b"x" * 60, b"x" * 60], b"10")
    assert sent[0]["status"] == 413


def test_oversize_swallowed_by_app_still_413():
    sent = _run(BodySizeLimitMiddleware(_swallowing_app), [b"x" * 60, b"x" * 60])
    assert sent[0]["status"] == 413
    assert len([m for m in sent if m["type"] == "http.response.start"]) == 1


def test_streamed_within_limit_passes():
    sent = _run(BodySizeLimitMiddleware(_reading_app), [b"x" * 50, b"x" * 50])
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"100"


# ---------------------------------------------------------------------------
# Receipt upload routes: the larger limit, and 422 instead of 413
# ---------------------------------------------------------------------------


def _run_at(app, path, chunks, content_length=None, method="POST"):
    headers = [(b"content-type", b"multipart/form-data; boundary=x")]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _unlocking(inner):
    """Like verify_csrf for a signed-in owner: raise the body limit to the published ceiling."""

    async def app(scope, receive, send):
        scope["body_limit"] = scope["upload_body_limit"]
        await inner(scope, receive, send)

    return app


@pytest.mark.parametrize("path", ["/expenses/new", "/expenses/12/receipts"])
def test_upload_routes_get_the_larger_limit_and_422(monkeypatch, path):
    monkeypatch.setattr(app_settings, "max_upload_body_bytes", 1000)
    app = BodySizeLimitMiddleware(_reading_app)
    # Until verify_csrf has seen a signed-in owner (scope["body_limit"]), the small limit holds.
    assert _run_at(app, path, [b"x" * 900])[0]["status"] == 422
    assert (
        _run_at(BodySizeLimitMiddleware(_unlocking(_reading_app)), path, [b"x" * 900])[0]["status"]
        == 200
    )  # over 100, under 1000
    unlocked = BodySizeLimitMiddleware(_unlocking(_reading_app))
    assert _run_at(unlocked, path, [b"x" * 1001])[0]["status"] == 422  # declared
    assert _run_at(unlocked, path, [b"x" * 600, b"x" * 600], b"10")[0]["status"] == 422  # streamed
    swallowed = _run_at(
        BodySizeLimitMiddleware(_unlocking(_swallowing_app)), path, [b"x" * 600, b"x" * 600]
    )
    assert swallowed[0]["status"] == 422


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/expenses/new/extra"),
        ("POST", "/expenses/1/void"),
        ("POST", "/expenses/abc/receipts"),
        ("POST", "/vendors/new"),
        ("GET", "/expenses/new"),
    ],
)
def test_other_routes_keep_the_small_limit_and_413(monkeypatch, method, path):
    monkeypatch.setattr(app_settings, "max_upload_body_bytes", 1000)
    sent = _run_at(BodySizeLimitMiddleware(_reading_app), path, [b"x" * 101], method=method)
    assert sent[0]["status"] == 413
