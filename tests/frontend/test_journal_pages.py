"""Journal pages: list, entry editor, view, reverse and void."""

from decimal import Decimal
from html.parser import HTMLParser

import pytest
from sqlmodel import select

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.reports.periods import company_today

XSS = "<img src=x onerror=alert(1)>"


@pytest.fixture(name="accts")
def accts_fixture(session, owner_user):
    service = AccountService(session)
    cash = service.create(actor=owner_user, name="Cash", type=AccountType.ASSET, number="1000")
    rent = service.create(actor=owner_user, name="Rent", type=AccountType.EXPENSE, number="6000")
    old = service.create(actor=owner_user, name="Old Acct", type=AccountType.ASSET)
    service.set_active(old.id, False, actor=owner_user)
    return cash, rent, old


def _form(accts, **over):
    cash, rent, _ = accts
    data = {
        "entry_date": "2026-03-01",
        "memo": "Pay rent",
        "line_account": [str(rent.id), str(cash.id)],
        "line_debit": ["100.00", ""],
        "line_credit": ["", "100.00"],
        "line_memo": ["", ""],
    }
    data.update(over)
    return data


def _entry(session, owner_user, accts, source=SourceType.MANUAL, memo="Seed"):
    cash, rent, _ = accts
    return LedgerPostingService(session).post(
        EntryDraft(
            entry_date=company_today(),
            memo=memo,
            source_type=source,
            lines=[
                LineDraft(rent.id, debit=Decimal("50")),
                LineDraft(cash.id, credit=Decimal("50")),
            ],
        ),
        actor=owner_user,
    )


def _create(client, accts, **over):
    return client.post("/journal", data=_form(accts, **over), follow_redirects=False)


def test_create_balanced_entry_redirects_and_shows_number(owner_client, accts, session):
    response = _create(owner_client, accts)
    assert response.status_code == 303
    entry = session.exec(select(JournalEntry)).one()
    assert response.headers["location"] == f"/journal/{entry.id}"
    page = owner_client.get(response.headers["location"])
    assert page.status_code == 200
    assert f"#{entry.entry_no}" in page.text
    assert "Pay rent" in page.text and "1000" in page.text and "Posted" in page.text
    assert "$100.00" in page.text


@pytest.mark.parametrize("debit", ["1,234.56", "$10.00"])
def test_formatted_amounts_accepted(owner_client, accts, debit):
    amount = debit
    response = _create(owner_client, accts, line_debit=[amount, ""], line_credit=["", amount])
    assert response.status_code == 303


def test_unbalanced_rerenders_422_with_rows_preserved(owner_client, accts):
    response = _create(owner_client, accts, line_credit=["", "90.00"], memo="Keep me")
    assert response.status_code == 422
    assert "Debits 100.00 do not equal credits 90.00" in response.text
    assert 'value="Keep me"' in response.text
    assert 'value="100.00"' in response.text and 'value="90.00"' in response.text
    assert "selected" in response.text


@pytest.mark.parametrize("bad", ["10.005", "abc", "-5", "NaN", "1e3"])
def test_bad_amounts_rejected_with_row_error(owner_client, accts, bad):
    response = _create(owner_client, accts, line_debit=[bad, ""])
    assert response.status_code == 422
    assert "Line 1" in response.text
    assert f'value="{bad}"' in response.text


def test_row_with_account_but_no_amount_and_both_sides(owner_client, accts):
    cash, rent, _ = accts
    none = _create(owner_client, accts, line_debit=["", ""], line_credit=["", ""])
    assert none.status_code == 422
    both = _create(owner_client, accts, line_debit=["5", ""], line_credit=["5", "5"])
    assert both.status_code == 422 and "either a debit or a credit" in both.text
    no_acct = _create(owner_client, accts, line_account=["", str(cash.id)])
    assert no_acct.status_code == 422 and "Choose an account" in no_acct.text


def test_blank_rows_ignored_and_bad_date(owner_client, accts):
    cash, rent, _ = accts
    ok = _create(
        owner_client,
        accts,
        line_account=[str(rent.id), "", str(cash.id)],
        line_debit=["100", "", ""],
        line_credit=["", "", "100"],
        line_memo=["", "", ""],
    )
    assert ok.status_code == 303
    bad = _create(owner_client, accts, entry_date="nope")
    assert bad.status_code == 422 and "not a valid date" in bad.text


def test_inactive_account_not_offered_and_rejected(owner_client, accts):
    cash, rent, old = accts
    page = owner_client.get("/journal/new")
    assert "1000 Cash" in page.text and "Old Acct" not in page.text
    bad = _create(owner_client, accts, line_account=[str(old.id), str(cash.id)])
    assert bad.status_code == 422 and "Inactive" in bad.text


def test_new_form_defaults_to_today_and_has_csrf_and_script(owner_client, accts):
    page = owner_client.get("/journal/new")
    assert f'value="{company_today()}"' in page.text
    assert 'name="csrf_token"' in page.text
    assert '<script src="/static/js/journal.js">' in page.text
    assert owner_client.get("/static/js/journal.js").status_code == 200


def test_reversal_links_both_ways_and_double_reverse(owner_client, accts, session, owner_user):
    entry = _entry(session, owner_user, accts)
    done = owner_client.post(
        f"/journal/{entry.id}/reverse",
        data={"entry_date": str(company_today())},
        follow_redirects=False,
    )
    assert done.status_code == 303
    reversal_url = done.headers["location"]
    original = owner_client.get(f"/journal/{entry.id}")
    assert f'href="{reversal_url}"' in original.text and "Reversed by" in original.text
    assert "Reverse entry" not in original.text
    reversal = owner_client.get(reversal_url)
    assert f'href="/journal/{entry.id}"' in reversal.text and "Reverses" in reversal.text
    again = owner_client.post(
        f"/journal/{entry.id}/reverse", data={"entry_date": str(company_today())}
    )
    assert again.status_code == 409 and "already been reversed" in again.text


def test_void_marks_status_and_bad_date(owner_client, accts, session, owner_user):
    entry = _entry(session, owner_user, accts)
    bad = owner_client.post(f"/journal/{entry.id}/void", data={"entry_date": "x"})
    assert bad.status_code == 422 and "not a valid date" in bad.text
    done = owner_client.post(
        f"/journal/{entry.id}/void",
        data={"entry_date": str(company_today()), "memo": "oops"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    page = owner_client.get(f"/journal/{entry.id}")
    assert "Void" in page.text


def test_non_manual_entry_has_no_buttons_and_post_refused(owner_client, accts, session, owner_user):
    entry = _entry(session, owner_user, accts, source=SourceType.INVOICE)
    page = owner_client.get(f"/journal/{entry.id}")
    assert "Reverse entry" not in page.text
    response = owner_client.post(
        f"/journal/{entry.id}/reverse", data={"entry_date": str(company_today())}
    )
    assert response.status_code == 422 and "source document" in response.text


def test_missing_entry_404(owner_client):
    assert owner_client.get("/journal/999999").status_code == 404
    assert (
        owner_client.post("/journal/999999/void", data={"entry_date": "2026-01-01"}).status_code
        == 404
    )


def test_list_filters_and_paging(owner_client, accts, session, owner_user):
    manual = _entry(session, owner_user, accts, memo="Manual one")
    _entry(session, owner_user, accts, source=SourceType.INVOICE, memo="Invoice one")
    page = owner_client.get("/journal")
    assert "Manual one" in page.text and "Invoice one" in page.text
    only = owner_client.get("/journal?source=manual")
    assert "Manual one" in only.text and "Invoice one" not in only.text
    assert f'href="/journal/{manual.id}"' in only.text
    far = owner_client.get("/journal?start=2000-01-01&end=2000-12-31")
    assert "Manual one" not in far.text and "No journal entries" in far.text
    assert owner_client.get("/journal?source=bogus").status_code == 422
    assert owner_client.get("/journal?start=zzz").status_code == 422
    assert owner_client.get("/journal?page=0").status_code == 200


def test_list_pages(owner_client, accts, session, owner_user, monkeypatch):
    from payroll.frontend import journal_routers

    monkeypatch.setattr(journal_routers, "JOURNAL_PAGE_SIZE", 2)
    for i in range(3):
        _entry(session, owner_user, accts, memo=f"Entry {i}")
    first = owner_client.get("/journal")
    assert "page=2" in first.text and "page=1" not in first.text
    second = owner_client.get("/journal?page=2")
    assert "page=1" in second.text and "page=3" not in second.text


def test_viewer_sees_pages_but_no_controls_and_posts_403(viewer_client, accts, session, owner_user):
    entry = _entry(session, owner_user, accts)
    listing = viewer_client.get("/journal")
    assert listing.status_code == 200 and "New entry" not in listing.text
    view = viewer_client.get(f"/journal/{entry.id}")
    assert view.status_code == 200
    assert "Reverse entry" not in view.text and "Void entry" not in view.text
    assert viewer_client.get("/journal/new").status_code == 403
    assert viewer_client.post("/journal", data=_form(accts)).status_code == 403
    for action in ("reverse", "void"):
        resp = viewer_client.post(
            f"/journal/{entry.id}/{action}", data={"entry_date": "2026-01-01"}
        )
        assert resp.status_code == 403


def test_accountant_cannot_post(accountant_client, accts):
    assert accountant_client.post("/journal", data=_form(accts)).status_code == 403


def test_memo_escaped_everywhere(owner_client, accts, session):
    response = _create(owner_client, accts, memo=XSS, line_memo=[XSS, XSS])
    assert response.status_code == 303
    view = owner_client.get(response.headers["location"])
    listing = owner_client.get("/journal")
    bad = _create(owner_client, accts, memo=XSS, line_credit=["", "1.00"])
    for page in (view, listing, bad):
        assert XSS not in page.text
        assert "&lt;img" in page.text


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_pages_are_csp_safe(owner_client, accts, session, owner_user):
    entry = _entry(session, owner_user, accts)
    urls = ["/journal", "/journal/new", f"/journal/{entry.id}"]
    for url in urls:
        parser = _Collector()
        parser.feed(owner_client.get(url).text)
        for tag, attrs in parser.tags:
            assert not [n for n in attrs if n.startswith("on")], (url, tag)
            assert "style" not in attrs, (url, tag)
            if tag == "script":
                assert attrs.get("src"), (url, attrs)


def test_unicode_and_huge_inputs_never_500(owner_client, accts):
    cash, rent, _ = accts
    sup = _create(owner_client, accts, line_account=["²", str(cash.id)])
    assert sup.status_code == 422 and "Choose an account" in sup.text
    huge = _create(owner_client, accts, line_account=["9" * 25, str(cash.id)])
    assert huge.status_code == 422 and "Choose an account" in huge.text
    assert owner_client.get("/journal?page=99999999999999999999").status_code == 200
    arabic = _create(owner_client, accts, line_debit=["١٠", ""])
    assert arabic.status_code == 422 and "Line 1" in arabic.text
    big = _create(
        owner_client,
        accts,
        line_debit=["1000000000000.00", ""],
        line_credit=["", "1000000000000.00"],
    )
    assert big.status_code == 422 and "too large" in big.text


def test_too_many_rows_rejected_and_zero_row_ignored(owner_client, accts):
    cash, rent, _ = accts
    n = 201
    many = _create(
        owner_client,
        accts,
        line_account=[str(cash.id)] * n,
        line_debit=["1"] * n,
        line_credit=[""] * n,
        line_memo=[""] * n,
    )
    assert many.status_code == 422 and "at most 200 lines" in many.text
    zero = _create(
        owner_client,
        accts,
        line_account=[str(rent.id), "", str(cash.id)],
        line_debit=["100", "0", ""],
        line_credit=["", "0.00", "100"],
        line_memo=["", "", ""],
    )
    assert zero.status_code == 303


def test_enormous_digit_strings_are_form_errors(owner_client, accts):
    cash, _, _ = accts
    acct = _create(owner_client, accts, line_account=["7" * 5000, str(cash.id)])
    assert acct.status_code == 422 and "Choose an account" in acct.text
    amount = _create(owner_client, accts, line_debit=["1" * 5000, ""])
    assert amount.status_code == 422 and "Line 1" in amount.text


def test_void_reversal_page_hides_buttons_and_post_refused(
    owner_client, accts, session, owner_user
):
    entry = _entry(session, owner_user, accts)
    service = LedgerPostingService(session)
    reversal = service.void(entry.id, entry_date=company_today(), actor=owner_user)
    session.commit()
    page = owner_client.get(f"/journal/{reversal.id}")
    assert page.status_code == 200 and "Reverse entry" not in page.text
    assert "Void entry" not in page.text
    response = owner_client.post(
        f"/journal/{reversal.id}/void", data={"entry_date": str(company_today())}
    )
    assert response.status_code == 409 and "reversal created by voiding" in response.text
