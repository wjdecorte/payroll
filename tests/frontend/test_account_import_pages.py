"""Import a QBO chart of accounts: upload, preview and confirm pages."""

import html

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType

CSV = (
    "Account number,Account name,Account type,Detail type\n"
    "1000,Business Checking,Bank,Checking\n"
    ",Payroll Expenses,Expenses,Payroll Expenses\n"
    ",Payroll Expenses:Wages,Expenses,Payroll Expenses\n"
    "4100,Service Revenue,Income,Service/Fee Income\n"
)


def _upload(client, text=CSV, name="chart.csv"):
    return client.post(
        "/accounts/import",
        files={"file": (name, text.encode(), "text/csv")},
        follow_redirects=False,
    )


def _confirm(client, text=CSV, lines=None):
    """Confirm the import; by default every data line of the file is ticked."""
    if lines is None:
        lines = range(2, text.count("\n") + 1)
    data = {"csv_text": text, "line": [str(n) for n in lines]}
    return client.post("/accounts/import/confirm", data=data, follow_redirects=False)


def _names(session) -> set[str]:
    session.expire_all()
    return {a.name for a in session.exec(select(Account)).all()}


def test_upload_form_renders_for_the_owner(owner_client):
    page = owner_client.get("/accounts/import")
    assert page.status_code == 200
    assert "Preview import" in page.text
    assert 'enctype="multipart/form-data"' in page.text


def test_accounts_page_links_to_the_import_for_the_owner_only(owner_client, viewer_client):
    assert "/accounts/import" in owner_client.get("/accounts").text
    assert "/accounts/import" not in viewer_client.get("/accounts").text


def test_preview_lists_every_row_and_creates_nothing(owner_client, session):
    response = _upload(owner_client)
    assert response.status_code == 200
    assert "4 can be created" in response.text
    assert "Payroll Expenses:Wages" in response.text
    assert "Import 4 accounts" in response.text
    assert response.text.count('name="line"') == 4
    assert response.text.count(" checked>") == 4
    assert "/static/js/account_import.js" in response.text
    assert _names(session) == set()


def test_confirm_creates_the_accounts_and_shows_a_notice(owner_client, session):
    response = _confirm(owner_client)
    assert response.status_code == 303
    assert response.headers["location"] == "/accounts?imported=4"
    assert _names(session) == {"Business Checking", "Payroll Expenses", "Wages", "Service Revenue"}
    tree = {n.full_name: n.account for n in AccountService(session).tree()}
    assert tree["Payroll Expenses:Wages"].type == AccountType.EXPENSE
    assert (
        "Imported 4 new accounts from QuickBooks."
        in owner_client.get(response.headers["location"]).text
    )
    audited = session.exec(select(AuditLog).where(AuditLog.action == "account.import")).all()
    assert len(audited) == 1


def test_importing_the_same_file_again_changes_nothing(owner_client, session):
    _confirm(owner_client)
    again = _upload(owner_client)
    assert "0 can be created" in again.text
    assert "4 already in the chart" in again.text
    assert 'name="line"' not in again.text  # nothing left to tick
    assert "Import 0 accounts" in again.text
    assert _confirm(owner_client, lines=[]).status_code == 422
    assert len(_names(session)) == 4


def test_only_the_ticked_accounts_are_created(owner_client, session):
    response = _confirm(owner_client, lines=[2, 5])  # Business Checking, Service Revenue
    assert response.headers["location"] == "/accounts?imported=2"
    assert _names(session) == {"Business Checking", "Service Revenue"}


def test_the_rest_can_be_imported_later(owner_client, session):
    _confirm(owner_client, lines=[2])
    assert _names(session) == {"Business Checking"}
    again = _upload(owner_client)
    assert "3 can be created" in again.text
    assert "1 already in the chart" in again.text
    _confirm(owner_client, lines=[3, 4, 5])
    assert len(_names(session)) == 4


def test_error_rows_are_shown_but_do_not_block_the_rest(owner_client, session):
    bad = CSV + ",Mystery,Spaceship,\n"
    page = _upload(owner_client, bad)
    assert page.status_code == 200
    assert "1 with errors" in page.text
    assert "Unknown account type" in page.text
    assert page.text.count('name="line"') == 4  # the error row has no checkbox
    assert "Rows marked Error will not be imported" in page.text
    response = _confirm(owner_client, bad, lines=[2, 3, 4, 5])
    assert response.status_code == 303
    assert "Mystery" not in _names(session)
    assert len(_names(session)) == 4


def test_ticking_an_error_row_is_refused(owner_client, session):
    bad = CSV + ",Mystery,Spaceship,\n"
    response = _confirm(owner_client, bad, lines=[2, 6])
    assert response.status_code == 422
    assert "Line 6 cannot be imported" in response.text
    assert _names(session) == set()


def test_a_child_without_its_parent_is_refused_and_the_ticks_are_kept(owner_client, session):
    response = _confirm(owner_client, lines=[2, 4])  # Business Checking and Wages, no parent
    assert response.status_code == 422
    assert "needs its parent 'Payroll Expenses', which is not selected" in html.unescape(
        response.text
    )
    assert response.text.count(" checked>") == 2
    assert _names(session) == set()


@pytest.mark.parametrize("lines", [[], ["abc"], ["\u00b2"], ["999"], ["-1"], ["2.0"]])
def test_nothing_or_nonsense_ticked_creates_nothing(owner_client, session, lines):
    response = owner_client.post(
        "/accounts/import/confirm",
        data={"csv_text": CSV, "line": lines},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert _names(session) == set()


def test_nothing_ticked_says_so(owner_client):
    response = _confirm(owner_client, lines=[])
    assert response.status_code == 422
    assert "Tick at least one account" in response.text
    assert "Import 0 accounts" in response.text


@pytest.mark.parametrize(
    ("text", "name", "expected"),
    [
        ("not,a,chart\n1,2,3\n", "chart.csv", "Not a QBO chart of accounts"),
        ("Account name,Account type\n", "chart.csv", "no accounts"),
        ("Account name,Account type\n" + "\xff" * 3, "chart.csv", "UTF-8"),
    ],
)
def test_bad_files_rerender_the_form_with_a_message(owner_client, text, name, expected):
    response = owner_client.post(
        "/accounts/import",
        files={"file": (name, text.encode("latin-1"), "text/csv")},
    )
    assert response.status_code == 422
    assert expected in response.text


def test_missing_file_is_a_form_error(owner_client):
    response = owner_client.post("/accounts/import", data={"x": "1"})
    assert response.status_code == 422
    assert "Choose the CSV file" in response.text


def test_oversize_file_is_refused(owner_client):
    response = _upload(owner_client, "Account name,Account type\n" + "A,Bank\n" * 100_000)
    assert response.status_code == 422
    assert "too large" in response.text


def test_account_names_are_escaped_in_the_preview(owner_client):
    xss = "<script>alert(1)</script>"
    page = _upload(owner_client, f"Account name,Account type\n{xss},Bank\n")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_viewer_and_accountant_cannot_import(viewer_client, accountant_client, session):
    for client in (viewer_client, accountant_client):
        assert client.get("/accounts/import").status_code == 403
        assert _upload(client).status_code == 403
        assert _confirm(client).status_code == 403
    assert _names(session) == set()


def test_confirm_without_a_csrf_token_is_refused(owner_client, session):
    del owner_client.headers["X-CSRF-Token"]
    response = _confirm(owner_client)
    assert response.status_code == 403
    assert _names(session) == set()


def test_anonymous_visitors_are_turned_away(anon_client):
    assert anon_client.get("/accounts/import", follow_redirects=False).status_code in (303, 401)
