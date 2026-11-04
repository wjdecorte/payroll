"""Customer, item and receivables settings pages."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported below and used as an argument)

from datetime import date
from decimal import Decimal

from sqlmodel import select

from payroll.receivables.customers import CustomerService
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService
from payroll.receivables.items import ItemService
from payroll.receivables.models import (
    Customer,
    CustomerRefund,
    Item,
    Payment,
    PaymentApplication,
)
from payroll.receivables.payments import ApplicationInput as ApplicationInputForPages
from payroll.receivables.payments import PaymentService
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


def test_issue_button_and_void_form_on_detail(
    owner_client, owner_user, session, chart, monkeypatch
):
    monkeypatch.setattr("payroll.receivables.balances.company_today", lambda: date(2026, 3, 5))
    invoice, customer, item = _open_invoice(session, owner_user, chart, issue=False)
    draft_page = owner_client.get(f"/invoices/{invoice.id}")
    assert f'action="/invoices/{invoice.id}/issue"' in draft_page.text
    assert "/void" not in draft_page.text

    issued = owner_client.post(f"/invoices/{invoice.id}/issue", follow_redirects=False)
    assert issued.status_code == 303
    assert issued.headers["location"] == f"/invoices/{invoice.id}"
    page = owner_client.get(f"/invoices/{invoice.id}")
    assert "#1001" in page.text and 'id="inv-status">Open<' in page.text
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


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def _invoice_for(session, owner_user, customer, item, price, day):
    service = InvoiceService(session)
    draft = service.create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=day,
        lines=[InvoiceLineInput(item.id, "Work", Decimal("1"), Decimal(price))],
    )
    return service.issue(draft.id, actor=owner_user)


def _pay_form(customer, chart, **fields):
    data = {
        "customer_id": str(customer.id),
        "received_date": "2026-03-10",
        "amount": "100.00",
        "method": "check",
        "reference": "1001",
        "memo": "",
        "deposit_account_id": str(chart["Undeposited Funds"].id),
    }
    data.update(fields)
    return data


def _applied(session):
    session.expire_all()
    return {a.invoice_id: a.amount for a in session.exec(select(PaymentApplication)).all()}


def test_record_payment_form_oldest_first(owner_client, owner_user, session, chart):
    first, customer, item = _open_invoice(session, owner_user, chart)  # 300.00, due 2026-03-31
    second = _invoice_for(session, owner_user, customer, item, "50.00", date(2026, 2, 1))
    response = owner_client.post(
        "/payments/new",
        data=_pay_form(customer, chart, amount="320.00", oldest_first="1"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    payment = session.exec(select(Payment)).one()
    assert response.headers["location"] == f"/payments/{payment.id}"
    assert _applied(session) == {second.id: Decimal("50.00"), first.id: Decimal("270.00")}
    page = owner_client.get(response.headers["location"])
    assert "1001" in page.text and "$0.00" in page.text
    assert f"/invoices/{first.id}" in page.text
    # the invoice page shows paid and balance, and links the payment
    invoice_page = owner_client.get(f"/invoices/{first.id}")
    assert 'id="inv-paid">$270.00' in invoice_page.text
    assert 'id="inv-balance">$30.00' in invoice_page.text
    assert f"/payments/{payment.id}" in invoice_page.text
    assert f"/payments/new?customer_id={customer.id}" in invoice_page.text
    assert "Acme" in owner_client.get("/payments").text


def test_typed_amounts_override_oldest_first(owner_client, owner_user, session, chart):
    first, customer, item = _open_invoice(session, owner_user, chart)
    older = _invoice_for(session, owner_user, customer, item, "50.00", date(2026, 2, 1))
    response = owner_client.post(
        "/payments/new",
        data=_pay_form(customer, chart, amount="100.00", oldest_first="1")
        | {f"apply_{first.id}": "40.00"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _applied(session) == {first.id: Decimal("40.00")}  # not the older invoice
    assert older.id not in _applied(session)
    payment = session.exec(select(Payment)).one()
    # the 60.00 left over is applied later from the payment page, oldest first
    page = owner_client.get(f"/payments/{payment.id}")
    assert f'action="/payments/{payment.id}/apply"' in page.text
    applied = owner_client.post(
        f"/payments/{payment.id}/apply", data={"oldest_first": "1"}, follow_redirects=False
    )
    assert applied.status_code == 303
    # 60.00 left: the older invoice takes 50.00; the first invoice already has a row for this
    # payment (one row per payment and invoice), so it is skipped and 10.00 stays unapplied
    assert _applied(session) == {first.id: Decimal("40.00"), older.id: Decimal("50.00")}


def test_payment_form_errors_rerender_422(owner_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)
    bad_amount = owner_client.post(
        "/payments/new", data=_pay_form(customer, chart, amount="1.234", reference="keep-me")
    )
    assert bad_amount.status_code == 422 and 'value="keep-me"' in bad_amount.text
    over = owner_client.post(
        "/payments/new",
        data=_pay_form(customer, chart, amount="900.00") | {f"apply_{invoice.id}": "400.00"},
    )
    assert over.status_code == 409 and 'value="400.00"' in over.text
    no_date = owner_client.post("/payments/new", data=_pay_form(customer, chart, received_date=""))
    assert no_date.status_code == 422 and "date received" in no_date.text
    assert session.exec(select(Payment)).all() == []
    assert owner_client.get("/payments/new?customer_id=99999").status_code == 404
    assert owner_client.get("/payments/9999").status_code == 404


def test_step_one_customer_picker_lists_open_invoices(owner_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)
    other = CustomerService(session).create(actor=owner_user, name="Zed Co")
    foreign = _invoice_for(session, owner_user, other, item, "77.00", date(2026, 3, 2))
    picker = owner_client.get("/payments/new")
    assert picker.status_code == 200
    assert "Acme" in picker.text and "Zed Co" in picker.text and 'id="pay-form"' not in picker.text
    form = owner_client.get(f"/payments/new?customer_id={customer.id}")
    assert 'id="pay-form"' in form.text
    assert f'name="apply_{invoice.id}"' in form.text and "#1001" in form.text
    assert f'name="apply_{foreign.id}"' not in form.text
    assert "$300.00" in form.text and "Undeposited Funds" in form.text
    assert 'name="oldest_first" value="1" checked' in form.text
    assert f'value="{company_today()}"' in form.text
    selected = f'<option value="{chart["Undeposited Funds"].id}" selected>'
    assert selected in form.text


def test_void_payment_form_defaults_to_today(owner_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)
    owner_client.post(
        "/payments/new", data=_pay_form(customer, chart) | {f"apply_{invoice.id}": "100.00"}
    )
    payment = session.exec(select(Payment)).one()
    page = owner_client.get(f"/payments/{payment.id}")
    assert f'name="void_date" value="{company_today()}"' in page.text
    bad = owner_client.post(f"/payments/{payment.id}/void", data={"void_date": ""})
    assert bad.status_code == 422 and "Enter a void date" in bad.text
    session.refresh(payment)
    assert payment.status == "posted"
    ok = owner_client.post(
        f"/payments/{payment.id}/void",
        data={"void_date": "2026-03-12", "memo": "Bounced"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    session.refresh(payment)
    assert (payment.status, payment.void_date) == ("void", date(2026, 3, 12))
    after = owner_client.get(f"/payments/{payment.id}")
    assert "/void" not in after.text and "2026-03-12" in after.text
    again = owner_client.post(f"/payments/{payment.id}/void", data={"void_date": "2026-03-12"})
    assert again.status_code == 409
    invoice_page = owner_client.get(f"/invoices/{invoice.id}")
    assert 'id="inv-balance">$300.00' in invoice_page.text


def test_xss_in_payment_reference_and_memo_escaped(owner_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)
    created = owner_client.post(
        "/payments/new",
        data=_pay_form(customer, chart, reference=XSS[:60], memo=XSS)
        | {f"apply_{invoice.id}": "100.00"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    payment = session.exec(select(Payment)).one()
    for url in (f"/payments/{payment.id}", f"/invoices/{invoice.id}", "/payments"):
        text = owner_client.get(url).text
        assert "<script>alert(1)" not in text
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt;" in owner_client.get(f"/payments/{payment.id}").text
    )
    bad = owner_client.post(
        "/payments/new", data=_pay_form(customer, chart, reference=XSS, memo=XSS, amount="x")
    )
    assert bad.status_code == 422 and "<script>alert(1)" not in bad.text


def test_viewer_gets_403_on_record_apply_void(viewer_client, owner_user, session, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)
    payment = PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=date(2026, 3, 10),
        amount=Decimal("100.00"),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="cash",
    )
    page = viewer_client.get(f"/payments/{payment.id}")
    assert page.status_code == 200
    assert "/void" not in page.text and "/apply" not in page.text
    assert viewer_client.get("/payments").status_code == 200
    assert viewer_client.get("/payments/new").status_code == 403
    for path, data in (
        ("/payments/new", _pay_form(customer, chart)),
        (f"/payments/{payment.id}/apply", {f"apply_{invoice.id}": "10.00"}),
        (f"/payments/{payment.id}/void", {"void_date": "2026-03-12"}),
    ):
        assert viewer_client.post(path, data=data).status_code == 403
    session.expire_all()
    assert session.exec(select(PaymentApplication)).all() == []
    assert session.get(Payment, payment.id).status == "posted"
    invoice_page = viewer_client.get(f"/invoices/{invoice.id}")
    assert "Receive payment" not in invoice_page.text


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------


def _undeposited_payment(session, owner_user, customer, chart, amount, day=date(2026, 3, 10), **kw):
    return PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=day,
        amount=Decimal(amount),
        deposit_account_id=kw.pop("account", chart["Undeposited Funds"]).id,
        method="check",
        **kw,
    )


def _deposit_form_data(chart, *payments, **fields):
    data = {
        "deposit_date": "2026-03-12",
        "bank_account_id": str(chart["Business Checking"].id),
        "memo": "Friday",
        "payment_id": [str(p.id) for p in payments],
    }
    data.update(fields)
    return data


def test_new_deposit_page_lists_only_available_payments(owner_client, owner_user, session, chart):
    customer = Customer(name="Zed Customer")
    session.add(customer)
    session.flush()
    shown = _undeposited_payment(session, owner_user, customer, chart, "111.11", reference="SHOWN")
    _undeposited_payment(
        session,
        owner_user,
        customer,
        chart,
        "222.22",
        reference="DIRECT",
        account=chart["Business Checking"],
    )
    voided = _undeposited_payment(session, owner_user, customer, chart, "333.33", reference="VOID")
    PaymentService(session).void(voided.id, actor=owner_user, void_date=date(2026, 3, 11))
    page = owner_client.get("/deposits/new")
    assert page.status_code == 200
    assert "SHOWN" in page.text and "Zed Customer" in page.text
    assert f'value="{shown.id}"' in page.text and 'data-cents="11111"' in page.text
    assert "DIRECT" not in page.text and "VOID" not in page.text
    # Undeposited Funds is not offered as the bank; the real bank is
    assert "Business Checking" in page.text
    assert '<option value="' + str(chart["Undeposited Funds"].id) + '"' not in page.text
    assert "/static/js/deposit.js" in page.text and 'id="dep-save"' in page.text


def test_create_via_form_redirects_to_view(owner_client, owner_user, session, chart):
    customer = Customer(name="Acme")
    session.add(customer)
    session.flush()
    one = _undeposited_payment(session, owner_user, customer, chart, "100.00", reference="A1")
    two = _undeposited_payment(session, owner_user, customer, chart, "50.50", reference="A2")
    response = owner_client.post(
        "/deposits/new", data=_deposit_form_data(chart, one, two), follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/deposits/")
    page = owner_client.get(location)
    assert page.status_code == 200
    assert "$150.50" in page.text and "A1" in page.text and "A2" in page.text
    assert "Business Checking" in page.text and "Void deposit" in page.text
    listing = owner_client.get("/deposits").text
    assert "Friday" in listing and "$150.50" in listing and "Business Checking" in listing
    # the payments left the form
    assert "A1" not in owner_client.get("/deposits/new").text

    deposit_id = int(location.rsplit("/", 1)[1])
    voided = owner_client.post(
        f"{location}/void", data={"void_date": "2026-03-13"}, follow_redirects=False
    )
    assert voided.status_code == 303
    page = owner_client.get(location)
    assert "Void deposit" not in page.text and "released" in page.text
    assert "A1" in owner_client.get("/deposits/new").text
    assert deposit_id


def test_deposit_form_errors_rerender_422_with_typed_values(
    owner_client, owner_user, session, chart
):
    customer = Customer(name="Acme")
    session.add(customer)
    session.flush()
    payment = _undeposited_payment(session, owner_user, customer, chart, "10.00")
    empty = owner_client.post("/deposits/new", data=_deposit_form_data(chart, memo="keep me"))
    assert empty.status_code == 422 and "at least one payment" in empty.text
    assert 'value="keep me"' in empty.text
    early = owner_client.post(
        "/deposits/new", data=_deposit_form_data(chart, payment, deposit_date="2026-03-01")
    )
    assert early.status_code == 422 and "after the deposit date" in early.text
    assert f'value="{payment.id}" data-cents="1000" aria-label' in early.text
    assert "checked" in early.text
    for bad in (
        {"bank_account_id": ""},
        {"deposit_date": ""},
        {"deposit_date": "nope"},
        {"payment_id": ["abc"]},
    ):
        assert owner_client.post(
            "/deposits/new", data=_deposit_form_data(chart, **bad)
        ).status_code in (
            422,
            400,
        )
    missing = owner_client.post("/deposits/999999/void", data={"void_date": "2026-03-13"})
    assert missing.status_code == 404
    assert owner_client.get("/deposits/999999").status_code == 404


def test_deposit_void_error_rerenders_view(owner_client, owner_user, session, chart):
    customer = Customer(name="Acme")
    session.add(customer)
    session.flush()
    payment = _undeposited_payment(session, owner_user, customer, chart, "10.00")
    created = owner_client.post(
        "/deposits/new", data=_deposit_form_data(chart, payment), follow_redirects=False
    )
    location = created.headers["location"]
    owner_client.post(f"{location}/void", data={"void_date": "2026-03-13"})
    again = owner_client.post(f"{location}/void", data={"void_date": "2026-03-13"})
    assert again.status_code == 409 and "already void" in again.text
    blank = owner_client.post(f"{location}/void", data={"void_date": ""})
    assert blank.status_code == 422


def test_deposit_js_served(owner_client):
    response = owner_client.get("/static/js/deposit.js")
    assert response.status_code == 200
    assert "BigInt" in response.text and "dep-total" in response.text
    assert "parseFloat" not in response.text


def test_viewer_gets_403_on_deposit_mutations(viewer_client, owner_user, session, chart):
    customer = Customer(name="Acme")
    session.add(customer)
    session.flush()
    payment = _undeposited_payment(session, owner_user, customer, chart, "10.00")
    assert viewer_client.get("/deposits").status_code == 200
    assert viewer_client.get("/deposits/new").status_code == 403
    assert (
        viewer_client.post("/deposits/new", data=_deposit_form_data(chart, payment)).status_code
        == 403
    )
    session.expire_all()
    assert session.get(Payment, payment.id).deposit_id is None


# --- customer page and refunds ------------------------------------------------------------


def _refund_form(chart, **fields):
    data = {
        "refund_date": "2026-03-20",
        "amount": "40.00",
        "method": "check",
        "reference": "R-1",
        "memo": "",
        "bank_account_id": str(chart["Business Checking"].id),
    }
    data.update(fields)
    return data


def _customer_with_credit(session, owner_user, chart):
    invoice, customer, item = _open_invoice(session, owner_user, chart)  # 300.00, due 2026-03-31
    PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=date(2026, 3, 10),
        amount=Decimal("350.00"),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=[ApplicationInputForPages(invoice.id, Decimal("100.00"))],
    )
    return invoice, customer


def test_customer_page_shows_credit_and_open_invoices(owner_client, owner_user, session, chart):
    invoice, customer = _customer_with_credit(session, owner_user, chart)
    page = owner_client.get(f"/customers/{customer.id}")
    assert page.status_code == 200
    assert "Acme" in page.text
    assert 'id="credit-balance">$250.00' in page.text
    assert f'href="/invoices/{invoice.id}">#1001' in page.text
    assert "$200.00" in page.text  # the invoice balance: 300.00 less 100.00 applied
    assert "$350.00" in page.text  # the payment
    assert f'href="/payments/{session.exec(select(Payment)).one().id}"' in page.text
    assert "Apply credit" in page.text  # 250.00 of it is unapplied
    assert 'id="refund-form"' in page.text and "Refund credit" in page.text
    assert f'href="/invoices/new?customer_id={customer.id}"' in page.text
    assert f'href="/payments/new?customer_id={customer.id}"' in page.text
    assert owner_client.get("/customers").text.count(f'href="/customers/{customer.id}"') == 1
    assert owner_client.get("/customers/999999").status_code == 404


def test_refund_form_roundtrip_and_422(owner_client, owner_user, session, chart):
    _, customer = _customer_with_credit(session, owner_user, chart)
    url = f"/customers/{customer.id}/refund"
    done = owner_client.post(
        url, data=_refund_form(chart, amount="1,000.00"), follow_redirects=False
    )
    assert done.status_code == 422  # more than the 250.00 of credit
    assert "Only 250.00 of credit" in done.text
    assert 'value="1,000.00"' in done.text and 'value="R-1"' in done.text  # typed values kept

    for bad in (
        _refund_form(chart, amount="abc"),
        _refund_form(chart, amount="1.005"),
        _refund_form(chart, refund_date=""),
        _refund_form(chart, bank_account_id=""),
        _refund_form(chart, method="bitcoin"),
    ):
        assert owner_client.post(url, data=bad).status_code == 422, bad
    assert session.exec(select(CustomerRefund)).all() == []

    ok = owner_client.post(url, data=_refund_form(chart, amount="40.00"), follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == f"/customers/{customer.id}"
    refund = session.exec(select(CustomerRefund)).one()
    assert (refund.amount, refund.reference, refund.status) == (Decimal("40.00"), "R-1", "posted")
    page = owner_client.get(f"/customers/{customer.id}")
    assert 'id="credit-balance">$210.00' in page.text
    assert "R-1" in page.text and "Business Checking" in page.text
    assert (
        owner_client.post("/customers/999999/refund", data=_refund_form(chart)).status_code == 404
    )


def test_refund_page_escapes_user_text(owner_client, owner_user, session, chart):
    _, customer = _customer_with_credit(session, owner_user, chart)
    owner_client.post(
        f"/customers/{customer.id}/refund", data=_refund_form(chart, reference=XSS, memo=XSS)
    )
    page = owner_client.get(f"/customers/{customer.id}")
    assert XSS not in page.text and "&lt;script&gt;" in page.text


def test_viewer_reads_customer_page_but_cannot_refund(
    viewer_client, accountant_client, owner_user, session, chart
):
    _, customer = _customer_with_credit(session, owner_user, chart)
    for client in (viewer_client, accountant_client):
        page = client.get(f"/customers/{customer.id}")
        assert page.status_code == 200
        assert 'id="credit-balance">$250.00' in page.text
        assert "refund-form" not in page.text and "Refund credit" not in page.text
        assert "/invoices/new" not in page.text and "Apply credit" not in page.text
        assert (
            client.post(f"/customers/{customer.id}/refund", data=_refund_form(chart)).status_code
            == 403
        )
    assert session.exec(select(CustomerRefund)).all() == []


# ---------------------------------------------------------------------------
# Derived invoice status, paid and balance
# ---------------------------------------------------------------------------


def test_list_shows_derived_status_and_filters(
    owner_client, owner_user, session, chart, monkeypatch
):
    monkeypatch.setattr("payroll.receivables.balances.company_today", lambda: date(2026, 4, 15))
    customer = CustomerService(session).create(actor=owner_user, name="Acme")
    item = ItemService(session).create(
        actor=owner_user,
        name="Consulting",
        unit_price=Decimal("100.00"),
        income_account_id=chart["Services Revenue"].id,
    )
    late = _invoice_for(session, owner_user, customer, item, "100.00", date(2026, 3, 1))
    partial = _invoice_for(session, owner_user, customer, item, "100.00", date(2026, 4, 10))
    paid = _invoice_for(session, owner_user, customer, item, "60.00", date(2026, 4, 10))
    open_ = _invoice_for(session, owner_user, customer, item, "10.00", date(2026, 4, 12))
    draft = InvoiceService(session).create_draft(
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=date(2026, 4, 12),
        lines=[InvoiceLineInput(item.id, "Work", Decimal("1"), Decimal("5.00"))],
    )
    PaymentService(session).record(
        actor=owner_user,
        customer_id=customer.id,
        received_date=date(2026, 4, 11),
        amount=Decimal("90.00"),
        deposit_account_id=chart["Undeposited Funds"].id,
        method="check",
        applications=[
            ApplicationInputForPages(partial.id, Decimal("30.00")),
            ApplicationInputForPages(paid.id, Decimal("60.00")),
        ],
    )
    session.commit()

    def numbers(query: str) -> list[str]:
        text = owner_client.get(f"/invoices{query}").text
        return [
            f"/invoices/{i.id}"
            for i in (late, partial, paid, open_, draft)
            if f'href="/invoices/{i.id}"' in text
        ]

    everything = owner_client.get("/invoices").text
    for badge in ("Overdue", "Partial", "Paid", "Open", "Draft"):
        assert f">{badge}</span>" in everything
    for status in ("overdue", "partial", "paid"):
        assert f'<option value="{status}"' in everything
    assert numbers("?status=overdue") == [f"/invoices/{late.id}"]
    assert numbers("?status=partial") == [f"/invoices/{partial.id}"]
    assert numbers("?status=paid") == [f"/invoices/{paid.id}"]
    assert numbers("?status=open") == [f"/invoices/{open_.id}"]
    assert numbers("?status=draft") == [f"/invoices/{draft.id}"]
    assert owner_client.get("/invoices?status=bogus").status_code == 422

    view = owner_client.get(f"/invoices/{partial.id}").text
    assert 'id="inv-status">Partial<' in view
    assert 'id="inv-paid">$30.00' in view and 'id="inv-balance">$70.00' in view
    assert 'id="inv-status">Overdue<' in owner_client.get(f"/invoices/{late.id}").text
