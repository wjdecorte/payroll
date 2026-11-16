"""Helpers shared by the payroll concurrency tests: lock-wait probe, worker threads, a hold."""

import threading
import time
from types import ModuleType

import sqlalchemy as sa

from payroll.exceptions import AppBaseError
from payroll.payroll_run import posting as posting_module


def wait_for_lock_waiter(engine, timeout: float = 10.0) -> None:
    """Block until some backend in the scratch database waits on a lock."""
    deadline = time.monotonic() + timeout
    with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
            if waiting:
                return
            conn.rollback()
            time.sleep(0.02)
    raise AssertionError("nothing ever waited on a lock")


def outcome_of(call) -> str:
    try:
        call()
    except AppBaseError as exc:
        return type(exc).__name__
    return "ok"


class Hold:
    """Pauses one audited action right after it posts, before it commits.

    ``module`` is the service module whose ``record_audit`` is wrapped (the payroll posting
    service by default); ``entity_id`` is the id of the document being held.
    """

    def __init__(
        self,
        monkeypatch,
        action: str,
        entity_id: int,
        module: ModuleType = posting_module,
    ) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        real_audit = module.record_audit

        def paused_audit(*args, **kwargs):
            if kwargs.get("action") == action and kwargs.get("entity_id") == str(entity_id):
                self.reached.set()  # the locks are held and the entry is posted
                assert self.release.wait(timeout=30)
            return real_audit(*args, **kwargs)

        monkeypatch.setattr(module, "record_audit", paused_audit)


def run_thread(results: dict, key: str, call) -> threading.Thread:
    thread = threading.Thread(target=lambda: results.__setitem__(key, outcome_of(call)))
    thread.start()
    return thread


def finish(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a worker is stuck (deadlock?)"
