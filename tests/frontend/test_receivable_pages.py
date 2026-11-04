"""Customer, item and receivables settings pages."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported below and used as an argument)

from sqlmodel import select

from payroll.receivables.customers import CustomerService
from payroll.receivables.items import ItemService
from payroll.receivables.models import Customer, Item
from payroll.receivables.settings import ReceivablesSettingsService
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
