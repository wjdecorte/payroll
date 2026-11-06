"""
ASGI middleware for security response headers and the pre-session CSRF cookie
(DESIGN §8.3). Both are plain ASGI so they add no per-request task overhead.
"""

import re
from http.cookies import SimpleCookie

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from payroll import app_settings
from payroll.auth.dependencies import PENDING_CSRF_COOKIE_STATE, csrf_cookie_name

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
# FastAPI's Swagger UI / ReDoc pages load their bundles from jsdelivr and boot with
# an inline script, so those two pages only get a looser policy. The app's own
# pages and the JSON API keep the strict one above.
DOCS_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
HSTS_VALUE = "max-age=31536000"
STATIC_PREFIX = "/static/"


def security_headers(path: str, docs_paths: frozenset[str] = frozenset()) -> dict[str, str]:
    """The security headers for a response to ``path`` (see SecurityHeadersMiddleware)."""
    headers = {
        "Content-Security-Policy": (
            DOCS_CONTENT_SECURITY_POLICY if path in docs_paths else CONTENT_SECURITY_POLICY
        ),
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
    }
    if not path.startswith(STATIC_PREFIX):
        headers["Cache-Control"] = "no-store"
    if app_settings.session_cookie_secure:
        headers["Strict-Transport-Security"] = HSTS_VALUE
    return headers


class SecurityHeadersMiddleware:
    """
    Adds CSP, ``X-Frame-Options: DENY``, ``Referrer-Policy: same-origin``,
    ``X-Content-Type-Options: nosniff`` to every response, ``Cache-Control:
    no-store`` to everything except ``/static/*`` (a route's own Cache-Control is kept), and HSTS only when
    ``session_cookie_secure`` is on (i.e. the app is served over TLS).

    Starlette's ServerErrorMiddleware sits outside every user middleware, so the
    app's unhandled-exception (500) handler adds :func:`security_headers` itself.
    """

    def __init__(self, app: ASGIApp, docs_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        wanted = security_headers(scope.get("path", ""), self.docs_paths)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in wanted.items():
                    if name == "Cache-Control" and name in headers:
                        continue  # a route that set its own (receipt downloads) keeps it
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _BodyTooLargeError(Exception):
    pass


# The receipt upload routes (expense create and add-receipts) take multipart bodies of
# several files, so they get the larger ``max_upload_body_bytes`` limit and answer 422
# (their forms are pages, so a too-large upload is a validation failure) instead of 413.
_UPLOAD_PATH = re.compile(r"^/expenses/(?:new|[0-9]{1,12}/receipts)$")


def _is_upload(scope: Scope) -> bool:
    return scope.get("method") == "POST" and _UPLOAD_PATH.match(scope.get("path", "")) is not None


class BodySizeLimitMiddleware:
    """
    Refuses request bodies larger than ``app_settings.max_request_body_bytes``
    (``max_upload_body_bytes`` and 422 on the receipt upload routes) with 413, before FastAPI parses them (FastAPI reads a route's declared form
    or JSON body before running dependencies such as the CSRF check, so the
    limit has to sit in front of routing).

    - A ``Content-Length`` over the limit is refused without reading the body
      (an unparsable one gets 400).
    - A streamed body (no ``Content-Length``, or one that understates the size)
      is counted as it is received and cut off once it passes the limit. If the
      app turned that error into its own response (FastAPI makes body read
      errors a 400), that response is replaced by the 413.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        upload = _is_upload(scope)
        limit = app_settings.max_request_body_bytes
        refusal = 413
        if upload:
            limit = max(limit, app_settings.max_upload_body_bytes)
            refusal = 422
        declared = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                declared = value
                break
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await _plain_response(send, 400, b"Invalid Content-Length")
                return
            if length > limit:
                await _plain_response(send, refusal, b"Request body too large")
                return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                # Replace whatever the app made of the aborted read with one 413.
                if not response_started:
                    response_started = True
                    await _plain_response(send, refusal, b"Request body too large")
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLargeError:
            if not response_started:
                response_started = True
                await _plain_response(send, refusal, b"Request body too large")


async def _plain_response(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def presession_cookie_header(token: str) -> str:
    """The ``Set-Cookie`` value for a pre-session CSRF token (browser-session lifetime)."""
    name = csrf_cookie_name()  # __Host- prefixed when Secure: Path=/ and no Domain
    cookie = SimpleCookie()
    cookie[name] = token
    morsel = cookie[name]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    if app_settings.session_cookie_secure:
        morsel["secure"] = True
    return morsel.OutputString()


class CSRFCookieMiddleware:
    """
    Sets the pre-session CSRF cookie when a page minted a token during rendering
    (see :func:`payroll.auth.dependencies.csrf_token_for`).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Make sure the per-request state dict exists before the app runs: scope
        # copies made further in are shallow, so request.state writes land here.
        state = scope.setdefault("state", {})

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                token = state.get(PENDING_CSRF_COOKIE_STATE)
                if token:
                    MutableHeaders(scope=message).append(
                        "Set-Cookie", presession_cookie_header(token)
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)
