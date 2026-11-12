"""Payroll page: ledger preview, approve, void, status in history, ``?run=`` and the garbage-input net."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

from html.parser import HTMLParser

import pytest
from sqlmodel import select

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, JournalEntry, SystemRole
from payroll.payroll_run.models import PayrollRun
from tests.frontend.test_csp_compat import _assert_csp_safe
from tests.frontend.test_pages import FORM
from tests.ledger.conftest import chart  # noqa: F401
from tests.payroll_run.conftest import bank, payroll_roles  # noqa: F401

XSS = "<img src=x onerror=alert(1)>"
HX = {"HX-Request": "true"}
APPROVE = {"pay_date": "2026-01-31", "net_pay_source": "owner"}
PLACEHOLDER = "Enter payroll details and click"


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles, year_settings):
    """Commit the fixture rows: a service rollback must not wipe them mid-test."""
    session.commit()


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.posts: list[str] = []
        self.options: list[str] = []
        self.inputs: dict[str, str | None] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if a.get("hx-post"):
            self.posts.append(a["hx-post"])
        if tag == "option":
            self.options.append(a.get("value") or "")
        if tag in ("input", "textarea") and a.get("name"):
            self.inputs[a["name"]] = a.get("value")


def parse(html: str) -> _Anchors:
    parser = _Anchors()
    parser.feed(html)
    return parser


def save_draft(client, period="2026-01", **over) -> int:
    response = client.post("/htmx/save-run", data={**FORM, "pay_period": period, **over})
    assert response.status_code == 200, response.text
    return _run_id(client, period)


def _run_id(client, period: str) -> int:
    from tests.conftest import PREFIX

    rows = client.get(f"{PREFIX}/payroll/runs").json()
    return next(r["id"] for r in rows if r["payPeriod"] == period)


def approve(client, run_id, **over):
    return client.post(f"/htmx/runs/{run_id}/approve", data={**APPROVE, **over}, headers=HX)


def void(client, run_id, **over):
    return client.post(
        f"/htmx/runs/{run_id}/void", data={"void_date": "2026-02-01", **over}, headers=HX
    )


# ---------------------------------------------------------------------------
# Draft panel: preview and Approve form
# ---------------------------------------------------------------------------


def test_draft_panel_shows_preview_and_approve_form(owner_client, bank, payroll_roles):
    run_id = save_draft(owner_client)
    html = owner_client.get(f"/htmx/runs/{run_id}").text

    assert "badge-draft" in html
    assert "Ledger preview" in html
    assert "Net pay account (chosen when approving)" in html
    assert payroll_roles[SystemRole.PAYROLL_WAGES].name in html
    assert payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE].name in html
    assert "Employer Social Security + Medicare" in html  # a preview memo

    page = parse(html)
    assert page.posts.count(f"/htmx/runs/{run_id}/approve") == 1
    assert page.inputs["pay_date"] == "2026-01-31"  # the period's last day
    assert str(bank.id) in page.options
    assert "owner" in page.options and "account" in page.options
    assert f"/htmx/runs/{run_id}/void" not in page.posts
    _assert_csp_safe(html)


def test_bank_picker_offers_only_active_unreserved_asset_accounts(
    owner_client, owner_user, session, bank, chart
):
    service = AccountService(session)
    inactive = service.create(actor=owner_user, name="Old Bank", type=AccountType.ASSET)
    service.set_active(inactive.id, False, actor=owner_user)
    reserved = session.exec(
        select(Account).where(Account.system_role == SystemRole.UNDEPOSITED_FUNDS)
    ).one()
    card = service.create(actor=owner_user, name="Plain Card", type=AccountType.LIABILITY)
    session.commit()

    run_id = save_draft(owner_client)
    options = parse(owner_client.get(f"/htmx/runs/{run_id}").text).options
    assert str(bank.id) in options
    assert str(inactive.id) not in options
    assert str(reserved.id) not in options
    assert str(card.id) not in options  # not an asset account


def test_preview_refusal_is_shown_not_a_500(owner_client, owner_user, session, payroll_roles):
    run_id = save_draft(owner_client)
    AccountService(session).bind_role(SystemRole.PAYROLL_WAGES, None, actor=owner_user)
    session.commit()
    response = owner_client.get(f"/htmx/runs/{run_id}")
    assert response.status_code == 200
    assert "payroll_wages is not bound" in response.text
    assert f"/htmx/runs/{run_id}/approve" in response.text  # approving still offers the refusal


def test_futa_and_suta_are_visible_on_the_run_panel(owner_client):
    run_id = save_draft(owner_client)
    html = owner_client.get(f"/htmx/runs/{run_id}").text
    assert "Federal Unemployment (FUTA)" in html
    assert "Georgia Unemployment (SUTA)" in html


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


def test_approve_flips_panel_and_links_the_entry(owner_client, session):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id)
    assert response.status_code == 200, response.text

    run = session.get(PayrollRun, run_id)
    assert run.status == "approved" and run.entry_id
    page = parse(response.text)
    assert f"/journal/{run.entry_id}" in page.hrefs
    assert "badge-approved" in response.text
    assert f"/htmx/runs/{run_id}/void" in page.posts
    assert f"/htmx/runs/{run_id}/approve" not in page.posts
    assert "Posted entry" in response.text
    # The ledger link works.
    assert owner_client.get(f"/journal/{run.entry_id}").status_code == 200
    # History is refreshed out of band and shows the new status.
    assert 'hx-swap-oob="innerHTML:#history-panel"' in response.text
    assert "badge-approved" in owner_client.get("/htmx/history").text
    # The panel loaded again later is the same.
    assert f"/journal/{run.entry_id}" in parse(owner_client.get(f"/htmx/runs/{run_id}").text).hrefs
    assert session.get(JournalEntry, run.entry_id) is not None


def test_approve_to_a_bank_account(owner_client, session, bank):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id, net_pay_source="account", net_pay_account_id=bank.id)
    assert response.status_code == 200, response.text
    assert session.get(PayrollRun, run_id).net_pay_account_id == bank.id
    assert bank.name in response.text  # the posted entry names the bank


def test_approve_service_refusals_are_partials_with_the_message(owner_client, session):
    jan = save_draft(owner_client, "2026-01")
    feb = save_draft(owner_client, "2026-02")
    response = approve(owner_client, feb, pay_date="2026-02-28")
    assert response.status_code == 409
    assert "2026-01 payroll run is still a draft" in response.text
    assert session.get(PayrollRun, feb).status == "draft"

    assert approve(owner_client, jan).status_code == 200
    again = approve(owner_client, jan)
    assert again.status_code == 409
    assert "only a draft can be approved" in again.text

    early = approve(owner_client, feb, pay_date="2025-12-31")
    assert early.status_code == 422
    assert "cannot be before the pay period" in early.text
    assert early.text.count('value="2025-12-31"') == 1  # typed value kept
    assert "Approve" in early.text  # the panel and form are still there


def test_approve_unbound_role_is_a_422_partial(owner_client, owner_user, session):
    run_id = save_draft(owner_client)
    AccountService(session).bind_role(SystemRole.PAYROLL_HSA_PAYABLE, None, actor=owner_user)
    session.commit()
    response = approve(owner_client, run_id)
    assert response.status_code == 422
    assert "is not bound" in response.text


def test_unknown_run_is_404(owner_client, session):
    session.commit()  # the sign-in must survive the first refusal's rollback
    assert approve(owner_client, 999999).status_code == 404
    assert void(owner_client, 999999).status_code == 404


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------


def test_void_flips_back_and_the_period_can_be_saved_again(owner_client, session):
    run_id = save_draft(owner_client)
    assert approve(owner_client, run_id).status_code == 200
    response = void(owner_client, run_id, memo="entered twice")
    assert response.status_code == 200, response.text

    run = session.get(PayrollRun, run_id)
    assert run.status == "void" and run.void_entry_id
    page = parse(response.text)
    assert "badge-void" in response.text
    assert f"/journal/{run.entry_id}" in page.hrefs
    assert f"/journal/{run.void_entry_id}" in page.hrefs
    assert f"/htmx/runs/{run_id}/void" not in page.posts
    assert "entered twice" in session.get(JournalEntry, run.void_entry_id).memo

    assert owner_client.post("/htmx/save-run", data=FORM).status_code == 200
    new = _run_id_of_status(owner_client, "draft")
    assert new != run_id
    assert approve(owner_client, new).status_code == 200


def _run_id_of_status(client, status: str) -> int:
    from tests.conftest import PREFIX

    rows = client.get(f"{PREFIX}/payroll/runs").json()
    return next(r["id"] for r in rows if r["status"] == status)


def test_void_refusals_keep_the_typed_values(owner_client, session):
    jan = save_draft(owner_client, "2026-01")
    assert approve(owner_client, jan).status_code == 200
    feb = save_draft(owner_client, "2026-02")

    blocked = void(owner_client, jan, memo=XSS)
    assert blocked.status_code == 409
    assert "2026-02 payroll run is draft after this one; delete it first" in blocked.text
    assert XSS not in blocked.text and "&lt;img src=x onerror=alert(1)&gt;" in blocked.text
    assert session.get(PayrollRun, jan).status == "approved"

    owner_client.delete(f"/htmx/runs/{feb}")
    before = void(owner_client, jan, void_date="2026-01-15")
    assert before.status_code == 422
    assert 'value="2026-01-15"' in before.text

    draft = void(owner_client, save_draft(owner_client, "2026-03"))
    assert draft.status_code == 409
    assert "only an approved run can be voided" in draft.text


# ---------------------------------------------------------------------------
# Delete and history
# ---------------------------------------------------------------------------


def test_delete_of_an_approved_run_is_a_409_partial(owner_client, session):
    run_id = save_draft(owner_client)
    assert approve(owner_client, run_id).status_code == 200
    response = owner_client.delete(f"/htmx/runs/{run_id}", headers=HX)
    assert response.status_code == 409
    assert "only a draft can be deleted" in response.text
    assert session.get(PayrollRun, run_id) is not None


def test_history_shows_status_and_deletes_only_drafts(owner_client):
    jan = save_draft(owner_client, "2026-01")
    save_draft(owner_client, "2026-02")
    approve(owner_client, jan)
    html = owner_client.get("/htmx/history").text
    assert "badge-approved" in html and "badge-draft" in html
    deletes = [tag for tag in html.split("<button") if "hx-delete" in tag.split(">")[0]]
    assert len(deletes) == 1  # only the draft can be deleted
    assert f"/htmx/runs/{jan}" in html


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_non_owners_see_status_and_preview_but_no_forms(request, role_client, owner_client):
    run_id = save_draft(owner_client)
    client = request.getfixturevalue(role_client)

    html = client.get(f"/htmx/runs/{run_id}").text
    assert "badge-draft" in html and "Ledger preview" in html
    assert "/approve" not in html and "/void" not in html
    assert 'name="pay_date"' not in html
    assert "badge-draft" in client.get("/htmx/history").text

    assert approve(client, run_id).status_code == 403
    assert void(client, run_id).status_code == 403
    page = client.get(f"/?run={run_id}").text
    assert "Ledger preview" in page and "/approve" not in page


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_non_owners_see_no_void_form_on_an_approved_run(request, role_client, owner_client):
    run_id = save_draft(owner_client)
    approve(owner_client, run_id)
    html = request.getfixturevalue(role_client).get(f"/htmx/runs/{run_id}").text
    assert "badge-approved" in html and "/void" not in html


# ---------------------------------------------------------------------------
# Garbage input is a 422, never a 500
# ---------------------------------------------------------------------------

BAD_IDS = ["abc", "0", "-1", "1.5", "99999999999", "2147483648", "%EF%BC%91", "1e3", "%20"]
BAD_DATES = [
    "yesterday",
    "2026-13-45",
    "2026-02-30",
    "2026-1-5",
    "2026-01-31T00:00:00",
    "0001-01-01",
    "9999-12-31",
    "",
    "２０２６-01-31",
]
BAD_ENUMS = ["cash", "", "Account", "owner,account", "1"]


@pytest.mark.parametrize("bad", BAD_IDS)
@pytest.mark.parametrize("action", ["approve", "void"])
def test_garbage_run_ids_are_422(owner_client, action, bad):
    form = APPROVE if action == "approve" else {"void_date": "2026-02-01"}
    response = owner_client.post(f"/htmx/runs/{bad}/{action}", data=form, headers=HX)
    assert response.status_code == 422, (bad, response.status_code)
    assert "not valid" in response.text


@pytest.mark.parametrize("bad", BAD_DATES)
def test_garbage_dates_are_422_and_kept(owner_client, bad):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id, pay_date=bad)
    assert response.status_code == 422, (bad, response.status_code)
    assert "pay date" in response.text.lower()
    assert "Approve" in response.text
    if bad:
        assert parse(response.text).inputs["pay_date"] == bad
    assert approve_missing_field(owner_client, run_id).status_code == 422

    # The same on void, with an approved run.
    assert approve(owner_client, run_id).status_code == 200
    response = void(owner_client, run_id, void_date=bad)
    assert response.status_code == 422, (bad, response.status_code)
    assert "void date" in response.text.lower()
    if bad:
        assert parse(response.text).inputs["void_date"] == bad
    no_field = owner_client.post(f"/htmx/runs/{run_id}/void", data={}, headers=HX)
    assert no_field.status_code == 422


def approve_missing_field(client, run_id):
    return client.post(f"/htmx/runs/{run_id}/approve", data={"net_pay_source": "owner"}, headers=HX)


@pytest.mark.parametrize("bad", BAD_ENUMS)
def test_garbage_net_pay_source_is_422(owner_client, bad):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id, net_pay_source=bad)
    assert response.status_code == 422, (bad, response.status_code)
    assert "net pay" in response.text.lower()


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "99999999999", "1.5", ""])
def test_garbage_bank_account_is_422(owner_client, session, bad):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id, net_pay_source="account", net_pay_account_id=bad)
    assert response.status_code == 422, (bad, response.status_code)
    assert session.get(PayrollRun, run_id).status == "draft"


def test_garbage_bank_account_is_422_even_when_paying_the_owner(owner_client):
    run_id = save_draft(owner_client)
    response = approve(owner_client, run_id, net_pay_account_id="abc")
    assert response.status_code == 422


def test_bank_account_that_cannot_pay_is_refused(owner_client, session, payroll_roles):
    run_id = save_draft(owner_client)
    liability = payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE]
    for account_id in (liability.id, 999999):
        response = approve(
            owner_client, run_id, net_pay_source="account", net_pay_account_id=account_id
        )
        assert response.status_code == 422, response.text
    assert session.get(PayrollRun, run_id).status == "draft"


@pytest.mark.parametrize("memo", ["x" * 501, "bad\x00memo", "line\x07bell", "a\u0085b"])
def test_garbage_memo_is_422_and_kept(owner_client, memo):
    run_id = save_draft(owner_client)
    approve(owner_client, run_id)
    response = void(owner_client, run_id, memo=memo)
    assert response.status_code == 422, response.status_code
    assert "Memo" in response.text


def test_memo_of_500_characters_is_accepted(owner_client):
    run_id = save_draft(owner_client)
    approve(owner_client, run_id)
    assert void(owner_client, run_id, memo="x" * 500).status_code == 200


def test_repeated_fields_do_not_crash(owner_client):
    run_id = save_draft(owner_client)
    response = owner_client.post(
        f"/htmx/runs/{run_id}/approve",
        data={"pay_date": ["2026-01-31", "2026-02-01"], "net_pay_source": ["owner", "owner"]},
        headers=HX,
    )
    assert response.status_code in (200, 422)


# ---------------------------------------------------------------------------
# XSS
# ---------------------------------------------------------------------------


def test_notes_are_escaped_in_panel_history_and_page(owner_client):
    run_id = save_draft(owner_client, notes=XSS)
    escaped = "&lt;img src=x onerror=alert(1)&gt;"
    for url in (f"/htmx/runs/{run_id}", "/htmx/history", f"/?run={run_id}", "/"):
        html = owner_client.get(url).text
        assert XSS not in html, url
        assert escaped in html, url
    approved = approve(owner_client, run_id).text
    assert XSS not in approved and escaped in approved


# ---------------------------------------------------------------------------
# GET /?run={id}
# ---------------------------------------------------------------------------


def test_page_opens_the_run_named_by_the_query(owner_client, bank):
    run_id = save_draft(owner_client)
    response = owner_client.get(f"/?run={run_id}")
    assert response.status_code == 200
    assert PLACEHOLDER not in response.text
    assert "Ledger preview" in response.text
    assert f"/htmx/runs/{run_id}/approve" in parse(response.text).posts
    _assert_csp_safe(response.text)


@pytest.mark.parametrize("raw", ["abc", "999999", "0", "-1", "1.5", "", "99999999999", "1&run=2"])
def test_page_ignores_a_bad_run_query(owner_client, raw):
    save_draft(owner_client)
    response = owner_client.get("/", params={"run": raw})
    assert response.status_code == 200
    assert PLACEHOLDER in response.text
    assert "Ledger preview" not in response.text


def test_page_ignores_a_repeated_run_query(owner_client):
    run_id = save_draft(owner_client)
    response = owner_client.get(f"/?run={run_id}&run=abc")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Static pieces
# ---------------------------------------------------------------------------


def test_lifecycle_forms_swap_error_responses_and_the_script_does_it(owner_client):
    from pathlib import Path

    run_id = save_draft(owner_client)
    assert "data-swap-errors" in owner_client.get(f"/htmx/runs/{run_id}").text
    script = Path("payroll/frontend/static/js/app.js").read_text()
    assert "htmx:beforeSwap" in script and "data-swap-errors" in script
