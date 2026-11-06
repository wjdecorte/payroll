"""Expense, transfer and reimbursement entries link to their documents, and cash basis counts them.

Data is built through the real expense, transfer and reimbursement services.
"""

# ruff: noqa: F811  (the ``chart`` fixture is re-exported and used as an argument)

import html
import re
from datetime import date
from decimal import Decimal

import pytest

from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.reimbursements import ReimbursementService
from payroll.expenses.transfers import TransferService
from payroll.frontend.source_links import source_href
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.reports.basis import Basis
from payroll.reports.periods import Period
from payroll.reports.statements import ReportService
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
WINDOW = "start=2026-01-01&end=2026-12-31"
YEAR = Period(date(2026, 1, 1), date(2026, 12, 31), "year")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("payroll.reports.runs.company_today", lambda: date(2026, 4, 15))
    monkeypatch.setattr("payroll.frontend.ledger_routers.company_today", lambda: date(2026, 4, 15))


def hrefs(text: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]*)"', text)]


@pytest.fixture
def docs(session, owner_user, chart):
    """An account-paid expense, an owner-paid one that is reimbursed, and a transfer."""
    savings = AccountService(session).create(
        actor=owner_user, name="Savings Test", type=AccountType.ASSET
    )
    office = chart["Office Expenses"]
    paid = ExpenseService(session).create(
        actor=owner_user,
        expense_date=date(2026, 2, 1),
        lines=[ExpenseLineInput(office.id, D("40.00"))],
        source="account",
        paid_from_account_id=chart["Business Checking"].id,
        payee_text="Staples",
    )
    owned = ExpenseService(session).create(
        actor=owner_user,
        expense_date=date(2026, 2, 5),
        lines=[ExpenseLineInput(office.id, D("25.00"))],
        source="owner",
        paid_from_account_id=None,
        payee_text="Cash buy",
    )
    reimbursement = ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=date(2026, 2, 10),
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[owned.id],
    )
    transfer = TransferService(session).create(
        actor=owner_user,
        transfer_date=date(2026, 2, 15),
        from_account_id=chart["Business Checking"].id,
        to_account_id=savings.id,
        amount=D("60.00"),
    )
    session.commit()
    return {
        "expense": paid,
        "owned": owned,
        "reimbursement": reimbursement,
        "transfer": transfer,
        "savings": savings,
        "office": office,
        "chart": chart,
    }


# --- the link builder --------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "source_id", "expected"),
    [
        (SourceType.EXPENSE, "12", "/expenses/12"),
        ("transfer", "7", "/transfers/7"),
        (SourceType.REIMBURSEMENT, "3", "/owner"),
        (SourceType.REIMBURSEMENT, "legacy", "/owner"),
        (SourceType.EXPENSE, "legacy-abc", None),
        (SourceType.EXPENSE, "", None),
        (SourceType.EXPENSE, None, None),
        (SourceType.EXPENSE, "-5", None),
        (SourceType.EXPENSE, "1.5", None),
        (SourceType.TRANSFER, "99999999999999999999", None),
        (SourceType.EXPENSE, "١٢", None),  # non-ASCII digits
        (SourceType.MANUAL, "5", None),
        (SourceType.INVOICE, "5", None),
    ],
)
def test_source_href(source_type, source_id, expected):
    assert source_href(source_type, source_id) == expected


# --- the pages ---------------------------------------------------------------------


def test_general_ledger_links_all_three_source_types(owner_client, docs):
    links = hrefs(owner_client.get(f"/reports/general-ledger?preset=custom&{WINDOW}").text)
    assert f"/expenses/{docs['expense'].id}" in links
    assert f"/expenses/{docs['owned'].id}" in links
    assert f"/transfers/{docs['transfer'].id}" in links
    assert "/owner" in links


def test_journal_report_links_all_three_source_types(owner_client, docs):
    links = hrefs(owner_client.get(f"/reports/journal?preset=custom&{WINDOW}").text)
    assert f"/expenses/{docs['expense'].id}" in links
    assert f"/transfers/{docs['transfer'].id}" in links
    assert "/owner" in links


def test_account_registers_link_their_documents(owner_client, docs):
    chart = docs["chart"]
    office = hrefs(owner_client.get(f"/accounts/{docs['office'].id}/register?{WINDOW}").text)
    assert f"/expenses/{docs['expense'].id}" in office
    savings = hrefs(owner_client.get(f"/accounts/{docs['savings'].id}/register?{WINDOW}").text)
    assert f"/transfers/{docs['transfer'].id}" in savings
    owner = hrefs(owner_client.get(f"/accounts/{chart['Due to Owner'].id}/register?{WINDOW}").text)
    assert "/owner" in owner
    assert f"/expenses/{docs['owned'].id}" in owner


def test_cash_basis_register_detail_rows_link_the_documents(owner_client, docs):
    page = owner_client.get(f"/accounts/{docs['office'].id}/register?{WINDOW}&basis=cash")
    assert page.status_code == 200 and "Cash basis view" in page.text
    assert f"/expenses/{docs['expense'].id}" in hrefs(page.text)


def test_journal_list_and_entry_view_link_the_documents(owner_client, session, docs):
    listing = hrefs(owner_client.get("/journal?start=2026-01-01&end=2026-12-31").text)
    assert f"/expenses/{docs['expense'].id}" in listing
    assert f"/transfers/{docs['transfer'].id}" in listing
    assert "/owner" in listing
    for key, expected in (
        ("expense", f"/expenses/{docs['expense'].id}"),
        ("transfer", f"/transfers/{docs['transfer'].id}"),
        ("reimbursement", "/owner"),
    ):
        view = owner_client.get(f"/journal/{docs[key].entry_id}")
        assert view.status_code == 200
        assert expected in hrefs(view.text), key


def test_a_voided_documents_reversal_entry_links_the_same_way(
    owner_client, session, owner_user, docs
):
    TransferService(session).void(docs["transfer"].id, actor=owner_user, void_date=date(2026, 3, 1))
    session.commit()
    reversal = session.get(JournalEntry, docs["transfer"].void_entry_id)
    assert reversal.source_id == str(docs["transfer"].id)
    links = hrefs(owner_client.get(f"/reports/journal?preset=custom&{WINDOW}").text)
    assert links.count(f"/transfers/{docs['transfer'].id}") == 2
    view = owner_client.get(f"/journal/{reversal.id}")
    assert f"/transfers/{docs['transfer'].id}" in hrefs(view.text)


def test_legacy_source_ids_render_without_a_link_and_without_a_500(
    owner_client, session, owner_user, chart
):
    for source_type, source_id in (
        (SourceType.EXPENSE, "legacy-abc"),
        (SourceType.TRANSFER, "99999999999999999999"),
        (SourceType.EXPENSE, None),
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
    pages = (
        f"/reports/general-ledger?preset=custom&{WINDOW}",
        f"/reports/journal?preset=custom&{WINDOW}",
        f"/accounts/{chart['Office Expenses'].id}/register?{WINDOW}",
        f"/accounts/{chart['Office Expenses'].id}/register?{WINDOW}&basis=cash",
        "/journal?start=2026-01-01&end=2026-12-31",
    )
    for path in pages:
        response = owner_client.get(path)
        assert response.status_code == 200, path
        links = hrefs(response.text)
        assert not [h for h in links if h.startswith(("/expenses/", "/transfers/"))], path


# --- cash basis --------------------------------------------------------------------


def _amounts(session, basis):
    statement = ReportService(session).profit_and_loss(YEAR, basis=basis)
    return {r.label: r.amounts for r in statement.rows}


def test_cash_basis_includes_expense_and_reimbursement_entries_unchanged(session, docs):
    cash = _amounts(session, Basis.CASH)
    accrual = _amounts(session, Basis.ACCRUAL)
    assert cash["Office Expenses"] == [D("65.00")]  # 40.00 paid + 25.00 owner-paid, once each
    assert cash["Net Income"] == [D("-65.00")]
    assert cash == accrual


def test_a_transfer_between_balance_sheet_accounts_does_not_change_the_profit_and_loss(
    session, owner_user, docs
):
    before = _amounts(session, Basis.CASH)
    TransferService(session).create(
        actor=owner_user,
        transfer_date=date(2026, 3, 1),
        from_account_id=docs["chart"]["Business Checking"].id,
        to_account_id=docs["savings"].id,
        amount=D("10.00"),
    )
    session.commit()
    assert _amounts(session, Basis.CASH) == before
    assert _amounts(session, Basis.ACCRUAL) == before


def test_the_reimbursement_alone_does_not_add_expense(session, docs):
    cash = _amounts(session, Basis.CASH)
    # The reimbursement moves Due to Owner to checking: no income or expense account moves.
    assert cash["Office Expenses"] == [D("65.00")]
    posted = session.get(JournalEntry, docs["reimbursement"].entry_id)
    assert posted.source_type == SourceType.REIMBURSEMENT
