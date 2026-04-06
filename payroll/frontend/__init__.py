from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class FrontendSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Payroll"
    app_version: str = "1.0.0"
    frontend_port: int = 8080
    debug_mode: bool = False

    # URL the frontend server uses to reach the API (internal Docker network URL in prod)
    api_base_url: str = "http://localhost:9000/payroll/api/v1"


@lru_cache
def get_frontend_settings() -> FrontendSettings:
    return FrontendSettings()


frontend_settings = get_frontend_settings()
