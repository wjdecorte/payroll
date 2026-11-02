"""Report pages: period picker, comparison, basis, invariant banner and drill-down."""

import html
import re
from datetime import date

import pytest

from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType
from payroll.reports.periods import PRESETS, resolve_period
from payroll.reports.statements import ReportService
from tests.reports.conftest import books, chart  # noqa: F401  (fixtures)

XSS = "<script>alert(1)</script>"
PAGES = (
    "/reports",
    "/reports/profit-and-loss",
    "/reports/balance-sheet",
    "/reports/trial-balance",
    "/reports/general-ledger",
    "/reports/journal",
)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 2, 15))


@pytest.fixture
def ledger(books, session):  # noqa: F811
    session.commit()
    return books


def _hrefs(text: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


def test_index_lists_all_six_reports(owner_client):
    page = owner_client.get("/reports")
    assert page.status_code == 200
    for path in PAGES[1:]:
        assert f'href="{path}"' in page.text


def test_default_pnl_is_cash_this_month(owner_client, ledger):
    page = owner_client.get("/reports/profit-and-loss")
    assert page.status_code == 200
    text = page.text
    assert "Profit and Loss" in text
    assert "2026-02-01 to 2026-02-28" in text
    assert "$2,500.00" in text and "$300.25" in text and "$2,199.75" in text
    assert "% change" not in text
    assert re.search(r'value="cash"[^>]*checked', text)
    assert "Balanced" in text
    assert ledger.chart["Services Revenue"].name in text


def test_presets_and_custom_change_heading(owner_client, ledger, monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 5, 10))
    last_q = owner_client.get("/reports/profit-and-loss?preset=last_quarter").text
    assert "2026-01-01 to 2026-03-31" in last_q and "$4,999.25" in last_q
    custom = owner_client.get(
        "/reports/profit-and-loss?preset=custom&start=2026-01-01&end=2026-01-31"
    ).text
    assert "2026-01-01 to 2026-01-31" in custom and "$2,799.50" in custom


def test_compare_previous_period_and_year(owner_client, ledger):
    prev = owner_client.get("/reports/profit-and-loss?compare=previous_period").text
    assert "2026-01-01 to 2026-01-31" in prev
    assert "% change" in prev and "$2,799.50" in prev
    year = owner_client.get("/reports/profit-and-loss?compare=previous_year").text
    assert "2025-02-01 to 2025-02-28" in year and "% change" in year


def test_accrual_basis_selected_and_note_shown(owner_client, ledger):
    page = owner_client.get("/reports/profit-and-loss?basis=accrual").text
    assert re.search(r'value="accrual"[^>]*checked', page)
    assert "Cash and accrual agree" in page


@pytest.mark.parametrize(
    "query",
    [
        "preset=bogus",
        "preset=custom",
        "preset=custom&start=2026-03-01&end=2026-01-01",
        "preset=custom&start=nope&end=2026-01-01",
        "preset=custom&start=2026-01-01&end=2026-13-45",
        "basis=weekly",
        "compare=next_year",
        "preset=%00&basis=%ff",
    ],
)
def test_invalid_pnl_input_is_inline_422(owner_client, ledger, query):
    response = owner_client.get(f"/reports/profit-and-loss?{query}")
    assert response.status_code == 422
    assert 'role="alert"' in response.text
    assert "Traceback" not in response.text
    assert 'name="preset"' in response.text  # the picker is still there


@pytest.mark.parametrize(
    "path",
    [
        "/reports/balance-sheet?asOf=garbage",
        "/reports/balance-sheet?compare=zzz",
        "/reports/balance-sheet?basis=zzz",
        "/reports/trial-balance?asOf=2026-02-30",
        "/reports/general-ledger?preset=custom&start=2026-02-01&end=2026-01-01",
        "/reports/general-ledger?account=abc",
        "/reports/general-ledger?account=" + "9" * 400,
        "/reports/general-ledger?account=-1",
        "/reports/journal?preset=nope",
        "/reports/journal?preset=custom&start=x&end=y",
    ],
)
def test_invalid_input_on_other_reports_is_422(owner_client, ledger, path):
    response = owner_client.get(path)
    assert response.status_code == 422
    assert 'role="alert"' in response.text


def test_missing_general_ledger_account_is_404(owner_client, ledger):
    assert owner_client.get("/reports/general-ledger?account=99999").status_code == 404


def test_account_rows_drill_down_to_register_with_same_total(owner_client, ledger):
    page = owner_client.get("/reports/profit-and-loss").text
    sales = ledger.chart["Services Revenue"].id
    link = f"/accounts/{sales}/register?start=2026-02-01&end=2026-02-28"
    assert link in _hrefs(page)
    register = owner_client.get(link)
    assert register.status_code == 200 and "$2,500.00" in register.text


def test_balance_sheet_defaults_to_today_and_links_from_fiscal_year_start(
    owner_client, ledger, session
):
    page = owner_client.get("/reports/balance-sheet").text
    assert "As of 2026-02-15" in page
    chk = ledger.chart["Business Checking"].id
    assert f"/accounts/{chk}/register?start=2026-01-01&end=2026-02-15" in _hrefs(page)
    statement = ReportService(session)
    rows = statement.balance_sheet(date(2026, 2, 15)).rows
    linked = [h for h in _hrefs(page) if h.startswith("/accounts/")]
    assert len(linked) == len([r for r in rows if r.account_id is not None])
    assert any(r.account_id is None and r.kind == "account" for r in rows)  # synthesized equity


def test_balance_sheet_compare_columns(owner_client, ledger):
    page = owner_client.get("/reports/balance-sheet?compare=previous_period").text
    assert "As of 2026-01-15" in page and "% change" in page


def test_invariant_banner_turns_red(owner_client, ledger, monkeypatch):
    from payroll.reports.schemas import Invariant

    monkeypatch.setattr(
        ReportService,
        "_invariants",
        lambda self, *a, **k: [Invariant(name="Equation", ok=False, detail="Off by $1.00")],
    )
    page = owner_client.get("/reports/balance-sheet").text
    assert "Off by $1.00" in page
    assert "alert-danger" in page and "Balanced" not in page


def test_trial_balance(owner_client, ledger):
    page = owner_client.get("/reports/trial-balance?asOf=2026-12-31").text
    assert "$16,500.00" in page and "$14,999.25" in page and "Balanced" in page
    chk = ledger.chart["Business Checking"].id
    assert f"/accounts/{chk}/register?start=2026-01-01&end=2026-12-31" in _hrefs(page)
    default = owner_client.get("/reports/trial-balance").text
    assert "2026-02-15" in default


def test_general_ledger_and_account_filter(owner_client, ledger):
    q = "preset=custom&start=2026-01-01&end=2026-12-31"
    page = owner_client.get(f"/reports/general-ledger?{q}").text
    assert "Services Revenue" in page and "Office Expenses" in page
    assert f"/journal/{ledger.sale_jan.id}" in _hrefs(page)
    sales = ledger.chart["Services Revenue"].id
    one = owner_client.get(f"/reports/general-ledger?{q}&account={sales}").text
    assert "Services Revenue" in one and "Office Expenses</h" not in one
    assert f"/journal/{ledger.office.id}" not in _hrefs(one)


def test_journal_report(owner_client, ledger):
    page = owner_client.get("/reports/journal?preset=custom&start=2026-01-01&end=2026-12-31").text
    assert "January sale" in page and "$4,000.00" in page
    assert f"/journal/{ledger.sale_jan.id}" in _hrefs(page)
    assert "Reversal" in page


@pytest.mark.parametrize("client_name", ["viewer_client", "accountant_client"])
def test_pages_open_to_every_role_and_not_cached(client_name, request, ledger):
    client = request.getfixturevalue(client_name)
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["Cache-Control"] == "no-store", path


def test_anonymous_redirected_to_login(anon_client):
    for path in PAGES:
        response = anon_client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path


def test_account_name_is_escaped(owner_client, session, owner_user, ledger):
    acct = AccountService(session).create(actor=owner_user, name=XSS, type=AccountType.EXPENSE)
    ledger.chart[XSS] = acct
    ledger.post(date(2026, 2, 12), XSS, "Business Checking", "5.00", f"memo {XSS}")
    session.commit()
    for path in PAGES[1:]:
        q = "&preset=custom&start=2026-02-01&end=2026-02-28" if "balance" not in path else ""
        text = owner_client.get(f"{path}?x=1{q}&asOf=2026-02-28").text
        assert XSS not in text, path
        if "balance" not in path:  # an expense account never appears on the balance sheet
            assert "&lt;script&gt;" in text, path


def test_presets_all_render(owner_client, ledger):
    for preset in PRESETS:
        assert owner_client.get(f"/reports/profit-and-loss?preset={preset}").status_code == 200
    assert resolve_period("this_month", date(2026, 2, 15)).start == date(2026, 2, 1)


def test_pages_have_no_inline_script_or_style(owner_client, ledger):
    for path in PAGES:
        text = owner_client.get(path).text
        assert " style=" not in text and " onclick=" not in text
        assert not re.search(r"<script(?![^>]*\bsrc=)", text)


OUT_OF_RANGE = ["0001-01-01", "9999-12-31", "1899-12-31", "2201-01-01"]
COMPARES = ["", "previous_period", "previous_year"]


@pytest.mark.parametrize("day", OUT_OF_RANGE)
@pytest.mark.parametrize("compare", COMPARES)
def test_out_of_range_dates_are_422_on_every_report(owner_client, ledger, day, compare):
    cmp = f"&compare={compare}" if compare else ""
    paths = [
        f"/reports/profit-and-loss?preset=custom&start={day}&end={day}{cmp}",
        f"/reports/profit-and-loss?preset=custom&start=2026-01-01&end={day}{cmp}",
        f"/reports/balance-sheet?asOf={day}{cmp}",
        f"/reports/trial-balance?asOf={day}",
        f"/reports/general-ledger?preset=custom&start={day}&end={day}",
        f"/reports/journal?preset=custom&start={day}&end={day}",
        f"/accounts/{ledger.chart['Travel'].id}/register?start={day}&end={day}",
        f"/payroll/api/v1/ledger/reports/trial-balance?asOf={day}",
        f"/payroll/api/v1/ledger/reports/balance-sheet?asOf={day}{cmp}",
        f"/payroll/api/v1/ledger/reports/profit-and-loss?start={day}&end={day}{cmp}",
        f"/payroll/api/v1/ledger/reports/general-ledger?start={day}&end={day}",
        f"/payroll/api/v1/ledger/reports/journal?start={day}&end={day}",
        f"/payroll/api/v1/ledger/reports/account-register/{ledger.chart['Travel'].id}?start={day}&end={day}",
    ]
    for path in paths:
        assert owner_client.get(path).status_code == 422, path


@pytest.mark.parametrize("compare", ["previous_period", "previous_year"])
def test_comparison_running_out_of_range_is_422(owner_client, ledger, compare):
    page = owner_client.get(
        f"/reports/profit-and-loss?preset=custom&start=1900-01-01&end=1900-01-31&compare={compare}"
    )
    assert page.status_code == 422
    assert (
        owner_client.get(f"/reports/balance-sheet?asOf=1900-01-15&compare={compare}").status_code
        == 422
    )


def test_boundary_dates_still_work(owner_client, ledger):
    for path in (
        "/reports/profit-and-loss?preset=custom&start=1900-01-01&end=2200-12-31",
        "/reports/balance-sheet?asOf=1900-01-01",
        "/reports/balance-sheet?asOf=2200-12-31&compare=previous_year",
        "/reports/trial-balance?asOf=2200-12-31",
        "/reports/general-ledger?preset=custom&start=1900-01-01&end=2200-12-31",
        "/reports/journal?preset=custom&start=1900-01-01&end=2200-12-31",
    ):
        assert owner_client.get(path).status_code == 200, path


@pytest.mark.parametrize("day", ["1899-12-31", "2201-01-01"])
def test_journal_entry_form_and_service_reject_unsupported_date(owner_client, ledger, session, day):
    from payroll.exceptions import AppBaseError
    from payroll.ledger.exceptions import InvalidEntryError

    form = {
        "entry_date": day,
        "memo": "x",
        "line_account": [str(ledger.chart["Travel"].id), str(ledger.chart["Business Checking"].id)],
        "line_debit": ["5.00", ""],
        "line_credit": ["", "5.00"],
        "line_memo": ["", ""],
    }
    assert owner_client.post("/journal", data=form).status_code == 422
    with pytest.raises(InvalidEntryError):
        ledger.post(date.fromisoformat(day), "Travel", "Business Checking", "5.00")
    with pytest.raises((InvalidEntryError, AppBaseError)):
        ledger.posting.reverse(
            ledger.sale_jan.id, entry_date=date.fromisoformat(day), actor=ledger.owner
        )
