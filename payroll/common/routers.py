import logging

from fastapi import APIRouter, Depends

from payroll import app_settings
from payroll.auth.dependencies import current_user
from payroll.common.middleware import LogRoute

logger = logging.getLogger(f"{app_settings.logger_name}.common")

router = APIRouter(route_class=LogRoute, tags=["system"])


# Public (PUBLIC_PATHS): container/load-balancer probes carry no session.
@router.get("/healthcheck", summary="Health check")
async def healthcheck() -> dict:
    """Returns 200 OK when the service is running."""
    return {"status": "healthy"}


@router.get("/info", summary="Application info", dependencies=[Depends(current_user)])
async def info() -> dict:
    """Returns application metadata and current configuration."""
    return {
        "appName": app_settings.app_name,
        "version": app_settings.app_version,
        "build": app_settings.build_sha[:7],
        "buildNumber": app_settings.build_number,
        "debugMode": app_settings.debug_mode,
        "baseUrlPrefix": app_settings.base_url_prefix,
    }
