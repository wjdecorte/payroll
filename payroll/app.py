import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from payroll import app_settings
from payroll.exceptions import AppBaseError
from payroll.logger_conf import configure_logging

logger = logging.getLogger(f"{app_settings.logger_name}.app")


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run setup tasks on startup and teardown on shutdown."""
    configure_logging()
    logger.info("Starting %s v%s", app_settings.app_name, app_settings.app_version)

    # Ensure all database tables exist.
    # Uses SQLModel's create_all (idempotent — safe to call on every startup).
    # When you need schema changes, generate an Alembic revision and run:
    #   python -m payroll.migrations.alembic_runner upgrade
    from sqlmodel import SQLModel

    import payroll.payroll_run.models  # noqa: F401 — registers models with metadata
    from payroll.common.dependencies import engine

    SQLModel.metadata.create_all(engine)
    logger.info("Database tables verified")

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
    )

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
        return JSONResponse(
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

    logger.info("Registered routers with prefix: %s", prefix)
    return application


app = create_app()
