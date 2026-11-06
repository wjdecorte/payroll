import json
import logging
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll import app_settings
from payroll.audit.models import AuditLog
from payroll.auth.security import hash_password
from payroll.common.exceptions import PermissionDeniedError
from payroll.ledger.exceptions import InvalidEntryError, LockPasswordError, PeriodLockedError
from payroll.ledger.models import SourceType
from payroll.ledger.period_lock import PeriodLockService
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from tests.conftest import make_user

PASSWORD = "Correct-Horse-Battery-9!"
WRONG = "Wrong-Horse-Battery-7?"
PREFIX = "/payroll/api/v1/ledger"
MARCH_31 = date(2026, 3, 31)


@pytest.fixture
def owner(session):
    return make_user(session, email="lock.owner@example.com", password_hash=hash_password(PASSWORD))


@pytest.fixture
def app_log(caplog):
    """caplog also attached to the app logger, which does not propagate to the root."""
    app_logger = logging.getLogger(app_settings.logger_name)
    app_logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger=app_settings.logger_name)
    yield caplog
    app_logger.removeHandler(caplog.handler)


def _logged(caplog) -> str:
    """Everything captured, including ``extra`` attributes such as a logged request body."""
    return caplog.text + "".join(str(r.__dict__) for r in caplog.records)


def _audit(session, action: str) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    return list(session.exec(stmt).all())


def _post(session, chart, owner, day: date):
    ids = sorted(a.id for a in chart.values())
    draft = EntryDraft(
        day, [LineDraft(ids[0], debit=Decimal("5.00")), LineDraft(ids[1], credit=Decimal("5.00"))]
    )
    return LedgerPostingService(session).post(draft, actor=owner)


def test_set_requires_correct_password(session, owner):
    svc = PeriodLockService(session)
    assert svc.get() is None
    svc.set(MARCH_31, password=PASSWORD, actor=owner)
    assert svc.get() == MARCH_31
    (row,) = _audit(session, "ledger.lock.set")
    assert row.user_id == owner.id
    assert row.before == {"lock_date": None}
    assert row.after == {"lock_date": "2026-03-31"}


def test_wrong_password_is_audited_and_changes_nothing(session, owner):
    svc = PeriodLockService(session)
    with pytest.raises(LockPasswordError) as info:
        svc.set(MARCH_31, password=WRONG, actor=owner)
    assert info.value.http_code == 403
    assert svc.get() is None
    assert _audit(session, "ledger.lock.set") == []
    (denied,) = _audit(session, "ledger.lock.denied")
    assert denied.user_id == owner.id


def test_set_lock_blocks_posting_on_and_before_date(session, owner, chart):
    PeriodLockService(session).set(MARCH_31, password=PASSWORD, actor=owner)
    for day in (MARCH_31, date(2026, 1, 15)):
        with pytest.raises(PeriodLockedError):
            _post(session, chart, owner, day)
    assert _post(session, chart, owner, date(2026, 4, 1)).entry_no == 1


def test_lock_can_be_moved_earlier_and_cleared_with_password(session, owner, chart):
    svc = PeriodLockService(session)
    svc.set(MARCH_31, password=PASSWORD, actor=owner)
    svc.set(date(2026, 1, 31), password=PASSWORD, actor=owner)
    assert svc.get() == date(2026, 1, 31)
    assert _post(session, chart, owner, date(2026, 2, 1)).entry_no == 1
    with pytest.raises(LockPasswordError):
        svc.set(None, password=WRONG, actor=owner)
    assert svc.get() == date(2026, 1, 31)
    svc.set(None, password=PASSWORD, actor=owner)
    assert svc.get() is None
    assert _post(session, chart, owner, date(2026, 1, 2)).entry_no == 2
    rows = _audit(session, "ledger.lock.set")
    assert [r.after["lock_date"] for r in rows] == ["2026-03-31", "2026-01-31", None]
    assert rows[1].before == {"lock_date": "2026-03-31"}


def test_non_owner_cannot_set(session, accountant_user):
    accountant_user.password_hash = hash_password(PASSWORD)
    svc = PeriodLockService(session)
    with pytest.raises(PermissionDeniedError):
        svc.set(MARCH_31, password=PASSWORD, actor=accountant_user)
    assert svc.get() is None
    assert _audit(session, "ledger.lock.set") == []


def test_password_not_in_audit_row_or_logs(session, owner, app_log):
    caplog = app_log
    svc = PeriodLockService(session)
    with pytest.raises(LockPasswordError):
        svc.set(MARCH_31, password=WRONG, actor=owner)
    svc.set(MARCH_31, password=PASSWORD, actor=owner)
    dumped = json.dumps(
        [
            (r.before, r.after, r.action)
            for a in ("denied", "set")
            for r in _audit(session, f"ledger.lock.{a}")
        ]
    )
    for secret in (PASSWORD, WRONG, owner.password_hash):
        assert secret not in dumped
        assert secret not in caplog.text
        assert secret not in _logged(caplog)


def test_failure_rolls_back_so_no_lock_outlives_the_call(session, owner, monkeypatch):
    svc = PeriodLockService(session)

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr("payroll.ledger.period_lock.record_audit", boom)
    with pytest.raises(RuntimeError):
        svc.set(MARCH_31, password=PASSWORD, actor=owner)
    assert svc.get() is None


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


@pytest.fixture
def owner_api(db_override, session):
    from tests.conftest import _signed_in_client

    user = make_user(session, email="api.owner@example.com", password_hash=hash_password(PASSWORD))
    with _signed_in_client(user) as client:
        yield client


def test_get_is_open_to_any_role(viewer_client):
    response = viewer_client.get(f"{PREFIX}/period-lock")
    assert response.status_code == 200
    assert response.json() == {"lockDate": None}


def test_owner_puts_lock_and_gets_it_back(owner_api, viewer_client):
    response = owner_api.put(
        f"{PREFIX}/period-lock", json={"lockDate": "2026-03-31", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json() == {"lockDate": "2026-03-31"}
    assert viewer_client.get(f"{PREFIX}/period-lock").json() == {"lockDate": "2026-03-31"}
    cleared = owner_api.put(f"{PREFIX}/period-lock", json={"lockDate": None, "password": PASSWORD})
    assert cleared.json() == {"lockDate": None}


def test_wrong_password_is_403_and_audited(owner_api, session):
    response = owner_api.put(
        f"{PREFIX}/period-lock", json={"lockDate": "2026-03-31", "password": WRONG}
    )
    assert response.status_code == 403
    assert response.json()["errors"][0]["type"] == "LockPasswordError"
    assert WRONG not in response.text
    assert len(_audit(session, "ledger.lock.denied")) == 1
    assert owner_api.get(f"{PREFIX}/period-lock").json() == {"lockDate": None}


def test_accountant_cannot_put(accountant_client):
    response = accountant_client.put(
        f"{PREFIX}/period-lock", json={"lockDate": "2026-03-31", "password": PASSWORD}
    )
    assert response.status_code == 403


def test_validation_error_does_not_echo_password(owner_api, app_log):
    caplog = app_log
    response = owner_api.put(f"{PREFIX}/period-lock", json={"password": WRONG})
    assert response.status_code == 422
    assert WRONG not in response.text
    assert WRONG not in caplog.text
    assert WRONG not in _logged(caplog)
    bad = owner_api.put(f"{PREFIX}/period-lock", json={"lockDate": "nope", "password": WRONG})
    assert bad.status_code == 422
    assert WRONG not in bad.text


def test_put_does_not_log_password(owner_api, app_log):
    caplog = app_log
    owner_api.put(f"{PREFIX}/period-lock", json={"lockDate": "2026-03-31", "password": PASSWORD})
    assert PASSWORD not in caplog.text
    assert PASSWORD not in _logged(caplog)


@pytest.mark.parametrize("bad", [date(9999, 12, 31), date(1899, 12, 31), date(2201, 1, 1)])
def test_lock_date_outside_supported_range_is_422(session, owner, bad):
    svc = PeriodLockService(session)
    with pytest.raises(InvalidEntryError) as info:
        svc.set(bad, password=PASSWORD, actor=owner)
    assert info.value.http_code == 422
    assert svc.get() is None
    assert _audit(session, "ledger.lock.set") == []


def test_lock_api_and_form_reject_unsupported_date(owner_client, owner_user, session):
    owner_user.password_hash = hash_password(PASSWORD)
    session.flush()
    resp = owner_client.put(
        f"{PREFIX}/period-lock", json={"lock_date": "9999-12-31", "password": PASSWORD}
    )
    assert resp.status_code == 422
    form = owner_client.post(
        "/settings/ledger/lock", data={"lock_date": "9999-12-31", "password": PASSWORD}
    )
    assert form.status_code == 422
    assert PeriodLockService(session).get() is None


DOCUMENT_VOID_TEXT = "move the lock back (Settings > Ledger), void the document, then lock again"
MANUAL_VOID_TEXT = "Reverse it with a date after the lock instead."


@pytest.mark.parametrize(
    "source_type",
    ["invoice", "payment", "deposit", "refund", "expense", "transfer", "reimbursement"],
)
def test_void_in_locked_period_of_a_document_names_the_lock_move(
    session, owner, chart, source_type
):
    ids = sorted(a.id for a in chart.values())
    draft = EntryDraft(
        date(2026, 3, 1),
        [LineDraft(ids[0], debit=Decimal("5.00")), LineDraft(ids[1], credit=Decimal("5.00"))],
        source_type=SourceType(source_type),
    )
    svc = LedgerPostingService(session)
    entry = svc.post(draft, actor=owner)
    PeriodLockService(session).set(MARCH_31, password=PASSWORD, actor=owner)
    with pytest.raises(PeriodLockedError) as err:
        svc.void(entry.id, entry_date=date(2026, 4, 2), actor=owner)
    assert err.value.message == (
        "The books are locked through 2026-03-31; an entry dated in that period cannot be "
        f"voided. To void this {source_type}, {DOCUMENT_VOID_TEXT}."
    )


def test_void_in_locked_period_of_a_manual_entry_keeps_the_reverse_advice(session, owner, chart):
    entry = _post(session, chart, owner, date(2026, 3, 1))
    PeriodLockService(session).set(MARCH_31, password=PASSWORD, actor=owner)
    with pytest.raises(PeriodLockedError) as err:
        LedgerPostingService(session).void(entry.id, entry_date=date(2026, 4, 2), actor=owner)
    assert err.value.message == (
        "The books are locked through 2026-03-31; an entry dated in that period cannot be "
        f"voided. {MANUAL_VOID_TEXT}"
    )
