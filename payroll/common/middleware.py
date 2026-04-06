import json
import logging
import time
from collections.abc import Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from payroll import app_settings

logger = logging.getLogger(f"{app_settings.logger_name}.middleware")


class LogRoute(APIRoute):
    """
    Custom APIRoute that logs incoming requests and outgoing responses.

    Attach to a router via ``route_class=LogRoute`` to enable structured
    request/response logging on all routes in that router.
    """

    def get_route_handler(self) -> Callable:
        original_handler = super().get_route_handler()

        async def logged_handler(request: Request) -> Response:
            start = time.perf_counter()

            # Attempt to parse request body as JSON for logging
            request_body: dict | None = None
            try:
                raw = await request.body()
                if raw:
                    request_body = json.loads(raw)
            except Exception:
                pass

            logger.info(
                "→ %s %s",
                request.method,
                request.url.path,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "query_params": dict(request.query_params),
                    "path_params": dict(request.path_params),
                    "body": request_body,
                },
            )

            response = await original_handler(request)

            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "← %s %s [%d] %.1fms",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )

            return response

        return logged_handler
