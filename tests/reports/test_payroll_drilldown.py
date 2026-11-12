"""Payroll-run and tax-payment entries link to their documents wherever entries are shown.

Assertions are on the anchor with class ``source-link`` and its exact href: the navbar links
to ``/`` on every page, so page text alone proves nothing.
"""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

import html
import re
from datetime import date
from decimal import Decimal

import pytest

from payroll.frontend.source_links import document_label, source_href
from payroll.ledger.models import SourceType, SystemRole
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.payroll_run.posting import PayrollPostingService
from payroll.payroll_run.tax_payments import TaxPaymentService
from tests.ledger.conftest import chart  # noqa: F401
from tests.payroll_run.conftest import bank, payroll_roles  # noqa: F401
from tests.payroll_run.test_posting import approve, save

D = Decimal
WINDOW = "start=2026-01-01&end=2026-12-31"
ANCHOR = re.compile(r'<a class="source-link[^"]*" href="([^"]*)">([^<]*)</a>')


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 4, 15))
    monkeypatch.setattr("payroll.frontend.ledger_routers.company_today", lambda: date(2026, 4, 15))


def anchors(text: str) -> list[tuple[str, str]]:
    return [(html.unescape(h), label) for h, label in ANCHOR.findall(text)]


@pytest.fixture
def docs(session, owner_user, chart, payroll_roles, year_settings, bank):
    run = save(session, owner_user, period="2026-01")
    approve(PayrollPostingService(session), owner_user, run, bank)
    payment = TaxPaymentService(session).create(
        actor=owner_user,
        payment_date=date(2026, 2, 5),
        agency="irs_941",
        bank_account_id=bank.id,
        amount=D("50.00"),
    )
    session.commit()
    session.refresh(run)
    session.refresh(payment)
    return {
        "run": run,
        "payment": payment,
        "run_href": f"/?run={run.id}",
        "pay_href": f"/payroll/tax-payments/{payment.id}",
        "federal": payroll_roles[SystemRole.PAYROLL_FEDERAL_PAYABLE],
        "bank": bank,
    }


@pytest.mark.parametrize(
    ("source_type", "source_id", "expected"),
    [
        (SourceType.PAYROLL_RUN, "12", "/?run=12"),
        ("payroll_run", "7", "/?run=7"),
        (SourceType.TAX_PAYMENT, "3", "/payroll/tax-payments/3"),
        (SourceType.PAYROLL_RUN, "legacy", None),
        (SourceType.PAYROLL_RUN, "", None),
        (SourceType.PAYROLL_RUN, None, None),
        (SourceType.PAYROLL_RUN, "0", None),
        (SourceType.PAYROLL_RUN, "-5", None),
        (SourceType.TAX_PAYMENT, "1.5", None),
        (SourceType.TAX_PAYMENT, "99999999999999999999", None),
        (SourceType.TAX_PAYMENT, "١٢", None),
    ],
)
def test_source_href(source_type, source_id, expected):
    assert source_href(source_type, source_id) == expected


def test_labels():
    assert document_label(SourceType.PAYROLL_RUN) == "Payroll run"
    assert document_label(SourceType.TAX_PAYMENT) == "Tax payment"


def test_general_ledger_links_both(owner_client, docs):
    found = anchors(owner_client.get(f"/reports/general-ledger?preset=custom&{WINDOW}").text)
    assert (docs["run_href"], "Payroll run") in found
    assert (docs["pay_href"], "Tax payment") in found


def test_journal_report_links_both(owner_client, docs):
    found = anchors(owner_client.get(f"/reports/journal?preset=custom&{WINDOW}").text)
    assert found.count((docs["run_href"], "Payroll run")) == 1
    assert found.count((docs["pay_href"], "Tax payment")) == 1


@pytest.mark.parametrize("basis", ["", "&basis=cash"])
def test_registers_link_both(owner_client, docs, basis):
    federal = owner_client.get(f"/accounts/{docs['federal'].id}/register?{WINDOW}{basis}")
    assert federal.status_code == 200
    found = anchors(federal.text)
    assert (docs["run_href"], "Payroll run") in found
    assert (docs["pay_href"], "Tax payment") in found
    bank = anchors(owner_client.get(f"/accounts/{docs['bank'].id}/register?{WINDOW}{basis}").text)
    assert (docs["pay_href"], "Tax payment") in bank


def test_journal_list_and_entry_view_link_both(owner_client, docs):
    found = anchors(owner_client.get("/journal?start=2026-01-01&end=2026-12-31").text)
    assert (docs["run_href"], "Payroll run") in found
    assert (docs["pay_href"], "Tax payment") in found
    for key, href, label in (
        ("run", docs["run_href"], "Payroll run"),
        ("payment", docs["pay_href"], "Tax payment"),
    ):
        view = owner_client.get(f"/journal/{docs[key].entry_id}")
        assert view.status_code == 200
        assert anchors(view.text) == [(href, label)], key


def test_the_linked_pages_open(owner_client, docs):
    assert owner_client.get(docs["pay_href"]).status_code == 200
    assert owner_client.get(docs["run_href"]).status_code == 200


def test_legacy_source_ids_render_without_a_link_and_without_a_500(
    owner_client, session, owner_user, chart
):
    for source_type, source_id in (
        (SourceType.PAYROLL_RUN, "legacy-abc"),
        (SourceType.TAX_PAYMENT, "99999999999999999999"),
        (SourceType.PAYROLL_RUN, None),
        (SourceType.TAX_PAYMENT, "0"),
    ):
        LedgerPostingService(session).post(
            EntryDraft(
                date(2026, 2, 20),
                [
                    LineDraft(chart["Office Expenses"].id, debit=D("1.00")),
                    LineDraft(chart["Business Checking"].id, credit=D("1.00")),
                ],
                source_type=source_type,
                source_id=source_id,
            ),
            actor=owner_user,
        )
    session.commit()
    office = chart["Office Expenses"].id
    for path in (
        f"/reports/general-ledger?preset=custom&{WINDOW}",
        f"/reports/journal?preset=custom&{WINDOW}",
        f"/accounts/{office}/register?{WINDOW}",
        f"/accounts/{office}/register?{WINDOW}&basis=cash",
        "/journal?start=2026-01-01&end=2026-12-31",
    ):
        response = owner_client.get(path)
        assert response.status_code == 200, path
        assert anchors(response.text) == [], path
