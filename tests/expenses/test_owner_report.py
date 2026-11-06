"""Owner-reimbursement report: figures, as-of, range, exports, escaping and access."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Vendor
from payroll.expenses.reimbursements import ReimbursementService
from payroll.expenses.reports import OwnerReport, owner_reimbursement_report
from payroll.frontend.report_export import render_print_html
from payroll.ledger.services import EntryDraft, JournalService, LineDraft
from payroll.reports.runs import run_owner_reimbursement
from tests.ledger.conftest import chart  # noqa: F401
from tests.reports.test_export import pdf_ready  # noqa: F401

D = Decimal
D1 = date(2026, 3, 1)
D2 = date(2026, 3, 10)
D3 = date(2026, 3, 20)
D4 = date(2026, 3, 25)
YEAR = (date(2026, 1, 1), date(2026, 12, 31))
PAGE = "/reports/owner-reimbursement"
FORMULA = "=cmd|' /C calc'!A0"
XSS = "<img src=x onerror=alert(1)>"


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, expense_account, card, owner_role):
    session.commit()


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 4, 15))


def owner_expense(session, owner_user, account, amount, day, **kwargs):
    kwargs.setdefault("payee_text", "Cash buy")
    expense = ExpenseService(session).create(
        actor=owner_user,
        expense_date=day,
        lines=[ExpenseLineInput(account.id, D(amount))],
        source="owner",
        paid_from_account_id=None,
        **kwargs,
    )
    session.commit()
    return expense


def reimburse(session, owner_user, chart, expenses, day):
    row = ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=day,
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[e.id for e in expenses],
    )
    session.commit()
    return row


@pytest.fixture
def books(session, owner_user, chart, expense_account):
    """Owner paid 30.00 (03-01) and 70.00 (03-10); the 30.00 was reimbursed on 03-20."""
    a = owner_expense(session, owner_user, expense_account, "30.00", D1, memo="Paper")
    b = owner_expense(session, owner_user, expense_account, "70.00", D2, memo="Toner")
    reim = reimburse(session, owner_user, chart, [a], D3)
    return a, b, reim


def report(session, as_of=D4, start=YEAR[0], end=YEAR[1]) -> OwnerReport:
    return owner_reimbursement_report(session, as_of, start, end)


def rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


# --- the figures -------------------------------------------------------------------


def test_figures_reconcile_to_the_ledger(session, books):
    a, b, reim = books
    result = report(session)
    assert (result.owed_to_me, result.open_expense_total, result.matches) == (
        D("70.00"),
        D("70.00"),
        True,
    )
    assert [
        (r.expense_id, r.total, r.memo, r.payee, r.reimbursed) for r in result.open_expenses
    ] == [(b.id, D("70.00"), "Toner", "Cash buy", False)]
    assert result.open_expenses[0].date == D2
    [row] = result.reimbursements
    assert (row.id, row.date, row.total, row.status, row.void_date, row.expense_count) == (
        reim.id,
        D3,
        D("30.00"),
        "posted",
        None,
        1,
    )
    assert row.bank_account == "Business Checking"
    assert (result.as_of, result.date_from, result.date_to) == (D4, *YEAR)


def test_vendor_name_is_the_payee_when_there_is_a_vendor(
    session, owner_user, expense_account, vendor
):
    owner_expense(
        session, owner_user, expense_account, "5.00", D1, vendor_id=vendor.id, payee_text=None
    )
    [row] = report(session).open_expenses
    assert row.payee == "Office Depot"


def test_as_of_in_the_past_excludes_later_expenses_and_reimbursements(session, books):
    a, b, _ = books
    early = report(session, as_of=date(2026, 3, 5))
    assert (early.owed_to_me, early.open_expense_total) == (D("30.00"), D("30.00"))
    assert [r.expense_id for r in early.open_expenses] == [a.id]
    before_reimbursement = report(session, as_of=date(2026, 3, 15))
    assert (before_reimbursement.owed_to_me, before_reimbursement.open_expense_total) == (
        D("100.00"),
        D("100.00"),
    )
    assert [r.expense_id for r in before_reimbursement.open_expenses] == [a.id, b.id]
    assert before_reimbursement.matches is True
    after = report(session, as_of=D3)
    assert (after.owed_to_me, after.open_expense_total) == (D("70.00"), D("70.00"))


def test_a_manual_journal_on_due_to_owner_shows_both_figures(
    session, owner_user, chart, owner_role, books
):
    JournalService(session).create_manual(
        EntryDraft(
            D4,
            [
                LineDraft(chart["Business Checking"].id, debit=D("7.00")),
                LineDraft(owner_role.id, credit=D("7.00")),
            ],
            memo="Owner put in cash",
        ),
        actor=owner_user,
    )
    session.commit()
    result = report(session)
    assert (result.owed_to_me, result.open_expense_total, result.matches) == (
        D("77.00"),
        D("70.00"),
        False,
    )


def test_reimbursements_honor_the_range_and_show_voided_ones_as_void(
    session, owner_user, chart, expense_account, books
):
    a, b, reim = books
    c = owner_expense(session, owner_user, expense_account, "12.00", D2)
    second = reimburse(session, owner_user, chart, [b, c], D4)
    ReimbursementService(session).void(second.id, actor=owner_user, void_date=date(2026, 3, 28))
    session.commit()
    window = report(session, as_of=date(2026, 4, 1), start=D4, end=D4)
    [row] = window.reimbursements
    assert (row.id, row.status, row.void_date, row.expense_count) == (
        second.id,
        "void",
        date(2026, 3, 28),
        2,
    )
    assert report(session, start=date(2026, 4, 1), end=date(2026, 4, 30)).reimbursements == []
    both = report(session)
    assert {r.id for r in both.reimbursements} == {reim.id, second.id}


# --- the page ----------------------------------------------------------------------


def test_page_shows_the_figures_and_links_the_documents(owner_client, books):
    a, b, reim = books
    response = owner_client.get(f"{PAGE}?as_of=2026-03-25&date_from=2026-01-01&date_to=2026-12-31")
    assert response.status_code == 200
    text = response.text
    assert "Owner Reimbursement" in text
    assert 'id="owed-to-me">$70.00' in text
    assert 'id="open-total">$70.00' in text
    assert f'href="/expenses/{b.id}"' in text and f'href="/expenses/{a.id}"' not in text
    assert 'href="/owner"' in text
    assert "mismatch-note" not in text
    assert 'name="as_of"' in text and 'name="date_from"' in text and 'name="date_to"' in text
    assert (
        'href="/reports/owner-reimbursement.csv?as_of=2026-03-25&amp;date_from=2026-01-01'
        '&amp;date_to=2026-12-31"'
    ) in text


def test_page_defaults_and_is_listed_on_the_index_and_owner_page(owner_client, books):
    page = owner_client.get(PAGE)
    assert page.status_code == 200
    assert "As of 2026-04-15" in page.text
    assert "2026-01-01 to 2026-04-15" in page.text
    assert f'href="{PAGE}"' in owner_client.get("/reports").text
    assert f'href="{PAGE}"' in owner_client.get("/owner").text


def test_page_flags_a_mismatch_and_shows_both_figures(
    owner_client, session, owner_user, chart, owner_role, books
):
    JournalService(session).create_manual(
        EntryDraft(
            D4,
            [
                LineDraft(chart["Business Checking"].id, debit=D("7.00")),
                LineDraft(owner_role.id, credit=D("7.00")),
            ],
        ),
        actor=owner_user,
    )
    session.commit()
    text = owner_client.get(f"{PAGE}?as_of=2026-04-01").text
    assert 'id="mismatch-note"' in text
    assert "$77.00" in text and "$70.00" in text


def test_page_escapes_payee_and_memo(owner_client, session, owner_user, expense_account):
    owner_expense(session, owner_user, expense_account, "9.00", D1, payee_text=XSS, memo=XSS)
    text = owner_client.get(PAGE).text
    assert XSS not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text


@pytest.mark.parametrize("field", ["as_of", "date_from", "date_to"])
@pytest.mark.parametrize("bad", ["garbage", "2026-02-30", "0001-01-01", "2201-01-01", "2026-1-1"])
@pytest.mark.parametrize("suffix", ["", ".csv", ".pdf"])
def test_bad_dates_are_422_never_500(owner_client, books, field, bad, suffix):
    response = owner_client.get(f"{PAGE}{suffix}?{field}={bad}")
    assert response.status_code == 422
    if not suffix:
        assert 'role="alert"' in response.text
        assert 'name="as_of"' in response.text  # the picker is still there


def test_a_range_that_ends_before_it_starts_is_422(owner_client, books):
    response = owner_client.get(f"{PAGE}?date_from=2026-03-01&date_to=2026-02-01")
    assert response.status_code == 422
    assert 'role="alert"' in response.text


def test_a_lone_earlier_date_to_defaults_the_start_to_that_year(owner_client, books):
    response = owner_client.get(f"{PAGE}?date_to=2025-06-30")
    assert response.status_code == 200
    assert "2025-01-01 to 2025-06-30" in response.text


@pytest.mark.parametrize("role_client", ["owner_client", "accountant_client", "viewer_client"])
def test_every_signed_in_role_can_read_the_page_and_the_csv(request, role_client, books):
    client = request.getfixturevalue(role_client)
    assert client.get(PAGE).status_code == 200
    assert client.get(f"{PAGE}.csv").status_code == 200


@pytest.mark.parametrize("suffix", ["", ".csv", ".pdf"])
def test_anonymous_is_refused(anon_client, suffix):
    response = anon_client.get(f"{PAGE}{suffix}", follow_redirects=False)
    assert response.status_code in (401, 303)


def test_there_is_no_api_twin(owner_client, books):
    assert owner_client.get("/api/reports/owner-reimbursement").status_code in (404, 405)
    assert owner_client.get("/api/reports/owner-reimbursement.csv").status_code in (404, 405)


# --- CSV ---------------------------------------------------------------------------


def test_csv_neutralizes_formula_payees_and_memos(
    owner_client, session, owner_user, expense_account
):
    owner_expense(session, owner_user, expense_account, "9.00", D1, payee_text=FORMULA, memo="+1")
    session.add(Vendor(name="-evil"))
    session.flush()
    vendor = session.exec(select(Vendor).where(Vendor.name == "-evil")).one()
    owner_expense(
        session,
        owner_user,
        expense_account,
        "8.00",
        D1,
        vendor_id=vendor.id,
        payee_text=None,
        memo="@SUM(A1)",
    )
    response = owner_client.get(f"{PAGE}.csv?as_of=2026-04-01")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "owner-reimbursement-2026-04-01" in response.headers["content-disposition"]
    table = rows(response.text)
    flat = [cell for row in table for cell in row]
    assert "'" + FORMULA in flat and FORMULA not in flat
    assert "'+1" in flat and "+1" not in flat
    assert "'-evil" in flat and "-evil" not in flat
    assert "'@SUM(A1)" in flat and "@SUM(A1)" not in flat


def test_csv_holds_the_figures_and_both_tables(owner_client, books):
    a, b, reim = books
    table = rows(owner_client.get(f"{PAGE}.csv?as_of=2026-03-25").text)
    assert ["Owed to me", "70.00"] in table
    assert ["Open expenses total", "70.00"] in table
    assert ["Matches", "Yes"] in table
    open_row = next(r for r in table if r[:1] == [str(b.id)])
    assert open_row == [str(b.id), "2026-03-10", "Cash buy", "Toner", "70.00"]
    reim_row = next(r for r in table if r[:1] == [str(reim.id)] and "Business Checking" in r)
    assert reim_row == [str(reim.id), "2026-03-20", "Business Checking", "30.00", "posted", "", "1"]


def test_csv_export_is_audited(owner_client, session, books):
    owner_client.get(f"{PAGE}.csv")
    row = session.exec(select(AuditLog).where(AuditLog.action == "report.export")).one()
    assert (row.entity, row.after["format"]) == ("owner-reimbursement", "csv")


# --- print HTML and PDF -----------------------------------------------------------


def test_print_html_has_the_figures_and_escapes(session, owner_user, expense_account, books):
    owner_expense(session, owner_user, expense_account, "9.00", D1, payee_text=XSS, memo=XSS)
    html = render_print_html(run_owner_reimbursement(session, {"as_of": "2026-03-25"}))
    assert "<h1>Owner Reimbursement</h1>" in html
    assert "$79.00" in html and "Business Checking" in html
    assert XSS not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_pdf_is_a_real_pdf_with_the_report_content(owner_client, books, pdf_ready):
    response = owner_client.get(f"{PAGE}.pdf?as_of=2026-03-25")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "owner-reimbursement-2026-03-25.pdf" in response.headers["content-disposition"]
    body = response.content
    assert body.startswith(b"%PDF")
    assert b"%%EOF" in body[-1024:]
    assert len(body) > 1000


def test_empty_books_render_and_export(owner_client, session):
    page = owner_client.get(PAGE)
    assert page.status_code == 200 and "Nothing is waiting to be reimbursed" in page.text
    table = rows(owner_client.get(f"{PAGE}.csv").text)
    assert ["Owed to me", "0.00"] in table
