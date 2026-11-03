"""env.py must never disable or replace the application's logging (I-1)."""

import logging.config

from alembic import command

from payroll.migrations import alembic_runner


def _spy_file_config(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def spy(fname, *args, **kwargs):
        calls.append({"fname": fname, **kwargs})

    # env.py does ``from logging.config import fileConfig`` each time it runs.
    monkeypatch.setattr(logging.config, "fileConfig", spy)
    return calls


def test_runner_config_skips_alembic_ini_logging(engine, monkeypatch):
    calls = _spy_file_config(monkeypatch)
    cfg = alembic_runner._get_alembic_config()
    assert cfg.attributes["configure_logger"] is False
    command.current(cfg)
    assert calls == []


def test_standalone_alembic_run_keeps_existing_loggers(engine, monkeypatch):
    calls = _spy_file_config(monkeypatch)
    cfg = alembic_runner._get_alembic_config()
    del cfg.attributes["configure_logger"]  # as the plain ``alembic`` command line
    command.current(cfg)
    assert len(calls) == 1
    assert calls[0]["disable_existing_loggers"] is False
