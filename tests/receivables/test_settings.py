import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.receivables.exceptions import InvalidInvoiceError, InvoiceNumberError
from payroll.receivables.models import Invoice
from payroll.receivables.settings import ReceivablesSettingsService
from tests.receivables.conftest import JAN


def test_next_number_must_exceed_used_numbers(session, owner_user, customer):
    session.add(
        Invoice(
            number=1042,
            customer_id=customer.id,
            invoice_date=JAN,
            due_date=JAN,
            terms_days=0,
            status="void",
            total=0,
        )
    )
    session.commit()  # the service rolls back on error; keep the invoice
    service = ReceivablesSettingsService(session)
    for bad in (1042, 1000):
        with pytest.raises(InvoiceNumberError):
            service.set_next_invoice_number(bad, actor=owner_user)
    assert service.set_next_invoice_number(1043, actor=owner_user).next_invoice_no == 1043
    assert service.get().next_invoice_no == 1043
    audit = session.exec(select(AuditLog).where(AuditLog.action == "receivables.next_number")).one()
    assert audit.after == {"next_invoice_no": 1043}


@pytest.mark.parametrize("bad", [0, -5, 100_000_000, True, "12", 1.5])
def test_next_number_rejects_zero_negative_huge(session, owner_user, bad):
    service = ReceivablesSettingsService(session)
    with pytest.raises(InvoiceNumberError):
        service.set_next_invoice_number(bad, actor=owner_user)


def test_next_number_limits_and_no_invoices(session, owner_user):
    service = ReceivablesSettingsService(session)
    service.set_next_invoice_number(1, actor=owner_user)
    service.set_next_invoice_number(99_999_999, actor=owner_user)
    assert service.get().next_invoice_no == 99_999_999


def test_company_details_roundtrip(session, owner_user, viewer_user):
    service = ReceivablesSettingsService(session)
    service.update_company(
        actor=owner_user,
        company_name=" DCI LLC ",
        company_address="1 Main St\nAtlanta, GA",
        remit_to="",
        invoice_footer="Thanks!",
    )
    row = service.get()
    assert row.company_name == "DCI LLC"
    assert row.company_address == "1 Main St\nAtlanta, GA"
    assert row.remit_to is None
    assert row.invoice_footer == "Thanks!"
    with pytest.raises(InvalidInvoiceError):
        service.update_company(
            actor=owner_user,
            company_name="x" * 501,
            company_address=None,
            remit_to=None,
            invoice_footer=None,
        )
    assert service.get().company_name == "DCI LLC"
    with pytest.raises(PermissionDeniedError):
        service.update_company(
            actor=viewer_user,
            company_name="x",
            company_address=None,
            remit_to=None,
            invoice_footer=None,
        )
    with pytest.raises(PermissionDeniedError):
        service.set_next_invoice_number(5000, actor=viewer_user)
