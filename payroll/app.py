import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from payroll import app_settings
from payroll.auth.dependencies import CSRFError, current_user, verify_csrf
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


def _add_docs_routes(application: FastAPI, docs_url: str, redoc_url: str, openapi_url: str) -> None:
    """
    Swagger UI, ReDoc and the OpenAPI schema, for any signed-in user. Only called
    when ``debug_mode`` is on: in production the schema (every route and model)
    is not served at all.
    """
    from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

    signed_in = [Depends(current_user)]

    @application.get(openapi_url, include_in_schema=False, dependencies=signed_in)
    async def openapi_schema() -> JSONResponse:
        return JSONResponse(application.openapi())

    @application.get(docs_url, include_in_schema=False, dependencies=signed_in)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{application.title} - Swagger UI",
            oauth2_redirect_url=None,
        )

    @application.get(redoc_url, include_in_schema=False, dependencies=signed_in)
    async def redoc() -> HTMLResponse:
        return get_redoc_html(openapi_url=openapi_url, title=f"{application.title} - ReDoc")


def create_app() -> FastAPI:
    prefix = app_settings.base_url_prefix
    docs_url, redoc_url = f"{prefix}/docs", f"{prefix}/redoc"
    # FastAPI's own docs routes would be public: always off; the authenticated
    # replacements are added below only in debug mode.
    application = FastAPI(
        title=app_settings.app_name,
        version=app_settings.app_version,
        description="S-Corp owner payroll calculator with QuickBooks journal entries.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
    # (Only reachable in debug mode, by a signed-in user.)
    docs_paths = frozenset({docs_url, redoc_url}) if app_settings.debug_mode else frozenset()
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
    import payroll.receivables.models  # noqa: F401  (registers the tables with the metadata)
    from payroll.common.routers import router as common_router
    from payroll.ledger.routers import router as ledger_router
    from payroll.payroll_run.routers import router as payroll_run_router
    from payroll.reports.routers import router as reports_router

    application.include_router(common_router, prefix=prefix)
    application.include_router(payroll_run_router, prefix=prefix)
    application.include_router(ledger_router, prefix=prefix)
    application.include_router(reports_router, prefix=prefix)

    # UI: pages + HTMX partials (hidden from the OpenAPI schema) and static assets.
    from fastapi.staticfiles import StaticFiles

    from payroll.frontend.journal_routers import router as journal_pages_router
    from payroll.frontend.ledger_routers import router as ledger_pages_router
    from payroll.frontend.receivable_routers import router as receivable_pages_router
    from payroll.frontend.report_routers import router as report_pages_router
    from payroll.frontend.routers import router as frontend_router

    application.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "frontend" / "static"),
        name="static",
    )
    application.include_router(frontend_router)
    application.include_router(ledger_pages_router)
    application.include_router(journal_pages_router)
    application.include_router(receivable_pages_router)
    application.include_router(report_pages_router)

    from payroll.auth.routers import router as auth_router

    application.include_router(auth_router)

    if app_settings.debug_mode:
        _add_docs_routes(application, docs_url, redoc_url, f"{prefix}/openapi.json")

    logger.info("Registered routers with prefix: %s", prefix)
    return application


app = create_app()
