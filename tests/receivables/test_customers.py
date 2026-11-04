import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.receivables.customers import CustomerService
from payroll.receivables.exceptions import CustomerConflictError, InvalidCustomerError


def test_create_customer_defaults_terms_30(session, owner_user):
    customer = CustomerService(session).create(actor=owner_user, name="  Globex  ")
    assert customer.name == "Globex"
    assert customer.terms_days == 30
    assert customer.is_active
    assert customer.email is None


def test_name_unique_case_insensitive_409(session, owner_user):
    service = CustomerService(session)
    service.create(actor=owner_user, name="Globex")
    with pytest.raises(CustomerConflictError) as err:
        service.create(actor=owner_user, name="GLOBEX ")
    assert err.value.http_code == 409
    other = service.create(actor=owner_user, name="Initech")
    with pytest.raises(CustomerConflictError):
        service.update(
            other.id,
            actor=owner_user,
            name="globex",
            email=None,
            phone=None,
            billing_address=None,
            terms_days=30,
        )
    # Renaming a customer to its own name (any case) is fine.
    service.update(
        other.id,
        actor=owner_user,
        name="INITECH",
        email=None,
        phone=None,
        billing_address=None,
        terms_days=45,
    )
    assert service.get(other.id).terms_days == 45


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "   "},
        {"name": "x" * 201},
        {"email": "no-at-sign"},
        {"email": "a@b@c.com"},
        {"email": "a b@c.com"},
        {"email": "@c.com"},
        {"email": "a@"},
        {"email": "a" * 195 + "@b.com"},
        {"phone": "1" * 51},
        {"billing_address": "x" * 501},
        {"terms_days": -1},
        {"terms_days": 366},
        {"terms_days": True},
        {"terms_days": "30"},
    ],
)
def test_rejects_bad_email_and_terms_and_long_text(session, owner_user, fields):
    kwargs = {"name": "Acme", **fields}
    with pytest.raises(InvalidCustomerError) as err:
        CustomerService(session).create(actor=owner_user, **kwargs)
    assert err.value.http_code == 422


def test_accepts_limits(session, owner_user):
    service = CustomerService(session)
    service.create(actor=owner_user, name="x" * 200, email="a@b.co", phone="1" * 50, terms_days=0)
    service.create(actor=owner_user, name="y", billing_address="z" * 500, terms_days=365)


def test_deactivate_hides_from_default_list(session, owner_user):
    service = CustomerService(session)
    a = service.create(actor=owner_user, name="Alpha")
    service.create(actor=owner_user, name="Beta")
    service.set_active(a.id, False, actor=owner_user)
    assert [c.name for c in service.list()] == ["Beta"]
    assert [c.name for c in service.list(include_inactive=True)] == ["Alpha", "Beta"]
    service.set_active(a.id, True, actor=owner_user)
    assert len(service.list()) == 2


def test_owner_only_and_audited(session, owner_user, viewer_user, accountant_user):
    service = CustomerService(session)
    customer = service.create(actor=owner_user, name="Alpha")
    for actor in (viewer_user, accountant_user):
        with pytest.raises(PermissionDeniedError):
            service.create(actor=actor, name="Nope")
        with pytest.raises(PermissionDeniedError):
            service.update(
                customer.id,
                actor=actor,
                name="Nope",
                email=None,
                phone=None,
                billing_address=None,
                terms_days=30,
            )
        with pytest.raises(PermissionDeniedError):
            service.set_active(customer.id, False, actor=actor)
    service.update(
        customer.id,
        actor=owner_user,
        name="Alpha 2",
        email="a@b.co",
        phone=None,
        billing_address=None,
        terms_days=10,
    )
    service.set_active(customer.id, False, actor=owner_user)
    actions = [
        row.action
        for row in session.exec(
            select(AuditLog).where(AuditLog.entity == "customer").order_by(AuditLog.id)
        )
    ]
    assert actions == ["customer.create", "customer.update", "customer.activate"]
