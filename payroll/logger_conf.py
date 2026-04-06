import logging
import logging.config

from payroll import app_settings


def get_logging_config() -> dict:
    log_level = "DEBUG" if app_settings.debug_mode else "INFO"
    formatter = "json" if app_settings.api_log_type == "json" else "standard"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "json": {
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": formatter,
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            app_settings.logger_name: {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": log_level,
                "propagate": False,
            },
        },
    }


def configure_logging() -> None:
    logging.config.dictConfig(get_logging_config())
