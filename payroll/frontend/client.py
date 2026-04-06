"""
HTTP client for the Payroll API.

The frontend server calls the API service internally (not the browser).
All requests use httpx with a shared AsyncClient for connection pooling.
"""

import httpx

from payroll.frontend import frontend_settings


class APIError(Exception):
    """Raised when the API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


def _extract_error_message(response: httpx.Response) -> str:
    """Pull the first error message out of a structured API error response."""
    try:
        body = response.json()
        errors = body.get("errors", [])
        if errors:
            return errors[0].get("message", response.text)
    except Exception:
        pass
    return response.text or f"HTTP {response.status_code}"


class PayrollAPIClient:
    """Thin async wrapper around the Payroll REST API."""

    def __init__(self) -> None:
        self.base_url = frontend_settings.api_base_url

    async def calculate(self, payload: dict) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/payroll/runs/calculate",
                json=payload,
                timeout=30.0,
            )
            if not response.is_success:
                raise APIError(response.status_code, _extract_error_message(response))
            return response.json()

    async def list_runs(self, limit: int = 50) -> list[dict]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/payroll/runs",
                params={"limit": limit},
                timeout=10.0,
            )
            if not response.is_success:
                raise APIError(response.status_code, _extract_error_message(response))
            return response.json()

    async def get_run(self, run_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/payroll/runs/{run_id}",
                timeout=10.0,
            )
            if not response.is_success:
                raise APIError(response.status_code, _extract_error_message(response))
            return response.json()

    async def delete_run(self, run_id: int) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{self.base_url}/payroll/runs/{run_id}",
                timeout=10.0,
            )
            if not response.is_success:
                raise APIError(response.status_code, _extract_error_message(response))
