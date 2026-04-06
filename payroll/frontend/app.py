from fastapi import FastAPI

from payroll.frontend import frontend_settings
from payroll.frontend.routers import router


def create_app() -> FastAPI:
    application = FastAPI(
        title=f"{frontend_settings.app_name} — UI",
        version=frontend_settings.app_version,
        # Hide docs for the frontend app (API docs are on the API service)
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(router)
    return application


app = create_app()
