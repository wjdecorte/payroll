import threading

import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from payroll.audit.models import AuditLog
from payroll.auth.models import User
from payroll.auth.permissions import PermissionDeniedError
from payroll.expenses.exceptions import (
    InvalidVendorError,
    VendorConflictError,
    VendorNotFoundError,
)
from payroll.expenses.models import Vendor
from payroll.expenses.vendors import VendorService
from payroll.migrations import alembic_runner
from tests.conftest import make_user


def test_create_trims_and_audits(session, owner_user):
    vendor = VendorService(session).create(actor=owner_user, name="  Staples  ", note=" paper ")
    assert vendor.name == "Staples"
    assert vendor.note == "paper"
    assert vendor.active is True
    actions = [a.action for a in session.exec(select(AuditLog)).all()]
    assert "vendor.create" in actions


def test_blank_note_is_none(session, owner_user):
    assert VendorService(session).create(actor=owner_user, name="A", note="   ").note is None


def test_duplicate_name_differing_in_case_conflicts(session, owner_user):
    service = VendorService(session)
    service.create(actor=owner_user, name="Staples")
    with pytest.raises(VendorConflictError) as err:
        service.create(actor=owner_user, name=" STAPLES ")
    assert err.value.http_code == 409


def test_rename_and_conflict_on_rename(session, owner_user):
    service = VendorService(session)
    a = service.create(actor=owner_user, name="Alpha")
    service.create(actor=owner_user, name="Beta")
    with pytest.raises(VendorConflictError):
        service.update(a.id, actor=owner_user, name="beta", note=None)
    renamed = service.update(a.id, actor=owner_user, name="ALPHA", note="x")
    assert (renamed.name, renamed.note) == ("ALPHA", "x")
    assert "vendor.update" in [r.action for r in session.exec(select(AuditLog)).all()]


def test_set_active_hides_from_default_list(session, owner_user):
    service = VendorService(session)
    a = service.create(actor=owner_user, name="Alpha")
    service.create(actor=owner_user, name="Beta")
    service.set_active(a.id, actor=owner_user, active=False)
    assert [v.name for v in service.list()] == ["Beta"]
    assert [v.name for v in service.list(include_inactive=True)] == ["Alpha", "Beta"]
    service.set_active(a.id, actor=owner_user, active=True)
    assert len(service.list()) == 2
    assert "vendor.set_active" in [r.action for r in session.exec(select(AuditLog)).all()]


def test_list_query_limit_offset(session, owner_user):
    service = VendorService(session)
    for name in ("Apple", "Banana", "Pineapple", "Cherry"):
        service.create(actor=owner_user, name=name)
    assert [v.name for v in service.list(query="APPLE")] == ["Apple", "Pineapple"]
    assert [v.name for v in service.list(limit=2, offset=1)] == ["Banana", "Cherry"]
    assert service.list(query="%") == []  # wildcard characters are literal


def test_get_missing_is_404(session):
    with pytest.raises(VendorNotFoundError):
        VendorService(session).get(999999)


@pytest.mark.parametrize("name", ["", "   ", "x" * 201, "bad\x00name", "tab\tname", "nl\nname"])
def test_invalid_names_refused(session, owner_user, name):
    with pytest.raises(InvalidVendorError):
        VendorService(session).create(actor=owner_user, name=name)


def test_name_of_200_is_accepted_and_long_note_refused(session, owner_user):
    service = VendorService(session)
    assert len(service.create(actor=owner_user, name="x" * 200).name) == 200
    with pytest.raises(InvalidVendorError):
        service.create(actor=owner_user, name="ok", note="n" * 501)
    with pytest.raises(InvalidVendorError):
        service.create(actor=owner_user, name="ok2", note="bad\x07")


def test_only_the_owner_mutates(session, owner_user, accountant_user, viewer_user):
    service = VendorService(session)
    vendor = service.create(actor=owner_user, name="Alpha")
    for actor in (accountant_user, viewer_user):
        with pytest.raises(PermissionDeniedError):
            service.create(actor=actor, name="Other")
        with pytest.raises(PermissionDeniedError):
            service.update(vendor.id, actor=actor, name="Other", note=None)
        with pytest.raises(PermissionDeniedError):
            service.set_active(vendor.id, actor=actor, active=False)


def test_two_racing_creates_yield_one_row_and_one_conflict(scratch_engine):
    alembic_runner.upgrade()
    with Session(scratch_engine) as db:
        user_id = make_user(db).id
        db.commit()
    engine = sa.create_engine(scratch_engine.url, pool_size=5)
    barrier = threading.Barrier(2)
    outcome: list[str] = []

    def create():
        with Session(engine) as db:
            actor = db.get(User, user_id)
            barrier.wait(timeout=30)
            try:
                VendorService(db).create(actor=actor, name="Racer")
                outcome.append("ok")
            except VendorConflictError:
                outcome.append("conflict")

    threads = [threading.Thread(target=create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        assert sorted(outcome) == ["conflict", "ok"]
        with Session(engine) as db:
            assert len(db.exec(select(Vendor)).all()) == 1
            created = [a for a in db.exec(select(AuditLog)).all() if a.action == "vendor.create"]
            assert len(created) == 1
    finally:
        engine.dispose()
