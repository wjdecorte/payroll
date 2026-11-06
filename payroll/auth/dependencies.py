"""
Request-level auth: the session cookie, the signed-in user and CSRF (SPEC
SEC1/SEC4/SEC5, DESIGN §8.2).

Dependencies
------------
- ``current_session`` loads the session named by the cookie (or None) once per
  request and caches it on ``request.state.auth_session``.
- ``current_user`` returns the fully signed-in :class:`User`. A missing, expired or
  pending-MFA session, or an inactive user, is unauthenticated: API paths
  (under ``base_url_prefix``) and HTMX requests get 401, full-page requests a 303
  to ``/login?next=<path>``.
- ``pending_session_user`` is the same but also accepts a session that has passed
  the password step only (``mfa_verified`` False); only the MFA/enrollment routes
  may use it.
- ``verify_csrf`` is installed app-wide (``FastAPI(dependencies=...)``) and
  rejects POST/PUT/PATCH/DELETE with 403 unless they carry the right token in
  the ``X-CSRF-Token`` header or a ``csrf_token`` form field.

CSRF design
-----------
With a session, the expected token is the session's random ``csrf_token``
(synchronizer token; rotated together with the session).

Without a session (e.g. ``POST /login``), there is nothing server-side to bind a
token to, so a *signed double-submit cookie* is used: the token is
``<nonce>.<hmac>`` where ``hmac = HMAC-SHA256(derive_subkey(CSRF_KEY_LABEL), nonce)``.
The same value is set as the ``dci_csrf`` cookie (HttpOnly, SameSite=Lax, Secure
per ``session_cookie_secure``) and rendered into the page; an unsafe request must
send the cookie *and* the identical value in the header/form field, and the
cookie must carry a valid signature. A cross-site attacker can neither read the
cookie nor plant a value of their choosing (the signature is checked), so they
cannot produce a matching pair. Once a session exists the cookie is no longer
an alternative: only the session token is accepted.

Cookie names
------------
When ``session_cookie_secure`` is on, both the session cookie and the
pre-session CSRF cookie carry the ``__Host-`` prefix (always with ``Path=/``,
``Secure`` and no ``Domain``), so a sibling subdomain or a plain-http response
cannot set or shadow them. Use :func:`session_cookie_name` /
:func:`csrf_cookie_name`, never the bare setting.

Templates get the token through :func:`csrf_context`. When a page is rendered
with no session and no valid pre-session cookie, :func:`csrf_token_for` mints
one and leaves it in ``request.state`` for
:class:`payroll.auth.middleware.CSRFCookieMiddleware` to set on the response.
"""

import hashlib
import hmac
import secrets
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.models import AuthSession, User
from payroll.auth.sessions import load_session
from payroll.common.crypto import derive_subkey
from payroll.common.dependencies import get_session

CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_COOKIE_NAME = "dci_csrf"
CSRF_KEY_LABEL = "payroll/csrf-presession/v1"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# request.state key read by CSRFCookieMiddleware: a freshly minted pre-session token.
PENDING_CSRF_COOKIE_STATE = "csrf_cookie_pending"

HOST_COOKIE_PREFIX = "__Host-"

_MAX_NEXT_LENGTH = 2048
_MAX_TOKEN_LENGTH = 256
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class CSRFError(HTTPException):
    """403 for a missing or wrong CSRF token (the auth forms render it as a page)."""

    def __init__(self) -> None:
        super().__init__(403, "CSRF token missing or invalid")


def session_cookie_name() -> str:
    """The session cookie's name: ``__Host-`` prefixed when cookies are Secure."""
    base = app_settings.session_cookie_name
    return HOST_COOKIE_PREFIX + base if app_settings.session_cookie_secure else base


def csrf_cookie_name() -> str:
    """The pre-session CSRF cookie's name: ``__Host-`` prefixed when cookies are Secure."""
    if app_settings.session_cookie_secure:
        return HOST_COOKIE_PREFIX + CSRF_COOKIE_NAME
    return CSRF_COOKIE_NAME


# ---------------------------------------------------------------------------
# Redirect target sanitizing
# ---------------------------------------------------------------------------


def safe_next_path(next: str | None, default: str = "/") -> str:
    """
    ``next`` if it is a same-site relative path, else ``default``. Accepted values
    are printable ASCII only (0x20-0x7E; anything else must arrive
    percent-encoded), start with a single ``/`` and contain no backslash, scheme
    or network location, so they can never redirect off-site (``//host``,
    ``/\\host``, ``https://...``, ``javascript:...``, or Unicode look-alike
    slashes and line separators that a browser or proxy might reinterpret).
    """
    if not isinstance(next, str) or not next or len(next) > _MAX_NEXT_LENGTH:
        return default
    if not next.startswith("/") or next.startswith("//"):
        return default
    if "\\" in next:
        return default
    if any(not 0x20 <= ord(ch) <= 0x7E for ch in next):
        return default
    parts = urlsplit(next)
    if parts.scheme or parts.netloc:
        return default
    return next


# ---------------------------------------------------------------------------
# Pre-session (signed double-submit) CSRF tokens
# ---------------------------------------------------------------------------


def _sign(nonce: str) -> str:
    key = derive_subkey(CSRF_KEY_LABEL)
    return hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def new_presession_csrf_token() -> str:
    """A new signed pre-session token, ``<nonce>.<hmac>``."""
    nonce = secrets.token_urlsafe(32)
    return f"{nonce}.{_sign(nonce)}"


def presession_token_valid(token: str | None) -> bool:
    """True if ``token`` is a pre-session token this server signed."""
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
        return False
    nonce, sep, signature = token.partition(".")
    if not sep or not nonce or not signature:
        return False
    return hmac.compare_digest(signature.encode(), _sign(nonce).encode())


def csrf_token_for(request: Request) -> str:
    """
    The CSRF token a page rendered for ``request`` must embed: the session's token,
    else the valid pre-session cookie's, else a newly minted pre-session token
    (which CSRFCookieMiddleware then sets as a cookie on the response).
    """
    state = request.state
    # current_session copies the session's token here, so a page rendered after
    # the route committed never lazy-loads the (possibly rotated away) session row.
    token = getattr(state, "csrf_token", None)
    if token:
        return token
    auth = getattr(state, "auth_session", None)
    if auth is not None:
        return auth.csrf_token
    cookie = request.cookies.get(csrf_cookie_name())
    if presession_token_valid(cookie):
        token = cookie
    else:
        token = new_presession_csrf_token()
        setattr(state, PENDING_CSRF_COOKIE_STATE, token)
    state.csrf_token = token
    return token


def csrf_context(request: Request) -> dict[str, object]:
    """
    Jinja2 context processor: exposes ``csrf_token`` to every template, and
    ``current_user`` (the signed-in user when the route resolved one, else None).
    """
    return {
        "csrf_token": csrf_token_for(request),
        "current_user": getattr(request.state, "user", None),
    }


# ---------------------------------------------------------------------------
# Session / user dependencies
# ---------------------------------------------------------------------------


def current_session(request: Request, db: Session = Depends(get_session)) -> AuthSession | None:
    """
    The live session named by the session cookie, or None. Loaded once per request
    and cached on ``request.state.auth_session``. ``load_session`` may refresh
    ``last_seen_at`` or (bulk) delete an expired row; that is committed here
    because many requests (GETs) never commit. Nothing else is pending in the
    unit of work this early in the request.
    """
    state = request.state
    if hasattr(state, "auth_session"):
        return state.auth_session
    raw = request.cookies.get(session_cookie_name())
    auth = load_session(db, raw) if raw else None
    if auth is not None:
        state.csrf_token = auth.csrf_token
    if raw and (auth is None or db.dirty):
        db.commit()
    state.auth_session = auth
    return auth


def _is_api_path(path: str) -> bool:
    prefix = app_settings.base_url_prefix
    return path == prefix or path.startswith(prefix + "/")


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _unauthenticated(request: Request) -> HTTPException:
    if _is_htmx(request):
        # htmx does not swap a 401, but it follows HX-Redirect.
        return HTTPException(401, "Not authenticated", headers={"HX-Redirect": "/login"})
    if _is_api_path(request.url.path):
        return HTTPException(401, "Not authenticated")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    location = "/login?" + urlencode({"next": safe_next_path(target)})
    return HTTPException(303, "Login required", headers={"Location": location})


def _active_user(db: Session, auth: AuthSession | None) -> User | None:
    if auth is None:
        return None
    user = db.get(User, auth.user_id)
    if user is None or not user.is_active:
        return None
    return user


def current_user(
    request: Request,
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> User:
    """The fully signed-in user (password and MFA done); see module docstring."""
    user = _active_user(db, auth) if auth is not None and auth.mfa_verified else None
    if user is None:
        raise _unauthenticated(request)
    request.state.user = user
    return user


def pending_session_user(
    request: Request,
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> User:
    """
    The user of any live session, MFA verified or not. ONLY for the MFA challenge
    and enrollment routes; everything else must use :func:`current_user`.
    """
    user = _active_user(db, auth)
    if user is None:
        raise _unauthenticated(request)
    request.state.user = user
    return user


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def _supplied_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(_FORM_TYPES):
        # Starlette caches the parsed form on the request, so the route can still read it.
        value = (await request.form()).get(CSRF_FORM_FIELD)
        if isinstance(value, str) and value:
            return value
    return None


async def verify_csrf(
    request: Request,
    auth: AuthSession | None = Depends(current_session),
    db: Session = Depends(get_session),
) -> None:
    """
    App-wide: 403 (:class:`CSRFError`) for an unsafe method without the right
    CSRF token. The expected token is worked out first; when there is none (no
    session and no valid pre-session cookie) the request is refused before this
    dependency reads its body. A request with no session on any route that is not
    public is passed on without this dependency reading the body (a wrong header
    token is still a 403; the route's own sign-in check refuses the rest). That is
    not a guarantee that nothing parses it: FastAPI parses the body of a route that
    declares ``Form(...)`` / ``File(...)`` parameters before any dependency runs
    (bounded by the 1 MiB body limit). The upload routes declare none and read
    ``request.form()`` themselves, so for them an anonymous body is never parsed.
    The pre-session token is only compared on the public
    sign-in forms. A signed-in owner on a receipt upload route is granted the larger
    body limit (``scope["upload_body_limit"]``, set by ``BodySizeLimitMiddleware``)
    before the form is read; everyone else keeps the ordinary limit.
    """
    if request.method not in UNSAFE_METHODS:
        return
    if auth is not None:
        expected = auth.csrf_token
    else:
        cookie = request.cookies.get(csrf_cookie_name())
        expected = cookie if presession_token_valid(cookie) else None
    if not expected:
        raise CSRFError()
    if auth is None:
        from payroll.auth.permissions import is_public_path

        if not is_public_path(request.url.path):
            # Never parse a body for an anonymous request: a header token is compared,
            # otherwise the request goes on unchecked and the route's sign-in check refuses it.
            header = request.headers.get(CSRF_HEADER)
            if header and not hmac.compare_digest(header.encode(), expected.encode()):
                raise CSRFError()
            return
    elif "upload_body_limit" in request.scope and auth.mfa_verified:
        user = _active_user(db, auth)
        if user is not None and user.role == "owner":
            request.scope["body_limit"] = request.scope["upload_body_limit"]
    supplied = await _supplied_token(request)
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise CSRFError()
