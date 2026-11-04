"""Customer, item and receivables settings pages."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported below and used as an argument)

from datetime import date
from decimal import Decimal

from sqlmodel import select

from payroll.receivables.customers import CustomerService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.items import ItemService
from payroll.receivables.models import Customer, Item, Payment, PaymentApplication
from payroll.receivables.settings import ReceivablesSettingsService
from payroll.reports.periods import company_today
from tests.ledger.conftest import chart  # noqa: F401

XSS = "<script>alert(1)</script>"


def _customer_form(**fields):
    data = {
        "name": "Acme",
        "email": "ap@acme.example",
        "phone": "555-0100",
        "billing_address": "1 Main St",
        "terms_days": "45",
    }
    data.update(fields)
    return data


def test_owner_creates_customer_and_item_via_forms(owner_client, session, chart):
    response = owner_client.post("/customers/new", data=_customer_form(), follow_redirects=False)
    assert response.status_code == 303
    customer = session.exec(select(Customer)).one()
    assert (customer.name, customer.terms_days) == ("Acme", 45)
    assert "Acme" in owner_client.get("/customers").text

    edit = owner_client.post(
        f"/customers/{customer.id}/edit",
        data=_customer_form(name="Acme Inc", terms_days="10"),
        follow_redirects=False,
    )
    assert edit.status_code == 303
    session.refresh(customer)
    assert (customer.name, customer.terms_days) == ("Acme Inc", 10)
    owner_client.post(f"/customers/{customer.id}/active", data={"active": "0"})
    assert "Acme Inc" not in owner_client.get("/customers").text
    assert "Acme Inc" in owner_client.get("/customers?inactive=1").text
    assert owner_client.get(f"/customers/{customer.id}/edit").status_code == 200
    assert owner_client.get("/customers/new").status_code == 200

    form = owner_client.get("/items/new")
    assert "Services Revenue" in form.text
    assert "Business Checking" not in form.text
    account_id = chart["Services Revenue"].id
    created = owner_client.post(
        "/items/new",
        data={
            "name": "Consulting",
            "description": "Hourly",
            "unit_price": "1,500.50",
            "income_account_id": str(account_id),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    item = session.exec(select(Item)).one()
    assert str(item.unit_price) == "1500.50"
    listing = owner_client.get("/items")
    assert "Consulting" in listing.text and "$1,500.50" in listing.text
    assert 'value="1500.50"' in owner_client.get(f"/items/{item.id}/edit").text
    updated = owner_client.post(
        f"/items/{item.id}/edit",
        data={
            "name": "Consulting",
            "description": "",
            "unit_price": "10",
            "income_account_id": str(account_id),
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    owner_client.post(f"/items/{item.id}/active", data={"active": "0"})
    assert "Consulting" not in owner_client.get("/items").text


def test_form_errors_rerender_with_typed_values_422(owner_client, session, chart):
    owner_client.post("/customers/new", data=_customer_form())
    dup = owner_client.post("/customers/new", data=_customer_form(name="ACME", phone="555-9"))
    assert dup.status_code == 422
    assert "already exists" in dup.text
    assert 'value="555-9"' in dup.text and 'value="ap@acme.example"' in dup.text
    bad_terms = owner_client.post("/customers/new", data=_customer_form(name="B", terms_days="x"))
    assert bad_terms.status_code == 422 and 'value="x"' in bad_terms.text
    bad_email = owner_client.post("/customers/new", data=_customer_form(name="B", email="nope"))
    assert bad_email.status_code == 422 and "valid email" in bad_email.text

    account_id = str(chart["Services Revenue"].id)
    bad_price = owner_client.post(
        "/items/new",
        data={
            "name": "Typed Item",
            "description": "",
            "unit_price": "1.234",
            "income_account_id": account_id,
        },
    )
    assert bad_price.status_code == 422
    assert 'value="Typed Item"' in bad_price.text and 'value="1.234"' in bad_price.text
    assert f'<option value="{account_id}" selected>' in bad_price.text
    no_account = owner_client.post(
        "/items/new",
        data={"name": "N", "description": "", "unit_price": "1", "income_account_id": ""},
    )
    assert no_account.status_code == 422 and "income account" in no_account.text
    assert session.exec(select(Item)).all() == []

    assert owner_client.get("/customers/9999/edit").status_code == 404
    assert owner_client.get("/items/9999/edit").status_code == 404
    assert owner_client.post("/customers/9999/active", data={"active": "0"}).status_code == 404
    assert owner_client.post("/items/9999/active", data={"active": "0"}).status_code == 404

    number = owner_client.post("/settings/receivables/number", data={"next_invoice_no": "-4"})
    assert number.status_code == 422 and 'value="-4"' in number.text
    company = owner_client.post(
        "/settings/receivables/company",
        data={
            "company_name": "x" * 501,
            "company_address": "",
            "remit_to": "",
            "invoice_footer": "",
        },
    )
    assert company.status_code == 422 and "at most 500" in company.text


def test_xss_in_customer_name_is_escaped(owner_client, session):
    owner_client.post("/customers/new", data=_customer_form(name=XSS))
    page = owner_client.get("/customers")
    assert XSS not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    customer = session.exec(select(Customer)).one()
    assert XSS not in owner_client.get(f"/customers/{customer.id}/edit").text
    owner_client.post(
        "/settings/receivables/company",
        data={"company_name": XSS, "company_address": "", "remit_to": "", "invoice_footer": ""},
    )
    assert XSS not in owner_client.get("/settings/receivables").text


def test_viewer_sees_lists_but_no_controls_and_gets_403_on_post(
    viewer_client,
    owner_user,
    session,
    chart,
):
    CustomerService(session).create(actor=owner_user, name="Acme")
    item = ItemService(session).create(
        actor=owner_user, name="Consulting", income_account_id=chart["Services Revenue"].id
    )
    customer = session.exec(select(Customer)).one()
    page = viewer_client.get("/customers")
    assert page.status_code == 200 and "Acme" in page.text
    assert "New customer" not in page.text and "/edit" not in page.text
    assert 'name="active"' not in page.text
    page = viewer_client.get("/items")
    assert page.status_code == 200 and "Consulting" in page.text
    assert "New item" not in page.text and "/edit" not in page.text
    for path in ("/customers/new", "/items/new", f"/customers/{customer.id}/edit"):
        assert viewer_client.get(path).status_code == 403
    posts = [
        ("/customers/new", _customer_form(name="Nope")),
        (f"/customers/{customer.id}/edit", _customer_form()),
        (f"/customers/{customer.id}/active", {"active": "0"}),
        ("/items/new", {"name": "Nope"}),
        (f"/items/{item.id}/edit", {"name": "Nope"}),
        (f"/items/{item.id}/active", {"active": "0"}),
    ]
    for path, data in posts:
        assert viewer_client.post(path, data=data).status_code == 403, path
    assert len(session.exec(select(Customer)).all()) == 1


def test_settings_page_owner_only_for_posts(
    owner_client, viewer_client, accountant_client, session
):
    page = owner_client.get("/settings/receivables")
    assert page.status_code == 200 and 'value="1001"' in page.text
    saved = owner_client.post("/settings/receivables/number", data={"next_invoice_no": "2000"})
    assert saved.status_code == 200 and "Next invoice number saved." in saved.text
    assert ReceivablesSettingsService(session).get().next_invoice_no == 2000
    saved = owner_client.post(
        "/settings/receivables/company",
        data={
            "company_name": "DCI LLC",
            "company_address": "1 Main",
            "remit_to": "PO Box 1",
            "invoice_footer": "Thanks",
        },
    )
    assert saved.status_code == 200 and "Company details saved." in saved.text
    assert 'value="DCI LLC"' in owner_client.get("/settings/receivables").text
    for client in (viewer_client, accountant_client):
        read = client.get("/settings/receivables")
        assert read.status_code == 200 and "DCI LLC" in read.text
        assert "Save number" not in read.text
        assert "Save company details" not in read.text
        assert 'name="next_invoice_no"' not in read.text
        assert 'name="company_name"' not in read.text
        assert (
            client.post("/settings/receivables/number", data={"next_invoice_no": "5"}).status_code
            == 403
        )
        assert client.post("/settings/receivables/company", data={}).status_code == 403
    assert ReceivablesSettingsService(session).get().next_invoice_no == 2000


# ---------------------------------------------------------------------------
# Issuing, replacing and voiding invoices
# ---------------------------------------------------------------------------


def _open_invoice(session, owner_user, chart, *, issue=True):
    customer = CustomerService(session).create(actor=owner_user, name="Acme")
    item = ItemService(session).create(
        actor=owner_user,
        name="Consulting",
        unit_price=Decimal("150.00"),
        income_account_id=chart["Services Revenue"].id,
    )
    service = InvoiceService(session)
    invoice = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=date(2026, 3, 1),
        lines=[InvoiceLineInput(item.id, "Work", Decimal("2"), Decimal("150.00"))],
    )
    if issue:
        invoice = service.issue(invoice.id, actor=owner_user)
    return invoice, customer, item


def test_issue_button_and_void_form_on_detail(owner_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart, issue=False)
    draft_page = owner_client.get(f"/invoices/{invoice.id}")
    assert f'action="/invoices/{invoice.id}/issue"' in draft_page.text
    assert "/void" not in draft_page.text

    issued = owner_client.post(f"/invoices/{invoice.id}/issue", follow_redirects=False)
    assert issued.status_code == 303
    assert issued.headers["location"] == f"/invoices/{invoice.id}"
    page = owner_client.get(f"/invoices/{invoice.id}")
    assert "#1001" in page.text and "Open" in page.text
    assert f'action="/invoices/{invoice.id}/void"' in page.text
    assert "/issue" not in page.text
    assert f'href="/invoices/{invoice.id}/edit"' in page.text

    # editing an open invoice replaces it, keeping the number
    edit = owner_client.get(f"/invoices/{invoice.id}/edit")
    assert edit.status_code == 200
    saved = owner_client.post(
        f"/invoices/{invoice.id}/edit",
        data={
            "customer_id": str(customer.id),
            "invoice_date": "2026-03-05",
            "terms_days": "",
            "memo": "",
            "line_item": [str(item.id)],
            "line_description": ["Smaller"],
            "line_qty": ["1"],
            "line_unit_price": ["10.00"],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    session.refresh(invoice)
    assert (invoice.number, invoice.total, invoice.invoice_date) == (
        1001,
        Decimal("10.00"),
        date(2026, 3, 5),
    )

    again = owner_client.post(f"/invoices/{invoice.id}/issue")
    assert again.status_code == 409 and "Only a draft invoice can be issued" in again.text

    voided = owner_client.post(
        f"/invoices/{invoice.id}/void",
        data={"void_date": "2026-03-20", "memo": "Duplicate"},
        follow_redirects=False,
    )
    assert voided.status_code == 303
    session.refresh(invoice)
    assert (invoice.status, invoice.void_date) == ("void", date(2026, 3, 20))
    final = owner_client.get(f"/invoices/{invoice.id}")
    assert "/void" not in final.text and "2026-03-20" in final.text


def test_void_form_defaults_to_today(owner_client, owner_user, session, chart):
    invoice, _, _ = _open_invoice(session, owner_user, chart)
    page = owner_client.get(f"/invoices/{invoice.id}")
    assert f'name="void_date" value="{company_today()}"' in page.text
    bad = owner_client.post(f"/invoices/{invoice.id}/void", data={"void_date": ""})
    assert bad.status_code == 422 and "Enter a void date" in bad.text
    session.refresh(invoice)
    assert invoice.status == "open"


def test_edit_link_hidden_for_paid_invoice(owner_client, owner_user, session, chart):
    invoice, customer, _ = _open_invoice(session, owner_user, chart)
    payment = Payment(
        customer_id=customer.id,
        received_date=date(2026, 3, 2),
        amount=Decimal("50.00"),
        deposit_account_id=chart["Business Checking"].id,
    )
    session.add(payment)
    session.flush()
    session.add(
        PaymentApplication(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=Decimal("50.00"),
            recognized_date=date(2026, 3, 2),
            ar_account_id=chart["Accounts Receivable"].id,
        )
    )
    session.commit()
    page = owner_client.get(f"/invoices/{invoice.id}")
    assert f"/invoices/{invoice.id}/edit" not in page.text
    assert f"/invoices/{invoice.id}/void" not in page.text
    assert owner_client.get(f"/invoices/{invoice.id}/edit").status_code == 409
    refused = owner_client.post(f"/invoices/{invoice.id}/void", data={"void_date": "2026-03-20"})
    assert refused.status_code == 409


def test_viewer_gets_403_on_issue_void_replace(viewer_client, owner_user, session, chart):
    draft, customer, item = _open_invoice(session, owner_user, chart, issue=False)
    open_invoice = InvoiceService(session).issue(draft.id, actor=owner_user)
    page = viewer_client.get(f"/invoices/{open_invoice.id}")
    assert page.status_code == 200
    assert "/void" not in page.text and "/edit" not in page.text
    for path, data in (
        (f"/invoices/{open_invoice.id}/issue", {}),
        (f"/invoices/{open_invoice.id}/void", {"void_date": "2026-03-20"}),
        (f"/invoices/{open_invoice.id}/edit", {"customer_id": str(customer.id)}),
    ):
        assert viewer_client.post(path, data=data).status_code == 403, path
    assert viewer_client.get(f"/invoices/{open_invoice.id}/edit").status_code == 403
    session.refresh(open_invoice)
    assert open_invoice.status == "open"
