import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from payroll import app_settings
from payroll.auth.dependencies import CSRFError, verify_csrf
from payroll.auth.middleware import (
    BodySizeLimitMiddleware,
    CSRFCookieMiddleware,
    SecurityHeadersMiddleware,
    security_headers,
)
from payroll.exceptions import AppBaseError
from payroll.logger_conf import configure_logging

logger = logging.getLogger(f"{app_settings.logger_name}.app")


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run setup tasks on startup and teardown on shutdown."""
    configure_logging()
    logger.info("Starting %s v%s", app_settings.app_name, app_settings.app_version)

    # Bring the database schema to the latest revision via Alembic. A failed
    # migration must stop startup: never serve against a half-migrated schema.
    from payroll.migrations.alembic_runner import upgrade as _alembic_upgrade

    try:
        _alembic_upgrade()
    except Exception:
        logger.exception("Database migration failed — refusing to start")
        raise
    logger.info("Database schema is up to date (Alembic)")

    yield

    logger.info("Shutting down %s", app_settings.app_name)


def create_app() -> FastAPI:
    application = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        description="S-Corp owner payroll calculator with QuickBooks journal entries.",
        docs_url=f"{app_settings.base_url_prefix}/docs",
        redoc_url=f"{app_settings.base_url_prefix}/redoc",
        openapi_url=f"{app_settings.base_url_prefix}/openapi.json",
        lifespan=lifespan,
        # CSRF check on every POST/PUT/PATCH/DELETE route (safe methods pass through).
        # App-level dependencies are attached to every FastAPI route: those added
        # with include_router, @application.<method> or add_api_route. Plain
        # Starlette routes (application.add_route / add_websocket_route) and
        # anything mounted (application.mount: /static, a sub-application) bypass
        # it, so they must never accept state-changing requests.
        dependencies=[Depends(verify_csrf)],
    )

    # Security headers on every response (incl. errors and /static); the CSRF cookie
    # middleware sets the pre-session double-submit cookie when a page minted one.
    docs_paths = frozenset({application.docs_url, application.redoc_url})
    # Innermost user middleware (added first), so its 413s still get the security
    # headers; it runs before routing, i.e. before FastAPI parses any body.
    application.add_middleware(BodySizeLimitMiddleware)
    application.add_middleware(CSRFCookieMiddleware)
    application.add_middleware(SecurityHeadersMiddleware, docs_paths=docs_paths)

    from payroll.auth.routers import csrf_error_handler

    # Stale login/MFA/enroll forms get an "expired" page; other CSRF failures a 403.
    application.add_exception_handler(CSRFError, csrf_error_handler)

    # ------------------------------------------------------------------ #
    # Global exception handler — catches all AppBaseError subclasses and  #
    # returns a structured JSON response with error code, type, message.  #
    # ------------------------------------------------------------------ #
    @application.exception_handler(AppBaseError)
    async def app_error_handler(request: Request, exc: AppBaseError) -> JSONResponse:
        logger.warning("Application error [%s]: %s", exc.__class__.__name__, exc.message)
        return JSONResponse(
            status_code=exc.http_code,
            content={"errors": [exc.to_dict()]},
        )

    @application.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        # Starlette's ServerErrorMiddleware runs this outside the user middleware,
        # so SecurityHeadersMiddleware never sees the 500: add its headers here.
        return JSONResponse(
            headers=security_headers(request.url.path, docs_paths),
            status_code=500,
            content={
                "errors": [
                    {
                        "code": "payroll.error.unhandled",
                        "type": exc.__class__.__name__,
                        "message": "An unexpected error occurred.",
                    }
                ]
            },
        )

    # ------------------------------------------------------------------ #
    # Register routers                                                     #
    # ------------------------------------------------------------------ #
    from payroll.common.routers import router as common_router
    from payroll.payroll_run.routers import router as payroll_run_router

    prefix = app_settings.base_url_prefix

    application.include_router(common_router, prefix=prefix)
    application.include_router(payroll_run_router, prefix=prefix)

    # UI: pages + HTMX partials (hidden from the OpenAPI schema) and static assets.
    from fastapi.staticfiles import StaticFiles

    from payroll.frontend.routers import router as frontend_router

    application.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "frontend" / "static"),
        name="static",
    )
    application.include_router(frontend_router)

    from payroll.auth.routers import router as auth_router

    application.include_router(auth_router)

    logger.info("Registered routers with prefix: %s", prefix)
    return application


app = create_app()
