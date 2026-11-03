"""
Role-based authorization (SPEC SEC1, SEC7).

Every route needs a fully signed-in user except the anonymous ones under
:data:`PUBLIC_PATHS` (the login form, the health check and static assets). The
MFA/enrollment pages under ``/login`` and ``/enroll`` are not anonymous: they
use :func:`~payroll.auth.dependencies.pending_session_user`. ``/logout`` accepts
any caller and only ends a session if there is one.

Roles (``User.role``): ``owner`` may do everything; ``accountant`` and
``viewer`` may only read. Routes declare this with :func:`require_role`; the
payroll services check :func:`ensure_role` again on every mutation (defense in
depth), so a route that forgets the role still cannot change data.

A signed-in user lacking the role gets a JSON 403 (``PermissionDeniedError``)
on every path, full pages included; an anonymous request gets 401 (API/HTMX)
or a 303 to ``/login`` from :func:`~payroll.auth.dependencies.current_user`.
"""

import logging
from collections.abc import Callable

from fastapi import Depends

from payroll import app_settings
from payroll.auth.dependencies import current_user
from payroll.auth.models import User, UserRole
from payroll.common.exceptions import PermissionDeniedError

logger = logging.getLogger(f"{app_settings.logger_name}.auth.permissions")

ROLES: tuple[str, ...] = tuple(role.value for role in UserRole)
OWNER = UserRole.OWNER.value
# Prefix match on a path-segment boundary; "/healthcheck" lives under the API prefix.
PUBLIC_PATHS = ("/login", "/healthcheck", "/static")


def is_public_path(path: str) -> bool:
    """True for a path under :data:`PUBLIC_PATHS` (segment-boundary prefix match)."""
    prefix = app_settings.base_url_prefix
    for public in PUBLIC_PATHS:
        base = f"{prefix}{public}" if public == "/healthcheck" else public
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _checked_roles(roles: tuple[str, ...]) -> frozenset[str]:
    if not roles:
        raise ValueError("require at least one role")
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise ValueError(f"unknown role(s): {sorted(unknown)}")
    return frozenset(roles)


def ensure_role(actor: User, *roles: str) -> User:
    """Return ``actor`` if its role is one of ``roles``, else raise PermissionDeniedError."""
    allowed = _checked_roles(roles)
    if actor.role not in allowed:
        logger.warning(
            "Permission denied: user id=%s role=%s needs one of %s",
            actor.id,
            actor.role,
            sorted(allowed),
        )
        raise PermissionDeniedError()
    return actor


def require_role(*roles: str) -> Callable[..., User]:
    """
    FastAPI dependency factory: the signed-in :class:`User` if their role is one
    of ``roles``. Unauthenticated -> 401/303 (``current_user``); wrong role -> 403.
    Use the module-level instances below so FastAPI caches them per request.
    """
    allowed = tuple(_checked_roles(roles))

    def dependency(user: User = Depends(current_user)) -> User:
        return ensure_role(user, *allowed)

    dependency.__name__ = f"require_role_{'_'.join(allowed)}"
    return dependency


# Shared instances (one callable each, so FastAPI resolves each once per request).
require_owner = require_role(OWNER)
require_reader = require_role(*ROLES)
