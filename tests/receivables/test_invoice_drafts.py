"""Invoice drafts: the service rules and the invoice pages."""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported by conftest and used as an argument)

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.auth.permissions import PermissionDeniedError
from payroll.ledger.models import JournalEntry
from payroll.receivables.exceptions import (
    InvalidInvoiceError,
    InvoiceNotFoundError,
    InvoiceStateError,
)
from payroll.receivables.invoices import InvoiceLineInput, InvoiceService, line_amount
from payroll.receivables.models import Customer, Invoice, InvoiceLine
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
XSS = "<script>alert(1)</script>"
DAY = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, customer, item):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


def _line(item, qty="1", price=None, description="Work"):
    return InvoiceLineInput(
        item_id=item.id,
        description=description,
        qty=D(qty),
        unit_price=D(price) if price is not None else item.unit_price,
    )


def _draft(session, owner_user, customer, item, **kwargs):
    kwargs.setdefault("invoice_date", DAY)
    kwargs.setdefault("lines", [_line(item, "2")])
    return InvoiceService(session).create_draft(actor=owner_user, customer_id=customer.id, **kwargs)


def test_line_amount_rounds_half_up():
    assert line_amount(D("0.333"), D("10.00")) == D("3.33")
    assert line_amount(D("0.5"), D("0.01")) == D("0.01")
    assert line_amount(D("2.5"), D("1.01")) == D("2.53")


def test_create_draft_computes_total_and_due_date(session, owner_user, customer, item):
    invoice = _draft(session, owner_user, customer, item)
    assert invoice.status == "draft"
    assert invoice.total == D("300.00")
    assert invoice.terms_days == 30
    assert invoice.due_date == date(2026, 3, 31)
    lines = InvoiceService(session).lines(invoice.id)
    assert [(ln.line_no, ln.qty, ln.amount) for ln in lines] == [(1, D("2.000"), D("300.00"))]
    assert lines[0].income_account_id == item.income_account_id

    other = _draft(session, owner_user, customer, item, terms_days=45, memo="  Net 45 ")
    assert other.terms_days == 45
    assert other.due_date == date(2026, 4, 15)
    assert other.memo == "Net 45"

    zero_terms = _draft(session, owner_user, customer, item, terms_days=0)
    assert zero_terms.due_date == DAY


def test_description_defaults_to_item(session, owner_user, customer, item):
    item.description = "Hourly consulting"
    session.add(item)
    invoice = _draft(session, owner_user, customer, item, lines=[_line(item, description="  ")])
    assert InvoiceService(session).lines(invoice.id)[0].description == "Hourly consulting"
    item.description = None
    session.add(item)
    invoice = _draft(session, owner_user, customer, item, lines=[_line(item, description="")])
    assert InvoiceService(session).lines(invoice.id)[0].description == "Consulting"


def test_draft_has_no_number_entry_or_ledger_effect(session, owner_user, customer, item):
    before = session.exec(select(func.count()).select_from(JournalEntry)).one()
    invoice = _draft(session, owner_user, customer, item)
    assert invoice.number is None and invoice.entry_id is None
    assert session.exec(select(func.count()).select_from(JournalEntry)).one() == before


@pytest.mark.parametrize(
    "case",
    [
        "zero_lines",
        "over_100_lines",
        "zero_qty",
        "negative_qty",
        "negative_price",
        "three_decimal_price",
        "four_decimal_qty",
        "huge_qty",
        "huge_total",
        "zero_total",
        "long_description",
        "bad_terms",
        "long_memo",
    ],
)
def test_rejects_zero_lines_over_100_lines_zero_qty_negative_price_three_decimal_price_four_decimal_qty_huge_total(  # noqa: E501
    session, owner_user, customer, item, case
):
    kwargs = {}
    lines = {
        "zero_lines": [],
        "over_100_lines": [_line(item, "1", "1.00") for _ in range(101)],
        "zero_qty": [_line(item, "0")],
        "negative_qty": [_line(item, "-1")],
        "negative_price": [_line(item, "1", "-0.01")],
        "three_decimal_price": [_line(item, "1", "1.001")],
        "four_decimal_qty": [_line(item, "1.2345")],
        "huge_qty": [_line(item, "1000000", "1.00")],
        "huge_total": [_line(item, "999999.999", "999999999999.99")],
        "zero_total": [_line(item, "1", "0.00")],
        "long_description": [_line(item, description="x" * 501)],
    }.get(case)
    if case == "bad_terms":
        kwargs["terms_days"] = 366
    if case == "long_memo":
        kwargs["memo"] = "m" * 501
    if lines is not None:
        kwargs["lines"] = lines
    with pytest.raises(InvalidInvoiceError) as err:
        _draft(session, owner_user, customer, item, **kwargs)
    assert err.value.http_code == 422


def test_accepts_exactly_100_lines_and_limits(session, owner_user, customer, item):
    lines = [_line(item, "1", "1.00") for _ in range(100)]
    invoice = _draft(session, owner_user, customer, item, lines=lines)
    assert invoice.total == D("100.00")
    big = _draft(session, owner_user, customer, item, lines=[_line(item, "1", "999999999999.99")])
    assert big.total == D("999999999999.99")
    edge = _draft(session, owner_user, customer, item, lines=[_line(item, "999999.999", "1.00")])
    assert edge.total == D("1000000.00")
    free = _draft(
        session,
        owner_user,
        customer,
        item,
        lines=[_line(item, "1", "0.00"), _line(item, "1", "5.00")],
    )
    assert free.total == D("5.00")


def test_rejects_inactive_customer_and_inactive_item(session, owner_user, customer, item):
    item.is_active = False
    session.add(item)
    with pytest.raises(InvalidInvoiceError) as err:
        _draft(session, owner_user, customer, item)
    assert "item" in err.value.message.lower()
    item.is_active = True
    customer.is_active = False
    session.add_all([item, customer])
    with pytest.raises(InvalidInvoiceError) as err:
        _draft(session, owner_user, customer, item)
    assert "customer" in err.value.message.lower()


def test_rejects_unknown_customer_and_item(session, owner_user, customer, item):
    with pytest.raises(InvalidInvoiceError):
        InvoiceService(session).create_draft(
            actor=owner_user, customer_id=999999, invoice_date=DAY, lines=[_line(item)]
        )
    ghost = InvoiceLineInput(item_id=999999, description="x", qty=D("1"), unit_price=D("1"))
    with pytest.raises(InvalidInvoiceError):
        _draft(session, owner_user, customer, item, lines=[ghost])


def test_rejects_date_out_of_range_and_due_date_overflow(session, owner_user, customer, item):
    for bad in (date(1899, 12, 31), date(2201, 1, 1)):
        with pytest.raises(InvalidInvoiceError):
            _draft(session, owner_user, customer, item, invoice_date=bad)
    with pytest.raises(InvalidInvoiceError) as err:
        _draft(session, owner_user, customer, item, invoice_date=date(2200, 12, 31))
    assert "due date" in err.value.message.lower()
    ok = _draft(session, owner_user, customer, item, invoice_date=date(2200, 12, 31), terms_days=0)
    assert ok.due_date == date(2200, 12, 31)


def test_update_draft_replaces_lines(session, owner_user, customer, item):
    invoice = _draft(session, owner_user, customer, item)
    other = Customer(name="Globex", terms_days=10)
    session.add(other)
    session.flush()
    updated = InvoiceService(session).update_draft(
        invoice.id,
        actor=owner_user,
        customer_id=other.id,
        invoice_date=date(2026, 4, 1),
        lines=[_line(item, "1", "10.00", "A"), _line(item, "3", "2.50", "B")],
        memo="Updated",
    )
    assert updated.id == invoice.id
    assert (updated.customer_id, updated.total, updated.terms_days) == (
        other.id,
        D("17.50"),
        10,
    )
    assert updated.due_date == date(2026, 4, 11)
    assert updated.memo == "Updated"
    lines = InvoiceService(session).lines(invoice.id)
    assert [(ln.line_no, ln.description, ln.amount) for ln in lines] == [
        (1, "A", D("10.00")),
        (2, "B", D("7.50")),
    ]
    assert session.exec(select(func.count()).select_from(InvoiceLine)).one() == 2


def test_failed_update_leaves_draft_unchanged(session, owner_user, customer, item):
    invoice = _draft(session, owner_user, customer, item)
    with pytest.raises(InvalidInvoiceError):
        InvoiceService(session).update_draft(
            invoice.id,
            actor=owner_user,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[],
        )
    session.expire_all()
    assert InvoiceService(session).get(invoice.id).total == D("300.00")
    assert len(InvoiceService(session).lines(invoice.id)) == 1


def test_discard_draft_sets_void_without_number(session, owner_user, customer, item):
    invoice = _draft(session, owner_user, customer, item)
    discarded = InvoiceService(session).discard_draft(invoice.id, actor=owner_user)
    assert discarded.status == "void"
    assert discarded.number is None and discarded.entry_id is None
    assert InvoiceService(session).list(status="draft") == []
    assert [i.id for i in InvoiceService(session).list(status="void")] == [invoice.id]


def test_cannot_edit_or_discard_non_draft(session, owner_user, customer, item):
    service = InvoiceService(session)
    voided = _draft(session, owner_user, customer, item)
    service.discard_draft(voided.id, actor=owner_user)
    with pytest.raises(InvoiceStateError):
        service.discard_draft(voided.id, actor=owner_user)
    with pytest.raises(InvoiceStateError):
        service.update_draft(
            voided.id,
            actor=owner_user,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[_line(item)],
        )
    # An open invoice (inserted directly; issuing arrives with the posting task).
    entry = JournalEntry(
        entry_no=900001,
        entry_date=DAY,
        source_type="invoice",
        source_id="0",
        status="posted",
        posted_by=owner_user.id,
    )
    session.add(entry)
    session.flush()
    open_row = Invoice(
        number=5000,
        customer_id=customer.id,
        invoice_date=DAY,
        due_date=DAY,
        status="open",
        total=D("10.00"),
        entry_id=entry.id,
    )
    session.add(open_row)
    session.commit()  # a failing service call rolls back; keep the row
    with pytest.raises(InvoiceStateError):
        service.discard_draft(open_row.id, actor=owner_user)
    with pytest.raises(InvoiceStateError):
        service.update_draft(
            open_row.id,
            actor=owner_user,
            customer_id=customer.id,
            invoice_date=DAY,
            lines=[_line(item)],
        )


def test_missing_invoice_404(session, owner_user):
    with pytest.raises(InvoiceNotFoundError):
        InvoiceService(session).get(424242)


def test_owner_only_and_audited(session, owner_user, viewer_user, accountant_user, customer, item):
    service = InvoiceService(session)
    for actor in (viewer_user, accountant_user):
        with pytest.raises(PermissionDeniedError):
            service.create_draft(
                actor=actor, customer_id=customer.id, invoice_date=DAY, lines=[_line(item)]
            )
    invoice = _draft(session, owner_user, customer, item)
    for actor in (viewer_user, accountant_user):
        with pytest.raises(PermissionDeniedError):
            service.update_draft(
                invoice.id,
                actor=actor,
                customer_id=customer.id,
                invoice_date=DAY,
                lines=[_line(item)],
            )
        with pytest.raises(PermissionDeniedError):
            service.discard_draft(invoice.id, actor=actor)
    service.update_draft(
        invoice.id,
        actor=owner_user,
        customer_id=customer.id,
        invoice_date=DAY,
        lines=[_line(item, "1")],
    )
    service.discard_draft(invoice.id, actor=owner_user)
    rows = session.exec(
        select(AuditLog).where(AuditLog.entity == "invoice").order_by(AuditLog.id)
    ).all()
    assert [r.action for r in rows] == [
        "invoice.create",
        "invoice.update",
        "invoice.discard",
    ]
    assert all(r.user_id == owner_user.id and r.entity_id == str(invoice.id) for r in rows)


def test_list_filters(session, owner_user, customer, item):
    other = Customer(name="Globex")
    session.add(other)
    session.flush()
    a = _draft(session, owner_user, customer, item)
    b = InvoiceService(session).create_draft(
        actor=owner_user, customer_id=other.id, invoice_date=DAY, lines=[_line(item)]
    )
    service = InvoiceService(session)
    service.discard_draft(a.id, actor=owner_user)
    assert {i.id for i in service.list()} == {a.id, b.id}
    assert [i.id for i in service.list(status="draft")] == [b.id]
    assert [i.id for i in service.list(customer_id=customer.id)] == [a.id]
    assert len(service.list(limit=1)) == 1
    assert len(service.list(limit=1, offset=1)) == 1
    assert service.list(limit=1, offset=2) == []


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _form(customer, item, **over):
    data = {
        "customer_id": str(customer.id),
        "invoice_date": "2026-03-01",
        "terms_days": "",
        "memo": "March work",
        "line_item": [str(item.id), str(item.id)],
        "line_description": ["Design", ""],
        "line_qty": ["2", "1.5"],
        "line_unit_price": ["100.00", "10.00"],
    }
    data.update(over)
    return data


def test_create_invoice_form_roundtrip(owner_client, session, customer, item):
    response = owner_client.post(
        "/invoices/new", data=_form(customer, item), follow_redirects=False
    )
    assert response.status_code == 303
    invoice = session.exec(select(Invoice)).one()
    assert response.headers["location"] == f"/invoices/{invoice.id}"
    assert (invoice.status, invoice.total, invoice.terms_days) == ("draft", D("215.00"), 30)
    lines = InvoiceService(session).lines(invoice.id)
    assert [ln.description for ln in lines] == ["Design", "Consulting"]

    view = owner_client.get(f"/invoices/{invoice.id}")
    assert view.status_code == 200
    assert "Design" in view.text and "$215.00" in view.text and "March work" in view.text
    assert "Draft" in view.text

    edit = owner_client.get(f"/invoices/{invoice.id}/edit")
    assert edit.status_code == 200
    assert 'value="100.00"' in edit.text and "March work" in edit.text

    saved = owner_client.post(
        f"/invoices/{invoice.id}/edit",
        data=_form(
            customer,
            item,
            line_item=[str(item.id)],
            line_description=["Only"],
            line_qty=["1"],
            line_unit_price=["5.00"],
        ),
        follow_redirects=False,
    )
    assert saved.status_code == 303
    session.refresh(invoice)
    assert invoice.total == D("5.00")

    listing = owner_client.get("/invoices")
    assert listing.status_code == 200
    assert "Acme Corp" in listing.text and "$5.00" in listing.text and "Draft" in listing.text

    discard = owner_client.post(f"/invoices/{invoice.id}/discard", follow_redirects=False)
    assert discard.status_code == 303
    session.refresh(invoice)
    assert invoice.status == "void"
    assert owner_client.get(f"/invoices/{invoice.id}/edit").status_code == 409
    assert owner_client.post(f"/invoices/{invoice.id}/discard").status_code == 409
    assert (
        owner_client.post(f"/invoices/{invoice.id}/edit", data=_form(customer, item)).status_code
        == 409
    )


def test_new_form_has_items_data_csrf_and_script(owner_client, customer, item):
    page = owner_client.get("/invoices/new")
    assert page.status_code == 200
    assert 'name="csrf_token"' in page.text
    assert f'data-price="{item.unit_price}"' in page.text
    assert 'data-description="Consulting"' in page.text
    assert "/static/js/invoice.js" in page.text
    assert "Acme Corp" in page.text
    assert "<script>" not in page.text.replace('<script src="', "")


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"line_qty": ["0", "1"]}, "Line 1"),
        ({"line_qty": ["1", "1.2345"]}, "Line 2"),
        ({"line_unit_price": ["1.001", "1"]}, "Line 1"),
        ({"line_unit_price": ["abc", "1"]}, "Line 1"),
        ({"line_item": ["", "1"]}, "Line 1"),
        ({"invoice_date": "nope"}, "date"),
        ({"customer_id": ""}, "customer"),
        ({"terms_days": "x"}, "Terms"),
    ],
)
def test_invalid_form_rerenders_422_with_typed_lines(
    owner_client, session, customer, item, over, message
):
    data = _form(customer, item, memo="Typed memo", **over)
    response = owner_client.post("/invoices/new", data=data)
    assert response.status_code == 422
    assert message.lower() in response.text.lower()
    assert "Typed memo" in response.text and "Design" in response.text
    assert session.exec(select(func.count()).select_from(Invoice)).one() == 0


def test_no_lines_rerenders_422(owner_client, session, customer, item):
    response = owner_client.post(
        "/invoices/new",
        data=_form(
            customer,
            item,
            line_item=["", ""],
            line_description=["", ""],
            line_qty=["", ""],
            line_unit_price=["", ""],
        ),
    )
    assert response.status_code == 422
    assert "at least one line" in response.text.lower()


def test_blank_rows_are_ignored(owner_client, session, customer, item):
    data = _form(
        customer,
        item,
        line_item=[str(item.id), ""],
        line_description=["Design", ""],
        line_qty=["2", ""],
        line_unit_price=["100.00", ""],
    )
    assert owner_client.post("/invoices/new", data=data, follow_redirects=False).status_code == 303
    assert len(session.exec(select(InvoiceLine)).all()) == 1


def test_line_description_xss_escaped(owner_client, session, customer, item):
    data = _form(customer, item, memo=XSS, line_description=[XSS, XSS])
    created = owner_client.post("/invoices/new", data=data, follow_redirects=False)
    invoice = session.exec(select(Invoice)).one()
    assert created.status_code == 303
    for path in (f"/invoices/{invoice.id}", f"/invoices/{invoice.id}/edit", "/invoices"):
        text = owner_client.get(path).text
        assert XSS not in text, path
    assert "&lt;script&gt;" in owner_client.get(f"/invoices/{invoice.id}").text
    bad = owner_client.post(
        "/invoices/new",
        data=_form(customer, item, memo=XSS, line_qty=["0", "1"], line_description=[XSS, XSS]),
    )
    assert bad.status_code == 422 and XSS not in bad.text


def test_list_filters_by_status_and_customer(owner_client, session, owner_user, customer, item):
    other = Customer(name="Globex")
    session.add(other)
    session.flush()
    service = InvoiceService(session)
    a = _draft(session, owner_user, customer, item)
    b = service.create_draft(
        actor=owner_user, customer_id=other.id, invoice_date=DAY, lines=[_line(item, "7")]
    )
    service.discard_draft(a.id, actor=owner_user)

    def rows(query):
        response = owner_client.get(f"/invoices{query}")
        assert response.status_code == 200
        return response.text.split("<tbody>")[1]  # the filter dropdowns list every customer

    only_draft = rows("?status=draft")
    assert "Globex" in only_draft and "Acme Corp" not in only_draft
    assert f"/invoices/{b.id}" in only_draft and "Draft" in only_draft
    only_void = rows("?status=void")
    assert "Acme Corp" in only_void and "Globex" not in only_void
    by_customer = rows(f"?customer_id={customer.id}")
    assert "Acme Corp" in by_customer and "Globex" not in by_customer
    both = rows("")
    assert "Acme Corp" in both and "Globex" in both
    assert owner_client.get("/invoices?status=bogus").status_code == 422
    assert owner_client.get("/invoices?customer_id=abc").status_code == 422


def test_viewer_reads_but_cannot_post(viewer_client, session, owner_user, customer, item):
    invoice = _draft(session, owner_user, customer, item)
    listing = viewer_client.get("/invoices")
    assert listing.status_code == 200
    assert "/invoices/new" not in listing.text
    view = viewer_client.get(f"/invoices/{invoice.id}")
    assert view.status_code == 200
    assert "/discard" not in view.text and "/edit" not in view.text
    assert viewer_client.get("/invoices/new").status_code == 403
    assert viewer_client.get(f"/invoices/{invoice.id}/edit").status_code == 403
    assert viewer_client.post("/invoices/new", data=_form(customer, item)).status_code == 403
    assert (
        viewer_client.post(f"/invoices/{invoice.id}/edit", data=_form(customer, item)).status_code
        == 403
    )
    assert viewer_client.post(f"/invoices/{invoice.id}/discard").status_code == 403
    session.refresh(invoice)
    assert invoice.status == "draft"


def test_missing_invoice_pages_404(owner_client):
    assert owner_client.get("/invoices/424242").status_code == 404
    assert owner_client.get("/invoices/424242/edit").status_code == 404
    assert owner_client.post("/invoices/424242/discard").status_code == 404


def test_invoice_js_served_and_referenced(owner_client, customer, item):
    script = owner_client.get("/static/js/invoice.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "inv-form" in script.text
    page = owner_client.get("/invoices/new")
    assert '<script src="/static/js/invoice.js"></script>' in page.text


def test_sales_nav_points_at_invoices(owner_client):
    assert 'href="/invoices"' in owner_client.get("/customers").text


def test_view_shows_quantities_without_trailing_zeros(owner_client, session, customer, item):
    owner_client.post(
        "/invoices/new",
        data=_form(
            customer,
            item,
            line_qty=["20", "1.5"],
            line_description=["A", "B"],
        ),
    )
    invoice = session.exec(select(Invoice)).one()
    text = owner_client.get(f"/invoices/{invoice.id}").text
    assert '<td class="num">20</td>' in text and '<td class="num">1.5</td>' in text
