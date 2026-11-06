"""Vendor and expense pages: owner flow, validation, uploads, CSV, XSS, roles, garbage input."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import func, select

from payroll import app_settings
from payroll.expenses.attachments import AttachmentService
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Attachment, Expense, Vendor
from payroll.reports.periods import company_today
from tests.expenses.conftest import card, expense_account, owner_role, vendor  # noqa: F401
from tests.frontend.test_csp_compat import _assert_csp_safe
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
XSS = "<script>alert(1)</script>"
IMG_XSS = "<img src=x onerror=alert(1)>"  # no slash: safe_filename keeps all of it
FORMULA = "=cmd|' /C calc'!A0"
PDF = b"%PDF-1.4\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 20
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "attachment_dir", str(tmp_path / "store"))


@pytest.fixture
def second_account(chart, owner_user, session):
    from payroll.ledger.accounts import AccountService
    from payroll.ledger.models import AccountType

    return AccountService(session).create(
        actor=owner_user, name="Travel Test", type=AccountType.EXPENSE
    )


def new_form(card, account, **over):
    data = {
        "expense_date": "2026-03-05",
        "vendor_id": "",
        "payee_text": "Corner Shop",
        "paid_from": str(card.id),
        "memo": "",
        "line_account_id": [str(account.id)],
        "line_amount": ["25.00"],
        "line_memo": [""],
    }
    data.update(over)
    return data


def pdf(n: int) -> bytes:
    return b"%PDF-1.4\n" + str(n).encode() + b"\n%%EOF"


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def make_expense(session, owner_user, card, account, **over):
    kwargs = {
        "actor": owner_user,
        "expense_date": date(2026, 3, 1),
        "lines": [ExpenseLineInput(account.id, D("10.00"))],
        "source": "account",
        "paid_from_account_id": card.id,
        "payee_text": "Shop",
    }
    kwargs.update(over)
    row = ExpenseService(session).create(**kwargs)
    session.commit()
    return row


# --------------------------------------------------------------------------- vendors


def test_owner_vendor_flow(owner_client, session):
    created = owner_client.post(
        "/vendors/new", data={"name": "Office Depot", "note": "supplies"}, follow_redirects=False
    )
    assert created.status_code == 303 and created.headers["location"] == "/vendors"
    vendor = session.exec(select(Vendor)).one()
    assert (vendor.name, vendor.note) == ("Office Depot", "supplies")
    page = owner_client.get("/vendors")
    assert page.status_code == 200 and "Office Depot" in page.text
    _assert_csp_safe(page.text)

    assert owner_client.get(f"/vendors/{vendor.id}/edit").status_code == 200
    edited = owner_client.post(
        f"/vendors/{vendor.id}/edit", data={"name": "Staples", "note": ""}, follow_redirects=False
    )
    assert edited.status_code == 303
    session.refresh(vendor)
    assert (vendor.name, vendor.note) == ("Staples", None)

    owner_client.post(f"/vendors/{vendor.id}/active", data={"active": "0"})
    assert "Staples" not in owner_client.get("/vendors").text
    assert "Staples" in owner_client.get("/vendors?inactive=1").text
    owner_client.post(f"/vendors/{vendor.id}/active", data={"active": "1"})
    assert "Staples" in owner_client.get("/vendors").text
    assert "Staples" in owner_client.get("/vendors?q=stap").text
    assert "Staples" not in owner_client.get("/vendors?q=zzz").text


def test_vendor_errors_redisplay_typed_values(owner_client, session, vendor):
    session.commit()
    dup = owner_client.post("/vendors/new", data={"name": "office depot", "note": "kept note"})
    assert dup.status_code == 409 and "already exists" in dup.text
    assert "kept note" in dup.text and 'value="office depot"' in dup.text
    blank = owner_client.post("/vendors/new", data={"name": "  ", "note": "n"})
    assert blank.status_code == 422 and "Name is required" in blank.text
    assert owner_client.get("/vendors/99999/edit").status_code == 404
    assert owner_client.post("/vendors/99999/edit", data={"name": "x"}).status_code == 404
    assert owner_client.post("/vendors/99999/active", data={"active": "0"}).status_code == 404


@pytest.mark.parametrize("value", ["maybe", "", "2", "true", "00", " ", "-1", "١"])
def test_garbage_active_value_leaves_the_vendor_unchanged(owner_client, session, vendor, value):
    session.commit()
    response = owner_client.post(f"/vendors/{vendor.id}/active", data={"active": value})
    assert response.status_code == 422
    session.refresh(vendor)
    assert vendor.active is True
    missing = owner_client.post(f"/vendors/{vendor.id}/active", data={})
    assert missing.status_code == 422
    session.refresh(vendor)
    assert vendor.active is True


def test_vendor_list_paging_is_bounded(owner_client, session):
    for i in range(3):
        session.add(Vendor(name=f"Vendor {i}"))
    session.commit()
    assert owner_client.get("/vendors?page=1").status_code == 200
    assert owner_client.get("/vendors?page=2").status_code == 200  # empty, not an error
    for bad in ("0", "-1", "x", "1.5", "99999999999999999999", "1" * 40):
        assert owner_client.get(f"/vendors?page={bad}").status_code == 422, bad


# --------------------------------------------------------------------------- expense flow


def test_owner_creates_split_expense_lists_filters_and_voids(
    owner_client, session, owner_user, card, expense_account, second_account, vendor
):
    session.commit()
    response = owner_client.post(
        "/expenses/new",
        data=new_form(
            card,
            expense_account,
            vendor_id=str(vendor.id),
            payee_text="",
            memo="Monthly run",
            line_account_id=[str(expense_account.id), str(second_account.id), ""],
            line_amount=["25.00", "1,000.50", ""],
            line_memo=["paper", "taxi", ""],
        ),
        files=[("receipts", ("scan.pdf", PDF, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    expense = session.exec(select(Expense)).one()
    assert response.headers["location"] == f"/expenses/{expense.id}"
    assert expense.total == D("1025.50") and expense.vendor_id == vendor.id
    assert count(session, Attachment) == 1

    detail = owner_client.get(f"/expenses/{expense.id}")
    assert detail.status_code == 200
    _assert_csp_safe(detail.text)
    for text in ("Office Depot", "paper", "taxi", "$1,025.50", "Business Visa", "scan.pdf"):
        assert text in detail.text, text
    attachment = session.exec(select(Attachment)).one()
    assert f'href="/attachments/{attachment.id}"' in detail.text
    assert f'href="/journal/{expense.entry_id}"' in detail.text
    assert f'action="/expenses/{expense.id}/void"' in detail.text
    assert f'action="/expenses/{expense.id}/receipts"' in detail.text

    listing = owner_client.get("/expenses")
    assert listing.status_code == 200 and "Office Depot" in listing.text
    assert "$1,025.50" in listing.text
    _assert_csp_safe(listing.text)

    other = make_expense(session, owner_user, card, second_account)
    assert other.id != expense.id

    def shown(query: str, row: Expense) -> bool:
        page = owner_client.get(f"/expenses?{query}")
        assert page.status_code == 200, (query, page.status_code)
        return f'href="/expenses/{row.id}"' in page.text

    assert shown("", expense) and shown("", other)
    assert shown(f"vendor_id={vendor.id}", expense) and not shown(f"vendor_id={vendor.id}", other)
    assert shown("date_from=2026-03-02", expense) and not shown("date_from=2026-03-02", other)
    assert shown("date_to=2026-03-02", other) and not shown("date_to=2026-03-02", expense)
    both_days = "date_from=2026-03-01&date_to=2026-03-05"
    assert shown(both_days, expense) and shown(both_days, other)
    assert shown(f"account_id={expense_account.id}", expense)
    assert not shown(f"account_id={expense_account.id}", other)
    assert shown(f"account_id={second_account.id}", other)
    assert shown(f"account_id={card.id}", expense) and shown(f"account_id={card.id}", other)
    assert shown("status=posted", expense) and not shown("status=void", expense)

    voided = owner_client.post(
        f"/expenses/{expense.id}/void",
        data={"void_date": "2026-03-10", "memo": "duplicate"},
        follow_redirects=False,
    )
    assert voided.status_code == 303 and voided.headers["location"] == f"/expenses/{expense.id}"
    session.refresh(expense)
    assert expense.status == "void" and expense.void_date == date(2026, 3, 10)
    assert shown("status=void", expense) and not shown("status=posted", expense)
    assert shown("status=posted", other)
    after = owner_client.get(f"/expenses/{expense.id}")
    assert f'action="/expenses/{expense.id}/void"' not in after.text
    assert "2026-03-10" in after.text
    again = owner_client.post(f"/expenses/{expense.id}/void", data={"void_date": "2026-03-11"})
    assert again.status_code == 409 and "already void" in again.text


def test_owner_paid_expense_and_open_owner_paid_filter(
    owner_client, session, owner_user, card, expense_account, owner_role
):
    session.commit()
    created = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account, paid_from="owner", payee_text="Hardware store"),
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    make_expense(session, owner_user, card, expense_account, payee_text="Card shop")
    owner_paid = session.exec(select(Expense).where(Expense.source == "owner")).one()
    assert owner_paid.paid_from_account_id is None
    page = owner_client.get("/expenses?open_owner_paid=1").text
    assert "Hardware store" in page and "Card shop" not in page
    assert "Owner (personal card)" in page
    both = owner_client.get("/expenses").text
    assert "Hardware store" in both and "Card shop" in both


def test_new_form_lists_pickers(owner_client, session, card, expense_account, vendor):
    session.commit()
    page = owner_client.get("/expenses/new")
    assert page.status_code == 200
    _assert_csp_safe(page.text)
    for text in (
        "Owner (personal card)",
        "Business Visa",
        "Office Supplies Test",
        "Office Depot",
        'enctype="multipart/form-data"',
        'name="receipts"',
        "/static/js/expense_form.js",
    ):
        assert text in page.text, text
    assert "Due to Owner" not in page.text.split('name="paid_from"')[1].split("</select>")[0]


# --------------------------------------------------------------------------- validation


def test_validation_error_keeps_typed_values_and_nothing_is_created(
    owner_client, session, card, expense_account, second_account
):
    session.commit()
    before = count(session, Expense)
    response = owner_client.post(
        "/expenses/new",
        data=new_form(
            card,
            expense_account,
            expense_date="2026-04-02",
            payee_text="Typed Payee",
            memo="typed memo",
            line_account_id=[str(expense_account.id), str(second_account.id)],
            line_amount=["12.345", "7.00"],
            line_memo=["first memo", "second memo"],
        ),
    )
    assert response.status_code == 422
    text = response.text
    for typed in (
        "Typed Payee",
        "typed memo",
        "first memo",
        "second memo",
        "12.345",
        "7.00",
        'value="2026-04-02"',
    ):
        assert typed in text, typed
    assert "two decimal places" in text or "Enter an amount" in text
    assert text.count('name="line_account_id"') >= 3  # two rows + the JS template
    assert f'value="{card.id}" selected' in text
    assert count(session, Expense) == before


@pytest.mark.parametrize(
    "over",
    [
        {"expense_date": ""},
        {"expense_date": "garbage"},
        {"expense_date": "2026-13-45"},
        {"expense_date": "20260305"},
        {"expense_date": "2026-3-5"},
        {"expense_date": "0001-01-01"},
        {"expense_date": "٢٠٢٦-٠٣-٠٥"},
        {"vendor_id": "abc"},
        {"vendor_id": "99999999999999999999"},
        {"vendor_id": "-1"},
        {"vendor_id": "1", "payee_text": "also a payee"},
        {"payee_text": ""},
        {"paid_from": ""},
        {"paid_from": "nope"},
        {"paid_from": "99999999999999999999"},
        {"paid_from": "True"},
        {"line_account_id": [""], "line_amount": ["5.00"], "line_memo": [""]},
        {"line_account_id": ["x"], "line_amount": ["5.00"], "line_memo": [""]},
        {"line_amount": ["abc"]},
        {"line_amount": ["1e5"]},
        {"line_amount": ["0.00"]},
        {"line_amount": ["999999999999999999999999.00"]},
        {"line_amount": ["5.00", "6.00"], "line_memo": [""]},  # ragged lists
        {"line_account_id": [], "line_amount": [], "line_memo": []},
        {"memo": "x" * 501},
        {"payee_text": "bad\x00payee"},
        {"line_memo": ["bad\x00memo"]},
    ],
)
def test_garbage_expense_form_input_is_a_422_never_a_500(
    owner_client, session, card, expense_account, over
):
    session.commit()
    response = owner_client.post("/expenses/new", data=new_form(card, expense_account, **over))
    assert response.status_code == 422, (over, response.status_code)
    assert "<html" in response.text.lower()
    assert count(session, Expense) == 0


def test_too_many_lines_is_a_422(owner_client, session, card, expense_account):
    session.commit()
    n = 150
    response = owner_client.post(
        "/expenses/new",
        data=new_form(
            card,
            expense_account,
            line_account_id=[str(expense_account.id)] * n,
            line_amount=["1.00"] * n,
            line_memo=[""] * n,
        ),
    )
    assert response.status_code == 422 and "at most 100 lines" in response.text


GARBAGE = ["abc", "-1", "0", "1.5", "99999999999999999999", "1e3", "%00", "٣"]


@pytest.mark.parametrize("bad", GARBAGE)
def test_garbage_ids_in_paths_are_422_or_404_never_500(owner_client, session, bad):
    session.commit()
    for method, path in (
        ("GET", f"/expenses/{bad}"),
        ("POST", f"/expenses/{bad}/void"),
        ("POST", f"/expenses/{bad}/receipts"),
        ("GET", f"/vendors/{bad}/edit"),
        ("POST", f"/vendors/{bad}/edit"),
        ("POST", f"/vendors/{bad}/active"),
        ("GET", f"/attachments/{bad}"),
    ):
        response = owner_client.request(
            method,
            path,
            data={"name": "x", "void_date": "2026-03-01"} if method == "POST" else None,
        )
        assert response.status_code in (404, 422), (method, path, response.status_code)


GARBAGE_PAGE_QUERIES = [
    "page=0",
    "page=-2",
    "page=abc",
    "page=99999999999999999999",
    "page=1.5",
    "page=",
]
GARBAGE_FILTER_QUERIES = [
    "date_from=garbage",
    "date_to=2026-02-30",
    "date_from=2026-3-1",
    "date_from=0001-01-01",
    "vendor_id=abc",
    "vendor_id=99999999999999999999",
    "vendor_id=-3",
    "account_id=x",
    "account_id=99999999999999999999",
    "status=bogus",
    "open_owner_paid=maybe",
]


@pytest.mark.parametrize("query", GARBAGE_FILTER_QUERIES)
@pytest.mark.parametrize("path", ["/expenses", "/expenses.csv"])
def test_garbage_filters_are_422_never_500(owner_client, path, query):
    response = owner_client.get(f"{path}?{query}")
    assert response.status_code == 422, (path, query, response.status_code)


@pytest.mark.parametrize("query", GARBAGE_PAGE_QUERIES)
def test_garbage_pages_are_422_never_500(owner_client, query):
    response = owner_client.get(f"/expenses?{query}")
    # an empty ``page=`` means "first page"; everything else here is refused
    expected = 200 if query == "page=" else 422
    assert response.status_code == expected, (query, response.status_code)


@pytest.mark.parametrize("query", [*GARBAGE_PAGE_QUERIES, "page=2", "page=999"])
def test_csv_ignores_page(owner_client, session, owner_user, card, expense_account, query):
    make_expense(session, owner_user, card, expense_account, payee_text="Row one")
    plain = owner_client.get("/expenses.csv")
    paged = owner_client.get(f"/expenses.csv?{query}")
    assert plain.status_code == paged.status_code == 200
    assert paged.text == plain.text and "Row one" in paged.text


def test_garbage_void_input_is_422(owner_client, session, owner_user, card, expense_account):
    expense = make_expense(session, owner_user, card, expense_account)
    for bad in ("", "garbage", "2026-02-30", "2026/03/05", "2026-03-05T00:00:00", "2026-02-28"):
        response = owner_client.post(f"/expenses/{expense.id}/void", data={"void_date": bad})
        assert response.status_code == 422, bad
        assert "<html" in response.text.lower()
    session.refresh(expense)
    assert expense.status == "posted"


# --------------------------------------------------------------------------- uploads


def test_receipt_of_ten_mb_plus_one_is_422_and_nothing_is_created(
    owner_client, session, card, expense_account
):
    session.commit()
    big = PDF + b"0" * (10 * MB + 1 - len(PDF))
    assert len(big) == 10 * MB + 1
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account, payee_text="Typed Payee"),
        files=[("receipts", ("big.pdf", big, "application/pdf"))],
    )
    assert response.status_code == 422
    assert "10 MB" in response.text and "Typed Payee" in response.text
    assert count(session, Expense) == 0 and count(session, Attachment) == 0


def test_exactly_ten_mb_is_accepted(owner_client, session, card, expense_account):
    session.commit()
    ok = PDF + b"0" * (10 * MB - len(PDF))
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[("receipts", ("ok.pdf", ok, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert count(session, Attachment) == 1


def test_owner_can_upload_close_to_the_fifty_mb_total(owner_client, session, card, expense_account):
    session.commit()
    chunk = PDF + b"0" * (int(9.8 * MB))
    five = [
        ("receipts", (f"c{i}.pdf", chunk + str(i).encode(), "application/pdf")) for i in range(5)
    ]
    response = owner_client.post(
        "/expenses/new", data=new_form(card, expense_account), files=five, follow_redirects=False
    )
    assert response.status_code == 303, response.text[:300]
    assert count(session, Attachment) == 5


def test_more_than_ten_files_or_fifty_mb_is_422(owner_client, session, card, expense_account):
    session.commit()
    eleven = [("receipts", (f"r{i}.pdf", pdf(i), "application/pdf")) for i in range(11)]
    response = owner_client.post(
        "/expenses/new", data=new_form(card, expense_account), files=eleven
    )
    assert response.status_code == 422 and "at most 10" in response.text
    assert count(session, Expense) == 0
    chunk = PDF + b"0" * (9 * MB)
    six = [
        ("receipts", (f"c{i}.pdf", chunk + str(i).encode(), "application/pdf")) for i in range(6)
    ]
    response = owner_client.post("/expenses/new", data=new_form(card, expense_account), files=six)
    assert response.status_code == 422 and "50 MB" in response.text
    assert count(session, Expense) == 0 and count(session, Attachment) == 0


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("evil.pdf", b"\x7fELF" + b"\x00" * 50, "only PDF, PNG, JPEG and HEIC"),
        ("empty.pdf", b"", "empty"),
    ],
)
def test_bad_receipts_are_refused_before_the_expense_is_created(
    owner_client, session, card, expense_account, name, data, message
):
    session.commit()
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[("receipts", (name, data, "application/pdf"))],
    )
    assert response.status_code == 422 and message in response.text
    assert name in response.text
    assert count(session, Expense) == 0


def test_duplicate_files_in_one_request_are_refused(owner_client, session, card, expense_account):
    session.commit()
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[
            ("receipts", ("a.pdf", PDF, "application/pdf")),
            ("receipts", ("b.pdf", PDF, "application/pdf")),
        ],
    )
    assert response.status_code == 422 and "same file" in response.text.lower()
    assert count(session, Expense) == 0


def test_no_file_chosen_is_fine(owner_client, session, card, expense_account):
    session.commit()
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[("receipts", ("", b"", "application/octet-stream"))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert count(session, Attachment) == 0
    detail = owner_client.get(response.headers["location"])
    assert "No receipts" in detail.text


def test_oserror_storing_a_receipt_keeps_the_expense_and_warns(
    owner_client, session, card, expense_account, monkeypatch
):
    session.commit()

    def boom(self, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(AttachmentService, "add", boom)
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account, payee_text="Saved Anyway"),
        files=[("receipts", ("scan.pdf", PDF, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 503
    assert "No space left" not in response.text  # no raw OS detail
    assert "expense was saved" in response.text.lower() and "receipts" in response.text.lower()
    assert "Saved Anyway" in response.text
    assert count(session, Expense) == 1 and count(session, Attachment) == 0


def test_receipts_are_stored_off_the_event_loop(
    owner_client, session, card, expense_account, monkeypatch
):
    import threading

    from payroll.frontend import expense_routers

    session.commit()
    seen: dict[str, int] = {}
    real_read = expense_routers._read_uploads
    real_add = AttachmentService.add

    async def read(form):  # a coroutine: runs on the event loop thread
        seen["loop"] = threading.get_ident()
        return await real_read(form)

    def add(self, **kwargs):
        seen["add"] = threading.get_ident()
        return real_add(self, **kwargs)

    monkeypatch.setattr(expense_routers, "_read_uploads", read)
    monkeypatch.setattr(AttachmentService, "add", add)
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[("receipts", ("scan.pdf", PDF, "application/pdf"))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert seen["add"] != seen["loop"]


def test_add_receipts_to_an_existing_expense(
    owner_client, session, owner_user, card, expense_account
):
    expense = make_expense(session, owner_user, card, expense_account)
    ok = owner_client.post(
        f"/expenses/{expense.id}/receipts",
        files=[
            ("receipts", ("a.pdf", pdf(1), "application/pdf")),
            ("receipts", ("b.png", PNG, "image/png")),
        ],
        follow_redirects=False,
    )
    assert ok.status_code == 303 and ok.headers["location"] == f"/expenses/{expense.id}"
    assert count(session, Attachment) == 2
    dup = owner_client.post(
        f"/expenses/{expense.id}/receipts",
        files=[("receipts", ("again.pdf", pdf(1), "application/pdf"))],
    )
    assert dup.status_code == 422 and "already attached" in dup.text
    none = owner_client.post(f"/expenses/{expense.id}/receipts", data={"x": "1"})
    assert none.status_code == 422 and "Choose" in none.text
    missing = owner_client.post(
        "/expenses/99999/receipts", files=[("receipts", ("a.pdf", pdf(9), "application/pdf"))]
    )
    assert missing.status_code == 404
    assert count(session, Attachment) == 2
    # Allowed on a void expense too.
    owner_client.post(f"/expenses/{expense.id}/void", data={"void_date": "2026-03-02"})
    late = owner_client.post(
        f"/expenses/{expense.id}/receipts",
        files=[("receipts", ("late.pdf", pdf(3), "application/pdf"))],
        follow_redirects=False,
    )
    assert late.status_code == 303 and count(session, Attachment) == 3


def test_add_receipts_oserror_is_a_friendly_503(
    owner_client, session, owner_user, card, expense_account, monkeypatch
):
    expense = make_expense(session, owner_user, card, expense_account)

    def boom(self, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(AttachmentService, "add", boom)
    response = owner_client.post(
        f"/expenses/{expense.id}/receipts",
        files=[("receipts", ("a.pdf", PDF, "application/pdf"))],
    )
    assert response.status_code == 503 and "Permission denied" not in response.text
    assert "could not be stored" in response.text.lower()


def test_oversize_upload_body_is_refused_early_with_422(owner_client, monkeypatch):
    monkeypatch.setattr(app_settings, "max_upload_body_bytes", 2 * MB)
    response = owner_client.post(
        "/expenses/new", files=[("receipts", ("big.pdf", PDF + b"0" * (3 * MB), "x/y"))]
    )
    assert response.status_code == 422
    assert owner_client.post("/vendors/new", data={"name": "x" * (2 * MB)}).status_code == 413


# --------------------------------------------------------------------------- CSV


def test_csv_neutralises_formulas_and_follows_filters(
    owner_client, session, owner_user, card, expense_account
):
    evil = Vendor(name=FORMULA)
    session.add(evil)
    session.flush()
    make_expense(
        session,
        owner_user,
        card,
        expense_account,
        vendor_id=evil.id,
        payee_text=None,
        memo="@SUM(1+1)",
    )
    make_expense(
        session, owner_user, card, expense_account, payee_text="+cmd", expense_date=date(2026, 4, 1)
    )
    response = owner_client.get("/expenses.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    cells = [c for row in rows for c in row]
    assert "'=cmd|' /C calc'!A0" in cells
    assert "'@SUM(1+1)" in cells and "'+cmd" in cells
    assert not [c for c in cells if c.startswith(("=", "+", "-", "@"))]
    assert rows[0][:3] == ["Date", "Payee", "Memo"]
    only_april = owner_client.get("/expenses.csv?date_from=2026-04-01").text
    assert "'+cmd" in only_april and "calc" not in only_april


def test_csv_is_capped(owner_client, session, owner_user, card, expense_account, monkeypatch):
    from payroll.frontend import expense_routers

    monkeypatch.setattr(expense_routers, "MAX_CSV_ROWS", 2)
    for i in range(3):
        make_expense(session, owner_user, card, expense_account, payee_text=f"P{i}")
    rows = list(csv.reader(io.StringIO(owner_client.get("/expenses.csv").text)))
    assert len(rows) == 1 + 2 + 1  # header, capped rows, a note
    assert "limited" in rows[-1][0].lower()


# --------------------------------------------------------------------------- escaping


def test_hostile_text_is_escaped_everywhere(
    owner_client, session, owner_user, card, expense_account, tmp_path
):
    evil_vendor = Vendor(name=XSS, note=XSS)
    session.add(evil_vendor)
    session.flush()
    with_vendor = make_expense(
        session,
        owner_user,
        card,
        expense_account,
        vendor_id=evil_vendor.id,
        payee_text=None,
        memo=XSS,
        lines=[ExpenseLineInput(expense_account.id, D("5.00"), XSS)],
    )
    with_payee = make_expense(session, owner_user, card, expense_account, payee_text=XSS)
    AttachmentService(session).add(
        actor=owner_user,
        owner_type="expense",
        owner_id=with_payee.id,
        filename=f"{IMG_XSS}.pdf",
        data=PDF,
    )
    session.commit()
    pages = [
        "/vendors",
        f"/vendors/{evil_vendor.id}/edit",
        "/expenses",
        "/expenses?open_owner_paid=1",
        f"/expenses?vendor_id={evil_vendor.id}",
        f"/expenses/{with_vendor.id}",
        f"/expenses/{with_payee.id}",
        "/expenses/new",
    ]
    for path in pages:
        text = owner_client.get(path).text
        assert "<script>alert(1)" not in text, path
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in owner_client.get("/vendors").text
    assert "&lt;script&gt;" in owner_client.get(f"/expenses/{with_vendor.id}").text
    assert "&lt;script&gt;" in owner_client.get(f"/expenses/{with_payee.id}").text
    detail = owner_client.get(f"/expenses/{with_payee.id}").text
    assert "&lt;img src=x onerror=alert(1)&gt;.pdf" in detail and "<img src=x" not in detail
    uploaded = owner_client.post(
        f"/expenses/{with_payee.id}/receipts",
        files=[("receipts", (f"{IMG_XSS}2.pdf", pdf(41), "application/pdf"))],
        follow_redirects=True,
    )
    assert "&lt;img src=x onerror=alert(1)&gt;2.pdf" in uploaded.text
    assert "<img src=x" not in uploaded.text
    dup = owner_client.post(
        f"/expenses/{with_payee.id}/receipts",
        files=[("receipts", (f"{IMG_XSS}3.pdf", pdf(41), "application/pdf"))],
    )
    assert dup.status_code == 422 and "<img src=x" not in dup.text
    assert "&lt;img src=x" in dup.text
    assert "&lt;script&gt;" in owner_client.get("/expenses").text
    # typed values on the error redisplay
    bad = owner_client.post(
        "/expenses/new",
        data=new_form(
            card,
            expense_account,
            payee_text=XSS,
            memo=XSS,
            line_memo=[XSS],
            line_amount=["nope"],
        ),
    )
    assert bad.status_code == 422 and "<script>alert(1)" not in bad.text
    assert "&lt;script&gt;" in bad.text
    badv = owner_client.post("/vendors/new", data={"name": "", "note": XSS})
    assert badv.status_code == 422 and "<script>alert(1)" not in badv.text


# --------------------------------------------------------------------------- roles


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_readers_read_but_cannot_change_anything(
    request, role_client, session, owner_user, card, expense_account, vendor
):
    expense = make_expense(session, owner_user, card, expense_account)
    AttachmentService(session).add(
        actor=owner_user, owner_type="expense", owner_id=expense.id, filename="r.pdf", data=PDF
    )
    session.commit()
    client = request.getfixturevalue(role_client)
    for path in ("/vendors", "/expenses", f"/expenses/{expense.id}", "/expenses.csv"):
        assert client.get(path).status_code == 200, path
    detail = client.get(f"/expenses/{expense.id}").text
    assert "r.pdf" in detail and "/attachments/" in detail
    for hidden in ("/void", "/receipts", 'name="void_date"', 'type="file"'):
        assert hidden not in detail, hidden
    vendors = client.get("/vendors").text
    assert "/vendors/new" not in vendors and "/edit" not in vendors and "/active" not in vendors
    listing = client.get("/expenses").text
    assert "/expenses/new" not in listing
    assert client.get("/expenses/new").status_code == 403
    assert client.get("/vendors/new").status_code == 403
    assert client.get(f"/vendors/{vendor.id}/edit").status_code == 403
    before = (count(session, Expense), count(session, Vendor), count(session, Attachment))
    posts = [
        ("/vendors/new", {"name": "Nope"}, None),
        (f"/vendors/{vendor.id}/edit", {"name": "Nope"}, None),
        (f"/vendors/{vendor.id}/active", {"active": "0"}, None),
        ("/expenses/new", new_form(card, expense_account), None),
        (f"/expenses/{expense.id}/void", {"void_date": "2026-03-05"}, None),
        (
            f"/expenses/{expense.id}/receipts",
            {},
            [("receipts", ("n.pdf", pdf(77), "application/pdf"))],
        ),
    ]
    for path, data, files in posts:
        response = client.post(path, data=data, files=files)
        assert response.status_code == 403, (path, response.status_code)
    assert before == (count(session, Expense), count(session, Vendor), count(session, Attachment))


def test_anonymous_is_sent_to_login(anon_client):
    for path in ("/vendors", "/expenses", "/expenses.csv", "/expenses/new", "/expenses/1"):
        response = anon_client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
    for path in ("/vendors/new", "/expenses/new", "/expenses/1/void", "/expenses/1/receipts"):
        response = anon_client.post(path, data={}, follow_redirects=False)
        assert response.status_code == 303, path


def test_owner_post_without_csrf_token_is_refused(owner_client, session, card, expense_account):
    from payroll.auth.dependencies import CSRF_HEADER

    session.commit()
    owner_client.headers.pop(CSRF_HEADER)
    response = owner_client.post("/expenses/new", data=new_form(card, expense_account))
    assert response.status_code == 403
    assert count(session, Expense) == 0
    response = owner_client.post(
        "/expenses/new",
        data=new_form(card, expense_account),
        files=[("receipts", ("a.pdf", PDF, "application/pdf"))],
    )
    assert response.status_code == 403 and count(session, Expense) == 0
    assert owner_client.post("/vendors/new", data={"name": "X"}).status_code == 403


def test_expense_not_found_page(owner_client):
    assert owner_client.get("/expenses/987654").status_code == 404


def test_today_default_in_new_form(owner_client, session, chart):
    session.commit()
    assert f'value="{company_today()}"' in owner_client.get("/expenses/new").text
