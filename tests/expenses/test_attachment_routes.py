"""GET /attachments/{id}: login required, any role, exact headers, inline only for PNG/JPEG."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

from datetime import date
from decimal import Decimal

import pytest

from payroll import app_settings
from payroll.expenses.attachments import AttachmentService
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from tests.expenses.conftest import card, expense_account  # noqa: F401
from tests.ledger.conftest import chart  # noqa: F401

PDF = b"%PDF-1.4\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 10
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "attachment_dir", str(tmp_path / "store"))


@pytest.fixture
def expense(session, owner_user, card, expense_account):
    row = ExpenseService(session).create(
        actor=owner_user,
        expense_date=date(2026, 3, 1),
        lines=[ExpenseLineInput(expense_account.id, Decimal("10.00"))],
        source="account",
        paid_from_account_id=card.id,
        payee_text="Shop",
    )
    session.commit()
    return row


@pytest.fixture
def upload(session, owner_user, expense):
    def make(data: bytes, filename: str):
        row = AttachmentService(session).add(
            actor=owner_user,
            owner_type="expense",
            owner_id=expense.id,
            filename=filename,
            data=data,
        )
        session.commit()
        return row

    return make


def headers_of(response) -> dict[str, str]:
    return {k.lower(): v for k, v in response.headers.items()}


@pytest.mark.parametrize(
    ("data", "name", "kind"),
    [
        (PDF, "receipt.pdf", "application/pdf"),
        (PNG, "photo.png", "image/png"),
        (JPEG, "photo.jpg", "image/jpeg"),
        (HEIC, "photo.heic", "image/heic"),
    ],
)
def test_download_headers_are_exact(owner_client, upload, data, name, kind):
    row = upload(data, name)
    response = owner_client.get(f"/attachments/{row.id}")
    assert response.status_code == 200
    assert response.content == data
    got = headers_of(response)
    assert got["content-type"] == kind
    assert got["content-disposition"] == (
        f"attachment; filename=\"{name}\"; filename*=UTF-8''{name}"
    )
    assert got["x-content-type-options"] == "nosniff"
    assert got["cache-control"] == "private, no-store"
    assert "etag" not in got and "last-modified" not in got


@pytest.mark.parametrize(
    ("data", "name", "kind"),
    [(PNG, "photo.png", "image/png"), (JPEG, "photo.jpg", "image/jpeg")],
)
def test_inline_is_honoured_for_png_and_jpeg(owner_client, upload, data, name, kind):
    row = upload(data, name)
    response = owner_client.get(f"/attachments/{row.id}?inline=1")
    got = headers_of(response)
    assert response.status_code == 200 and got["content-type"] == kind
    assert got["content-disposition"] == f"inline; filename=\"{name}\"; filename*=UTF-8''{name}"
    assert got["x-content-type-options"] == "nosniff"
    assert got["cache-control"] == "private, no-store"


@pytest.mark.parametrize(("data", "name"), [(PDF, "receipt.pdf"), (HEIC, "photo.heic")])
@pytest.mark.parametrize("flag", ["1", "true", "yes"])
def test_inline_is_ignored_for_pdf_and_heic(owner_client, upload, data, name, flag):
    row = upload(data, name)
    got = headers_of(owner_client.get(f"/attachments/{row.id}?inline={flag}"))
    assert got["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("flag", ["0", "", "true", "2", "1x"])
def test_inline_other_values_stay_attachment_for_images(owner_client, upload, flag):
    row = upload(PNG, "p.png")
    got = headers_of(owner_client.get(f"/attachments/{row.id}?inline={flag}"))
    assert got["content-disposition"].startswith("attachment;")


def test_content_type_is_the_stored_verified_type_not_the_name(owner_client, upload):
    row = upload(PDF, "totally-an-image.png")
    got = headers_of(owner_client.get(f"/attachments/{row.id}?inline=1"))
    assert got["content-type"] == "application/pdf"
    assert got["content-disposition"].startswith("attachment;")


def test_unicode_and_hostile_filenames_are_encoded(owner_client, upload):
    row = upload(PDF, 'récéipt "x"; ‮<b>日本.pdf')
    disposition = headers_of(owner_client.get(f"/attachments/{row.id}"))["content-disposition"]
    assert disposition.isascii() and "\n" not in disposition and "\r" not in disposition
    head, _, star = disposition.partition("; filename*=UTF-8''")
    assert head.startswith('attachment; filename="')
    fallback = head[len('attachment; filename="') : -1]
    assert fallback and all(c.isalnum() or c in "._ -" for c in fallback)
    assert "%C3%A9" in star and "%E6%97%A5" in star and "%22" in star and "%3B" in star
    assert '"' not in star and ";" not in star and "<" not in star


def test_download_requires_login(anon_client, upload):
    row = upload(PDF, "r.pdf")
    response = anon_client.get(f"/attachments/{row.id}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert PDF not in response.content


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client", "owner_client"])
def test_every_role_can_download(request, role_client, upload):
    row = upload(PDF, "r.pdf")
    client = request.getfixturevalue(role_client)
    response = client.get(f"/attachments/{row.id}")
    assert response.status_code == 200 and response.content == PDF


def test_missing_attachment_and_missing_file_are_404(owner_client, upload, tmp_path):
    assert owner_client.get("/attachments/424242").status_code == 404
    row = upload(PDF, "r.pdf")
    for stored in (tmp_path / "store").rglob("*"):
        if stored.is_file():
            stored.unlink()
    response = owner_client.get(f"/attachments/{row.id}")
    assert response.status_code == 404 and "not found" in response.text.lower()


@pytest.mark.parametrize("bad", ["abc", "-1", "0", "1.5", "99999999999999999999", "%00"])
def test_garbage_attachment_ids_are_never_a_500(owner_client, bad):
    assert owner_client.get(f"/attachments/{bad}").status_code in (404, 422)


def test_inline_garbage_values_are_never_a_500(owner_client, upload):
    row = upload(PNG, "p.png")
    for value in ("%00", "x" * 5000, "1&inline=0"):
        assert owner_client.get(f"/attachments/{row.id}?inline={value}").status_code == 200
