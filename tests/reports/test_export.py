"""CSV and PDF export: formula-injection safety, no-fetch PDFs, auditing, filenames."""

import csv
import io
import re
import socket
import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.ledger.models import AccountType, SourceType
from payroll.reports.export import csv_safe, to_csv, to_pdf
from payroll.reports.periods import resolve_period
from payroll.reports.schemas import (
    ColumnHeader,
    GeneralLedger,
    Invariant,
    JournalReport,
    JournalReportEntry,
    JournalReportLine,
    Statement,
    StatementRow,
    TrialBalance,
    TrialBalanceRow,
)
from payroll.reports.statements import ReportService

PAGE = "/reports"
API = "/payroll/api/v1/ledger/reports"
FORMULA = '=HYPERLINK("http://x","y")'


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 2, 15))


@pytest.fixture
def ledger(books, session):
    session.commit()
    return books


@pytest.fixture(scope="session")
def pdf_ready():
    """Skip PDF tests when WeasyPrint's native libraries (Pango) cannot be loaded."""
    try:
        import weasyprint  # noqa: F401
    except (OSError, ImportError) as exc:
        pytest.skip(f"WeasyPrint native libraries unavailable: {exc}")


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


def _entry(memo: str | None, account: str, amount: str) -> JournalReport:
    value = Decimal(amount)
    line = JournalReportLine(
        line_no=1,
        account_id=1,
        account_number="1000",
        account_name=account,
        debit=value if value > 0 else Decimal(0),
        credit=-value if value < 0 else Decimal(0),
        memo=memo,
    )
    entry = JournalReportEntry(
        id=1,
        entry_no=1,
        source_type=SourceType.MANUAL,
        entry_date=date(2026, 1, 5),
        memo=memo,
        status="posted",
        is_void=False,
        is_reversal=False,
        reverses_entry_id=None,
        total_debit=abs(value),
        total_credit=abs(value),
        lines=[line],
    )
    return JournalReport(start=date(2026, 1, 1), end=date(2026, 1, 31), entries=[entry])


# --- csv_safe -----------------------------------------------------------------


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_safe_prefixes_dangerous_leading_characters(lead):
    assert csv_safe(f"{lead}cmd") == f"'{lead}cmd"


@pytest.mark.parametrize("value", ["", "plain", "a=b", " =x", "'=x", "1000 Cash"])
def test_csv_safe_leaves_other_text_alone(value):
    assert csv_safe(value) == value


# --- CSV content --------------------------------------------------------------


def test_csv_has_title_period_columns_and_literal_totals(session, ledger):
    trial = ReportService(session).trial_balance(date(2026, 12, 31))
    text = to_csv(trial)
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    rows = _rows(text)
    assert rows[0] == ["Trial Balance"]
    assert rows[1] == ["As of", "2026-12-31"]
    assert rows[2] == []
    assert rows[3] == ["Account", "Debit", "Credit"]
    assert rows[4] == ["1000 Business Checking", "14999.25", ""]
    assert rows[-1] == ["Total", "16500.00", "16500.00"]


def test_csv_statement_columns_totals_and_comparison(session, ledger):
    statement = ReportService(session).profit_and_loss(
        resolve_period("custom", start=date(2026, 2, 1), end=date(2026, 2, 28)),
        compare="previous_period",
    )
    rows = _rows(to_csv(statement))
    assert rows[0] == ["Profit and Loss"]
    assert any(r[:1] == ["Basis"] and r[1] == "Cash" for r in rows)
    header = next(r for r in rows if r[:1] == ["Profit and Loss"] and len(r) > 1)
    assert header[-2:] == ["Change", "% change"]
    net = next(r for r in rows if r[:1] == ["Net Income"])
    assert net[1] == "2199.75"  # literal, no currency symbol or thousands separator
    assert ["Checks"] in rows
    assert all(r[1] == "OK" for r in rows[rows.index(["Checks"]) + 1 :])


def test_csv_general_ledger_and_journal_have_period_and_columns(session, ledger):
    service = ReportService(session)
    gl = _rows(to_csv(service.general_ledger(date(2026, 1, 1), date(2026, 2, 28), None)))
    assert gl[0] == ["General Ledger"]
    assert gl[1] == ["Period", "2026-01-01", "to", "2026-02-28"]
    assert ["Date", "Entry", "Memo", "Debit", "Credit", "Balance", "Status"] in gl
    journal = _rows(
        to_csv(
            JournalReport(
                start=date(2026, 1, 1),
                end=date(2026, 2, 28),
                entries=service.journal(date(2026, 1, 1), date(2026, 2, 28)),
            )
        )
    )
    assert journal[0] == ["Journal"]
    assert journal[1] == ["Period", "2026-01-01", "to", "2026-02-28"]
    assert any(r[:2] == ["2026-01-02", "1"] for r in journal)


def test_csv_neutralizes_formula_cells():
    text = to_csv(_entry(FORMULA, "@SUM(1)", "-12.00"))
    cells = [c for row in _rows(text) for c in row]
    assert "'" + FORMULA in cells
    assert FORMULA not in cells
    assert "-12.00" not in cells  # a credit: positive in its own column
    # The account cell is "<number> <name>"; without a number the name leads, so it is guarded.
    named = _entry(None, "x", "5.00")
    named.entries[0].lines[0].account_number = None
    named.entries[0].lines[0].account_name = "@SUM(1)"
    assert "'@SUM(1)" in [c for row in _rows(to_csv(named)) for c in row]


def test_csv_leaves_negative_money_untouched():
    statement = Statement(
        title="Profit and Loss",
        basis="cash",
        columns=[ColumnHeader(label="Jan", start=date(2026, 1, 1), end=date(2026, 1, 31))],
        rows=[
            StatementRow(
                kind="account",
                label="-Refunds",
                account_id=None,
                depth=0,
                amounts=[Decimal("-12.00")],
                change=None,
                pct_change=None,
            )
        ],
        invariants=[Invariant(name="x", ok=True, detail="=ok")],
        basis_note="note",
    )
    cells = [c for row in _rows(to_csv(statement)) for c in row]
    assert "-12.00" in cells and "'-12.00" not in cells
    assert "'-Refunds" in cells
    assert "'=ok" in cells


def test_csv_quotes_commas_quotes_and_newlines():
    memo = 'rent, "March"\nsecond line'
    text = to_csv(_entry(memo, "Acct, Inc", "10.00"))
    assert '"rent, ""March""\nsecond line"' in text
    cells = [c for row in _rows(text) for c in row]
    assert memo in cells


def test_csv_trial_balance_row_text_is_neutralized():
    trial = TrialBalance(
        as_of=date(2026, 1, 31),
        rows=[
            TrialBalanceRow(
                account_id=1,
                number=None,
                name="=1+1",
                type=AccountType.ASSET,
                debit=Decimal("1.00"),
                credit=Decimal("0.00"),
            )
        ],
        total_debit=Decimal("1.00"),
        total_credit=Decimal("1.00"),
        balanced=True,
    )
    assert "'=1+1" in [c for row in _rows(to_csv(trial)) for c in row]


def test_csv_empty_general_ledger():
    rows = _rows(to_csv(GeneralLedger(start=date(2026, 1, 1), end=date(2026, 1, 31), accounts=[])))
    assert rows[0] == ["General Ledger"]


def test_to_csv_rejects_unknown_report():
    with pytest.raises(TypeError):
        to_csv(object())


# --- Routes: CSV --------------------------------------------------------------


def test_csv_route_headers_and_body_match_page(owner_client, ledger):
    response = owner_client.get(
        f"{PAGE}/profit-and-loss.csv?preset=custom&start=2026-02-01&end=2026-02-28"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        'attachment; filename="profit-and-loss-2026-02-01_to_2026-02-28.csv"'
    )
    rows = _rows(response.text)
    net = next(r for r in rows if r[:1] == ["Net Income"])
    assert net[1] == "2199.75"
    page = owner_client.get(f"{PAGE}/profit-and-loss?preset=custom&start=2026-02-01&end=2026-02-28")
    assert "$2,199.75" in page.text


@pytest.mark.parametrize(
    "path,stem",
    [
        ("profit-and-loss?preset=ytd", "profit-and-loss-2026-01-01_to_2026-02-15"),
        ("balance-sheet?asOf=2026-02-28", "balance-sheet-2026-02-28"),
        ("trial-balance?asOf=2026-02-28", "trial-balance-2026-02-28"),
        ("general-ledger?preset=last_month", "general-ledger-2026-01-01_to_2026-01-31"),
        ("journal?preset=this_month", "journal-2026-02-01_to_2026-02-28"),
    ],
)
def test_every_report_exports_csv_with_page_and_api_urls(owner_client, ledger, path, stem):
    name, _, query = path.partition("?")
    for base in (f"{PAGE}/{name}.csv", f"{API}/{name}.csv"):
        response = owner_client.get(f"{base}?{query}")
        assert response.status_code == 200, (base, response.text)
        assert response.headers["content-disposition"] == f'attachment; filename="{stem}.csv"'
        assert response.headers["cache-control"] == "no-store"


def test_csv_route_neutralizes_stored_memo(owner_client, books, session):
    books.post(date(2026, 2, 12), "Office Expenses", "Business Checking", "5.00", memo=FORMULA)
    session.commit()
    response = owner_client.get(f"{PAGE}/journal.csv?preset=this_month")
    assert response.status_code == 200
    cells = [c for row in _rows(response.text) for c in row]
    assert "'" + FORMULA in cells
    assert FORMULA not in cells


def test_export_uses_page_validation(owner_client, ledger):
    for query in (
        "preset=nope",
        "preset=custom&start=2026-13-01&end=2026-01-01",
        "preset=custom&start=0001-01-01&end=2026-01-01",
        "preset=custom&start=2026-01-01&end=9999-01-01",
        "basis=weird",
        "compare=weird",
    ):
        response = owner_client.get(f"{PAGE}/profit-and-loss.csv?{query}")
        assert response.status_code == 422, query
        assert "attachment" not in response.headers.get("content-disposition", "")
    assert owner_client.get(f"{PAGE}/general-ledger.csv?account=abc").status_code == 422
    assert owner_client.get(f"{PAGE}/trial-balance.csv?asOf=nonsense").status_code == 422
    assert owner_client.get(f"{API}/balance-sheet.csv?asOf=9999-12-31").status_code == 422


def test_failed_export_writes_no_audit_row(owner_client, ledger, session):
    owner_client.get(f"{PAGE}/profit-and-loss.csv?preset=nope")
    assert session.exec(select(AuditLog).where(AuditLog.action == "report.export")).all() == []


def test_export_requires_login(anon_client):
    for suffix in ("csv", "pdf"):
        for path in (
            f"{PAGE}/profit-and-loss.{suffix}",
            f"{PAGE}/trial-balance.{suffix}",
            f"{API}/journal.{suffix}",
        ):
            response = anon_client.get(path, follow_redirects=False)
            assert response.status_code in (303, 401), path
            assert "attachment" not in response.headers.get("content-disposition", "")


def test_viewer_can_export_and_it_is_audited(viewer_client, viewer_user, ledger, session):
    response = viewer_client.get(f"{PAGE}/trial-balance.csv?asOf=2026-02-28")
    assert response.status_code == 200
    rows = session.exec(select(AuditLog).where(AuditLog.action == "report.export")).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == viewer_user.id
    assert row.entity == "trial-balance"
    assert row.after == {"asOf": "2026-02-28", "format": "csv"}


def test_audit_records_validated_parameters(accountant_client, ledger, session):
    accountant_client.get(
        f"{API}/profit-and-loss.csv?preset=ytd&basis=accrual&compare=previous_year"
    )
    row = session.exec(select(AuditLog).where(AuditLog.action == "report.export")).one()
    assert row.entity == "profit-and-loss"
    assert row.after == {
        "preset": "ytd",
        "start": "2026-01-01",
        "end": "2026-02-15",
        "basis": "accrual",
        "compare": "previous_year",
        "format": "csv",
    }


def test_export_rolls_back_when_commit_fails(owner_client, ledger, monkeypatch):
    from sqlmodel import Session

    def boom(self):
        raise RuntimeError("commit failed")

    rolled_back = []
    original = Session.rollback
    monkeypatch.setattr(Session, "commit", boom)
    monkeypatch.setattr(
        Session, "rollback", lambda self: (rolled_back.append(1), original(self))[1]
    )
    owner_client.raise_server_exceptions = False
    response = owner_client.get(f"{PAGE}/trial-balance.csv?asOf=2026-02-28")
    assert response.status_code == 500
    assert rolled_back


def test_filenames_contain_no_user_input_beyond_dates(owner_client, ledger):
    response = owner_client.get(
        f'{PAGE}/general-ledger.csv?preset=this_month&account=&name="evil.csv&x=../../etc'
        "&filename=pwn"
    )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(
        r'attachment; filename="[a-z-]+-\d{4}-\d{2}-\d{2}(_to_\d{4}-\d{2}-\d{2})?\.csv"',
        disposition,
    )
    assert "evil" not in disposition and "pwn" not in disposition


# --- print template -----------------------------------------------------------


def test_print_html_escapes_text(owner_client, books, session):
    from payroll.frontend.report_export import render_print_html
    from payroll.reports.runs import run_journal

    xss = "<script>alert(1)</script>"
    books.post(date(2026, 2, 12), "Office Expenses", "Business Checking", "5.00", memo=xss)
    session.commit()
    html = render_print_html(run_journal(session, {"preset": "this_month"}))
    assert xss not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<link" not in html and "<img" not in html


def test_print_html_renders_every_report(owner_client, ledger, session):
    from payroll.frontend.report_export import render_print_html
    from payroll.reports.runs import RUNNERS

    expected = {
        "profit-and-loss": ("Profit and Loss", "Net Income"),
        "balance-sheet": ("Balance Sheet", "$"),
        "trial-balance": ("Trial Balance", "$16,500.00"),
        "general-ledger": ("General Ledger", "Opening balance"),
        "journal": ("Journal", "Business Checking"),
        "ar-aging": ("A/R Aging", "Open invoices"),
    }
    for name, runner in RUNNERS.items():
        query = {"preset": "this_year", "asOf": "2026-12-31", "compare": "previous_period"}
        html = render_print_html(runner(session, query))
        assert html.lstrip().startswith("<!DOCTYPE html>")
        title, marker = expected[name]
        assert f"<h1>{title}</h1>" in html and marker in html, name
        assert "<script" not in html and "<link" not in html


# --- PDF ----------------------------------------------------------------------


def test_pdf_starts_with_pdf_header_and_has_content_disposition(owner_client, ledger, pdf_ready):
    response = owner_client.get(f"{PAGE}/profit-and-loss.pdf?preset=ytd")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="profit-and-loss-2026-01-01_to_2026-02-15.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"


def test_every_report_exports_pdf(owner_client, ledger, session, pdf_ready):
    for path in (
        f"{PAGE}/balance-sheet.pdf?asOf=2026-02-28&compare=previous_period",
        f"{PAGE}/trial-balance.pdf?asOf=2026-02-28",
        f"{API}/general-ledger.pdf?preset=this_year",
        f"{API}/journal.pdf?preset=this_year",
    ):
        response = owner_client.get(path)
        assert response.status_code == 200, path
        assert response.content.startswith(b"%PDF")
    formats = [
        r.after["format"]
        for r in session.exec(select(AuditLog).where(AuditLog.action == "report.export")).all()
    ]
    assert formats == ["pdf"] * 4


def test_pdf_cannot_fetch_external_or_local_resources(pdf_ready, monkeypatch):
    import weasyprint

    from payroll.reports import export

    attempts: list[str] = []
    real = export.NoFetch.__call__

    def spy(self, url, *args, **kwargs):
        attempts.append(url)
        return real(self, url, *args, **kwargs)

    monkeypatch.setattr(export.NoFetch, "__call__", spy)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.5)
    port = server.getsockname()[1]
    connections: list[bool] = []

    def accept():
        try:
            server.accept()
            connections.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=accept)
    thread.start()
    html = (
        '<html><body><img src="file:///etc/hosts">'
        f'<img src="http://127.0.0.1:{port}/x.png">'
        '<link rel="stylesheet" href="file:///etc/hosts"><p>hello</p></body></html>'
    )
    pdf = to_pdf(html)
    thread.join()
    server.close()
    assert pdf.startswith(b"%PDF")
    assert connections == []
    assert any(a.startswith("file:///etc/hosts") for a in attempts)
    assert any(f"127.0.0.1:{port}" in a for a in attempts)
    assert b"/Image" not in pdf
    with pytest.raises(Exception, match="blocked"):
        export.NoFetch()("http://example.com/x")
    assert weasyprint.__version__


def test_pages_link_to_their_exports_with_the_same_query(viewer_client, ledger):
    page = viewer_client.get(f"{PAGE}/profit-and-loss?preset=ytd&basis=accrual").text
    assert 'href="/reports/profit-and-loss.csv?preset=ytd&amp;basis=accrual">CSV</a>' in page
    assert 'href="/reports/profit-and-loss.pdf?preset=ytd&amp;basis=accrual">PDF</a>' in page
    bare = viewer_client.get(f"{PAGE}/journal").text
    assert 'href="/reports/journal.csv">CSV</a>' in bare
    bad = viewer_client.get(f"{PAGE}/journal?preset=nope").text
    assert ">CSV</a>" not in bad
