"""Tax payment pages: list, new, detail, void, validation as 422 pages, roles, escaping."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from sqlmodel import func, select

from payroll.ledger.models import SystemRole
from payroll.payroll_run.ledger_models import TaxPayment
from tests.frontend.test_csp_compat import _assert_csp_safe
from tests.ledger.conftest import chart  # noqa: F401
from tests.payroll_run.conftest import bank, payroll_roles  # noqa: F401

D = Decimal
IMG_XSS = "<img src=x onerror=alert(1)>"
ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.actions: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if tag == "form" and a.get("action"):
            self.actions.append(a["action"])


def anchors(html: str) -> _Anchors:
    parser = _Anchors()
    parser.feed(html)
    return parser


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles):
    session.commit()


def count(session) -> int:
    return session.exec(select(func.count()).select_from(TaxPayment)).one()


def pay_form(bank, **over):
    data = {
        "payment_date": "2026-03-05",
        "agency": "irs_941",
        "bank_account_id": str(bank.id),
        "amount": "125.50",
        "memo": "Q1 deposit",
    }
    data.update(over)
    return data


def make(owner_client, session, bank, **over) -> TaxPayment:
    response = owner_client.post(
        "/payroll/tax-payments/new", data=pay_form(bank, **over), follow_redirects=False
    )
    assert response.status_code == 303, response.text
    session.expire_all()
    return session.exec(select(TaxPayment).order_by(TaxPayment.id.desc())).first()


def test_happy_path_create_list_detail_void(owner_client, session, bank):
    session.commit()  # the sign-in session must survive a refused service call's rollback
    form = owner_client.get("/payroll/tax-payments/new")
    assert form.status_code == 200
    _assert_csp_safe(form.text)
    for agency in ("irs_941", "irs_futa", "ga_dor", "ga_dol"):
        assert f'value="{agency}"' in form.text
    response = owner_client.post(
        "/payroll/tax-payments/new", data=pay_form(bank), follow_redirects=False
    )
    assert response.status_code == 303
    row = session.exec(select(TaxPayment)).one()
    assert response.headers["location"] == f"/payroll/tax-payments/{row.id}"
    assert (row.amount, row.payment_date, row.memo, row.agency) == (
        D("125.50"),
        date(2026, 3, 5),
        "Q1 deposit",
        "irs_941",
    )

    listing = owner_client.get("/payroll/tax-payments")
    assert listing.status_code == 200
    assert f"/payroll/tax-payments/{row.id}" in anchors(listing.text).hrefs
    assert "$125.50" in listing.text and bank.name in listing.text
    assert "/payroll/tax-payments/new" in anchors(listing.text).hrefs

    detail = owner_client.get(f"/payroll/tax-payments/{row.id}")
    assert detail.status_code == 200
    _assert_csp_safe(detail.text)
    found = anchors(detail.text)
    assert f"/journal/{row.entry_id}" in found.hrefs
    assert f"/payroll/tax-payments/{row.id}/void" in found.actions

    voided = owner_client.post(
        f"/payroll/tax-payments/{row.id}/void",
        data={"void_date": "2026-03-06", "memo": "wrong agency"},
        follow_redirects=False,
    )
    assert voided.status_code == 303
    session.refresh(row)
    assert row.status == "void" and row.void_date == date(2026, 3, 6)
    after = owner_client.get(f"/payroll/tax-payments/{row.id}")
    found = anchors(after.text)
    assert f"/payroll/tax-payments/{row.id}/void" not in found.actions
    assert f"/journal/{row.void_entry_id}" in found.hrefs
    again = owner_client.post(
        f"/payroll/tax-payments/{row.id}/void", data={"void_date": "2026-03-07"}
    )
    assert again.status_code == 409 and 'role="alert"' in again.text


def test_overpayment_is_accepted_and_shown(owner_client, session, bank):
    row = make(owner_client, session, bank, amount="9,999,999.99")
    assert row.amount == D("9999999.99")
    assert "$9,999,999.99" in owner_client.get("/payroll/tax-payments").text
    assert "$9,999,999.99" in owner_client.get(f"/payroll/tax-payments/{row.id}").text


def bad_cases(bank, payroll_roles):
    return [
        {"agency": "irs_940"},
        {"agency": ""},
        {"bank_account_id": str(payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE].id)},
        {"amount": "0.00"},
        {"amount": "10.123"},
        {"amount": "1E+30"},
        {"amount": "NaN"},
        {"amount": "-5.00"},
        {"amount": ""},
        {"amount": "abc"},
        {"payment_date": "2026-02-30"},
        {"payment_date": "03/05/2026"},
        {"payment_date": "2026-03-05T10:00:00"},
        {"payment_date": ""},
        {"bank_account_id": "abc"},
        {"bank_account_id": "0"},
        {"bank_account_id": "99999999999"},
        {"bank_account_id": "2147483648"},
        {"bank_account_id": "999999"},
        {"bank_account_id": ""},
        {"memo": "x" * 501},
        {"memo": "bad\x00memo"},
    ]


def test_validation_errors_are_422_pages_and_keep_values(
    owner_client, session, bank, payroll_roles
):
    session.commit()  # the sign-in session must survive a refused service call's rollback
    for over in bad_cases(bank, payroll_roles):
        response = owner_client.post(
            "/payroll/tax-payments/new", data=pay_form(bank, **over), follow_redirects=False
        )
        assert response.status_code == 422, (over, response.status_code, response.text[:300])
        assert 'role="alert"' in response.text, over
    assert count(session) == 0
    kept = owner_client.post(
        "/payroll/tax-payments/new",
        data=pay_form(bank, amount="0.00", memo=IMG_XSS, agency="ga_dor"),
    )
    assert 'value="0.00"' in kept.text and 'value="2026-03-05"' in kept.text
    assert 'value="ga_dor" selected' in kept.text
    assert f'value="{bank.id}" selected' in kept.text
    assert IMG_XSS not in kept.text and ESCAPED in kept.text


@pytest.mark.parametrize(
    "path",
    ["/payroll/tax-payments/abc", "/payroll/tax-payments/0", "/payroll/tax-payments/99999999999"],
)
def test_garbage_ids_are_422_not_500(owner_client, path):
    assert owner_client.get(path).status_code == 422
    assert owner_client.post(path + "/void", data={"void_date": "2026-03-05"}).status_code == 422


def test_missing_payment_is_404(owner_client):
    assert owner_client.get("/payroll/tax-payments/12345").status_code == 404
    response = owner_client.post(
        "/payroll/tax-payments/12345/void", data={"void_date": "2026-03-05"}
    )
    assert response.status_code == 404


def test_void_validation_errors_keep_values(owner_client, session, bank):
    session.commit()  # the sign-in session must survive a refused service call's rollback
    row = make(owner_client, session, bank)
    path = f"/payroll/tax-payments/{row.id}/void"
    for data in (
        {"void_date": ""},
        {"void_date": "2026-03-04"},  # before the payment
        {"void_date": "nope"},
        {"void_date": "2026-03-06", "memo": "x" * 501},
    ):
        response = owner_client.post(path, data=data)
        assert response.status_code == 422, data
        assert 'role="alert"' in response.text
    kept = owner_client.post(path, data={"void_date": "2026-03-04", "memo": IMG_XSS})
    assert 'value="2026-03-04"' in kept.text and ESCAPED in kept.text
    assert IMG_XSS not in kept.text
    session.expire_all()
    assert session.get(TaxPayment, row.id).status == "posted"


def test_list_pages_by_fifty(owner_client, session, bank):
    for _ in range(51):
        session.add(
            TaxPayment(
                payment_date=date(2026, 3, 5),
                agency="irs_941",
                bank_account_id=bank.id,
                amount=D("1.00"),
            )
        )
    session.commit()
    first = owner_client.get("/payroll/tax-payments")
    assert "page=2" in first.text
    assert "page=2" not in owner_client.get("/payroll/tax-payments?page=2").text
    assert owner_client.get("/payroll/tax-payments?page=abc").status_code == 422
    assert owner_client.get("/payroll/tax-payments?page=0").status_code == 422


def test_memo_is_escaped_on_every_page(owner_client, session, bank):
    row = make(owner_client, session, bank, memo=IMG_XSS)
    for path in ("/payroll/tax-payments", f"/payroll/tax-payments/{row.id}"):
        text = owner_client.get(path).text
        assert IMG_XSS not in text, path
    assert ESCAPED in owner_client.get(f"/payroll/tax-payments/{row.id}").text


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_non_owners_read_but_cannot_write(request, role_client, owner_client, session, bank):
    row = make(owner_client, session, bank)
    client = request.getfixturevalue(role_client)
    listing = client.get("/payroll/tax-payments")
    assert listing.status_code == 200
    assert "/payroll/tax-payments/new" not in anchors(listing.text).hrefs
    detail = client.get(f"/payroll/tax-payments/{row.id}")
    assert detail.status_code == 200
    assert f"/payroll/tax-payments/{row.id}/void" not in anchors(detail.text).actions
    assert client.get("/payroll/tax-payments/new").status_code == 403
    assert client.post("/payroll/tax-payments/new", data=pay_form(bank)).status_code == 403
    assert (
        client.post(f"/payroll/tax-payments/{row.id}/void", data={"void_date": "2026-03-06"})
    ).status_code == 403
    session.expire_all()
    assert session.get(TaxPayment, row.id).status == "posted"


def test_payroll_area_links_to_tax_payments(owner_client):
    home = owner_client.get("/")
    assert "/payroll/tax-payments" in anchors(home.text).hrefs
