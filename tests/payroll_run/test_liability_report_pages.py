"""Payroll liability report page, CSV and PDF: figures equal the service, strict input, escaping."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.ledger.models import SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.payroll_run.liability import payroll_liability_report
from payroll.payroll_run.posting import PayrollPostingService
from payroll.payroll_run.tax_payments import TaxPaymentService
from tests.frontend.test_csp_compat import _assert_csp_safe
from tests.ledger.conftest import chart  # noqa: F401
from tests.payroll_run.conftest import bank, payroll_roles  # noqa: F401
from tests.payroll_run.test_posting import approve, save
from tests.reports.test_export import pdf_ready  # noqa: F401

D = Decimal
PAGE = "/reports/payroll-liability"
AS_OF = date(2026, 3, 31)
QUERY = "?as_of=2026-03-31"


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, payroll_roles, year_settings, bank):
    session.commit()


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: AS_OF)


@pytest.fixture
def books(session, owner_user, bank, payroll_roles):
    """Two approved runs, a part payment to the IRS, and a manual credit that is no run's."""
    posting = PayrollPostingService(session)
    jan = save(session, owner_user, period="2026-01")
    approve(posting, owner_user, jan, bank)
    feb = save(session, owner_user, period="2026-02", use_previous_ytd_fica=True)
    approve(posting, owner_user, feb, bank)
    TaxPaymentService(session).create(
        actor=owner_user,
        payment_date=date(2026, 2, 20),
        agency="irs_941",
        bank_account_id=bank.id,
        amount=D("100.00"),
    )
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 3, 1),
            [
                LineDraft(bank.id, debit=D("7.00")),
                LineDraft(payroll_roles[SystemRole.PAYROLL_GA_WH_PAYABLE].id, credit=D("7.00")),
            ],
            memo="opening",
        ),
        actor=owner_user,
    )
    session.commit()


def money(value: Decimal) -> str:
    return f"${abs(value):,.2f}"


def rows_of(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def test_page_shows_the_services_figures(owner_client, session, books):
    report = payroll_liability_report(session, AS_OF)
    response = owner_client.get(PAGE + QUERY)
    assert response.status_code == 200
    _assert_csp_safe(response.text)
    text = response.text
    assert "Georgia DOR (withholding)" in text and "IRS (Form 941)" in text
    for agency in report.agencies:
        assert agency.label in text
        assert money(agency.balance) in text
        assert money(agency.other) in text
        if agency.next_due:
            assert agency.next_due.isoformat() in text
        for period in agency.periods:
            for figure in (period.liability, period.paid, period.unpaid):
                assert money(figure) in text
            assert period.due_date.isoformat() in text
    irs = next(a for a in report.agencies if a.agency == "irs_941")
    assert [p.pay_period for p in irs.periods] == ["2026-01", "2026-02"]
    assert irs.periods[0].paid == D("100.00") and irs.periods[0].overdue
    assert "Overdue" in text
    ga = next(a for a in report.agencies if a.agency == "ga_dor")
    assert ga.other == D("7.00")


def test_other_line_is_shown_even_at_zero(owner_client, session, books):
    text = owner_client.get(PAGE + "?as_of=2025-06-30").text
    assert text.count("Other (not explained by pay periods)") == 4
    assert "$0.00" in text


def test_default_as_of_is_today(owner_client, session, books):
    page = owner_client.get(PAGE)
    assert page.status_code == 200
    assert f"As of {AS_OF.isoformat()}" in page.text
    assert 'name="as_of" value="2026-03-31"' in page.text


@pytest.mark.parametrize(
    "bad", ["garbage", "2026-13-01", "2026-3-1", "20260331", "2026-03-31T00:00", "0001-01-01"]
)
def test_bad_as_of_is_a_422_everywhere(owner_client, bad):
    for suffix in ("", ".csv", ".pdf"):
        response = owner_client.get(f"{PAGE}{suffix}?as_of={bad}")
        assert response.status_code == 422, (suffix, bad)
    page = owner_client.get(f"{PAGE}?as_of={bad}")
    assert 'role="alert"' in page.text and 'name="as_of"' in page.text


def test_failed_export_writes_no_audit_row(owner_client, session):
    owner_client.get(f"{PAGE}.csv?as_of=nope")
    assert session.exec(select(AuditLog).where(AuditLog.action == "report.export")).all() == []


def test_csv_equals_the_service(owner_client, session, books):
    report = payroll_liability_report(session, AS_OF)
    response = owner_client.get(f"{PAGE}.csv{QUERY}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="payroll-liability-2026-03-31.csv"'
    )
    rows = rows_of(response.text)
    assert rows[0] == ["Payroll Liability"] and rows[1] == ["As of", "2026-03-31"]
    assert rows[3] == [
        "Agency",
        "Row",
        "Pay period",
        "Due date",
        "Liability",
        "Paid",
        "Unpaid",
        "Overdue",
    ]
    for agency in report.agencies:
        mine = [r for r in rows[4:] if r[0] == agency.label]
        assert all(len(r) == 8 for r in mine)
        by_kind = {kind: [r for r in mine if r[1] == kind] for kind in ("Balance", "Other")}
        # Balance sits under Liability, Other under Unpaid; the date column stays empty.
        assert by_kind["Balance"] == [
            [agency.label, "Balance", "", "", f"{agency.balance:f}", "", "", ""]
        ]
        assert by_kind["Other"] == [
            [agency.label, "Other", "", "", "", "", f"{agency.other:f}", ""]
        ]
        next_due = agency.next_due.isoformat() if agency.next_due else ""
        assert [r for r in mine if r[1] == "Next due"] == [
            [agency.label, "Next due", "", next_due, "", "", "", ""]
        ]
        periods = [r for r in mine if r[1] == "Period"]
        assert periods == [
            [
                agency.label,
                "Period",
                p.pay_period,
                p.due_date.isoformat(),
                f"{p.liability:f}",
                f"{p.paid:f}",
                f"{p.unpaid:f}",
                "Yes" if p.overdue else "No",
            ]
            for p in agency.periods
        ]
        # The Unpaid column over Period and Other rows adds up to the balance.
        unpaid = sum(D(r[6]) for r in mine if r[1] in ("Period", "Other"))
        assert unpaid == agency.balance
    irs = next(a for a in report.agencies if a.agency == "irs_941")
    assert irs.next_due is not None  # the Next due assertion above is not vacuous
    audit = session.exec(select(AuditLog).where(AuditLog.action == "report.export")).all()
    assert [a.after["format"] for a in audit] == ["csv"]


def test_csv_neutralises_formula_text(owner_client, session, monkeypatch):
    from payroll.payroll_run import liability

    hostile = liability.AgencyLiability("irs_941", "=HYPERLINK(1)", D("1.00"), [], D("1.00"), None)
    fake = liability.LiabilityReport(AS_OF, [hostile])
    monkeypatch.setattr("payroll.reports.runs.payroll_liability_report", lambda s, d: fake)
    text = owner_client.get(f"{PAGE}.csv{QUERY}").text
    assert "'=HYPERLINK(1)" in text
    assert "\r\n=HYPERLINK" not in text and ",=HYPERLINK" not in text


def test_text_is_escaped_on_the_page_and_in_print_html(owner_client, session, monkeypatch):
    from payroll.frontend.report_export import render_print_html
    from payroll.payroll_run import liability
    from payroll.reports.runs import run_payroll_liability

    xss = "<script>alert(1)</script>"
    fake = liability.LiabilityReport(
        AS_OF, [liability.AgencyLiability("irs_941", xss, D("1.00"), [], D("1.00"), None)]
    )
    monkeypatch.setattr("payroll.reports.runs.payroll_liability_report", lambda s, d: fake)
    page = owner_client.get(PAGE + QUERY).text
    assert xss not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    html = render_print_html(run_payroll_liability(session, {"as_of": "2026-03-31"}))
    assert xss not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<h1>Payroll Liability</h1>" in html and "<script" not in html


def test_print_html_has_the_figures(session, books):
    from payroll.frontend.report_export import render_print_html
    from payroll.reports.runs import run_payroll_liability

    report = payroll_liability_report(session, AS_OF)
    html = render_print_html(run_payroll_liability(session, {"as_of": "2026-03-31"}))
    for agency in report.agencies:
        assert agency.label in html and money(agency.balance) in html
        for p in agency.periods:
            assert money(p.unpaid) in html and p.pay_period in html


def test_pdf_is_a_real_pdf(owner_client, session, books, pdf_ready):
    response = owner_client.get(f"{PAGE}.pdf{QUERY}")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="payroll-liability-2026-03-31.pdf"'
    )


@pytest.mark.parametrize("client_name", ["viewer_client", "accountant_client"])
def test_viewers_and_accountants_can_read(request, client_name, books):
    client = request.getfixturevalue(client_name)
    assert client.get(PAGE + QUERY).status_code == 200
    assert client.get(f"{PAGE}.csv{QUERY}").status_code == 200


def test_anonymous_is_refused(anon_client):
    for suffix in ("", ".csv", ".pdf"):
        response = anon_client.get(f"{PAGE}{suffix}", follow_redirects=False)
        assert response.status_code in (401, 303)


def test_reports_index_links_the_report(owner_client):
    text = owner_client.get("/reports").text
    assert f'href="{PAGE}"' in text and "Payroll Liability" in text


def test_no_api_twin(owner_client):
    assert (
        owner_client.get("/payroll/api/v1/ledger/reports/payroll-liability.csv").status_code == 404
    )
