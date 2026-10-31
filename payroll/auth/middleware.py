"""
ASGI middleware for security response headers and the pre-session CSRF cookie
(DESIGN §8.3). Both are plain ASGI so they add no per-request task overhead.
"""

from http.cookies import SimpleCookie

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from payroll import app_settings
from payroll.auth.dependencies import CSRF_COOKIE_NAME, PENDING_CSRF_COOKIE_STATE

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


class SecurityHeadersMiddleware:
    """
    Adds CSP, ``X-Frame-Options: DENY``, ``Referrer-Policy: same-origin``,
    ``X-Content-Type-Options: nosniff`` to every response, ``Cache-Control:
    no-store`` to everything except ``/static/*``, and HSTS only when
    ``session_cookie_secure`` is on (i.e. the app is served over TLS).
    """

    def __init__(self, app: ASGIApp, docs_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.docs_paths = docs_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        is_static = path.startswith(STATIC_PREFIX)
        csp = DOCS_CONTENT_SECURITY_POLICY if path in self.docs_paths else CONTENT_SECURITY_POLICY

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = csp
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
                headers["X-Content-Type-Options"] = "nosniff"
                if not is_static:
                    headers["Cache-Control"] = "no-store"
                if app_settings.session_cookie_secure:
                    headers["Strict-Transport-Security"] = HSTS_VALUE
            await send(message)

        await self.app(scope, receive, send_wrapper)


def presession_cookie_header(token: str) -> str:
    """The ``Set-Cookie`` value for a pre-session CSRF token (browser-session lifetime)."""
    cookie = SimpleCookie()
    cookie[CSRF_COOKIE_NAME] = token
    morsel = cookie[CSRF_COOKIE_NAME]
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
