from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from payroll.auth.models import User
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import seed_default_chart
from payroll.ledger.models import Account, AccountType, JournalEntry, SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft


@pytest.fixture
def chart(session, owner_user) -> dict[str, Account]:
    """The seeded default chart, keyed by full name (``Parent:Child``)."""
    seed_default_chart(session, actor=owner_user)
    return {node.full_name: node.account for node in AccountService(session).tree()}


@dataclass
class Books:
    chart: dict[str, Account]
    owner: User
    opening: JournalEntry
    sale_jan: JournalEntry
    sale_feb: JournalEntry
    software: JournalEntry
    office: JournalEntry
    travel: JournalEntry
    travel_reversal: JournalEntry
    posting: LedgerPostingService

    def post(
        self,
        entry_date: date,
        debit: str,
        credit: str,
        amount: str,
        memo: str | None = None,
        source_type: SourceType = SourceType.MANUAL,
    ) -> JournalEntry:
        """Post a two-line entry: debit account ``debit``, credit account ``credit`` (full names)."""
        value = Decimal(amount)
        return self.posting.post(
            EntryDraft(
                entry_date=entry_date,
                memo=memo,
                source_type=source_type,
                lines=[
                    LineDraft(self.chart[debit].id, debit=value),
                    LineDraft(self.chart[credit].id, credit=value),
                ],
            ),
            actor=self.owner,
        )


@pytest.fixture
def books(session, owner_user, chart) -> Books:
    """A small, known 2026 ledger, so expected report numbers are literal.

    Entries in posting order (``entry_no`` counts per source type):

    ====  ==========  ===============  =============================  =========
    no    date        source           debit / credit                 amount
    ====  ==========  ===============  =============================  =========
    ob 1  2026-01-02  opening_balance  Business Checking / Opening    10,000.00
                                       Balance Equity
    m 1   2026-01-15  manual           Business Checking / Services    4,000.00
                                       Revenue
    m 2   2026-02-10  manual           Business Checking / Services    2,500.00
                                       Revenue
    m 3   2026-01-20  manual           Software and Subscriptions /    1,200.50
                                       Business Checking
    m 4   2026-02-15  manual           Office Expenses / Business        300.25
                                       Checking
    m 5   2026-02-20  manual           Travel / Business Checking         99.99
    m 6   2026-02-21  manual           reversal of m 5 (Business Checking / Travel)
    ====  ==========  ===============  =============================  =========

    Balances at 2026-12-31 (debit / credit column of the trial balance):

    * Business Checking (1000)            14,999.25 debit
    * Opening Balance Equity (3900)       10,000.00 credit
    * Services Revenue (4000)              6,500.00 credit
    * Office Expenses (6220)                 300.25 debit
    * Software and Subscriptions (6210)    1,200.50 debit
    * Travel (6230) nets to zero (entry m 5 and its reversal m 6)

    Totals: 16,500.00 debit = 16,500.00 credit. Business Checking after
    2026-01-31 is 12,799.50 (10,000 + 4,000 - 1,200.50). Nothing is dated
    outside calendar 2026. ``Books.post`` adds further entries in a test.
    """
    posting = LedgerPostingService(session)
    b = Books(
        chart=chart,
        owner=owner_user,
        opening=None,  # type: ignore[arg-type]
        sale_jan=None,  # type: ignore[arg-type]
        sale_feb=None,  # type: ignore[arg-type]
        software=None,  # type: ignore[arg-type]
        office=None,  # type: ignore[arg-type]
        travel=None,  # type: ignore[arg-type]
        travel_reversal=None,  # type: ignore[arg-type]
        posting=posting,
    )
    chk, obe, rev = "Business Checking", "Opening Balance Equity", "Services Revenue"
    b.opening = b.post(
        date(2026, 1, 2), chk, obe, "10000.00", "Opening equity", SourceType.OPENING_BALANCE
    )
    b.sale_jan = b.post(date(2026, 1, 15), chk, rev, "4000.00", "January sale")
    b.sale_feb = b.post(date(2026, 2, 10), chk, rev, "2500.00", "February sale")
    b.software = b.post(date(2026, 1, 20), "Software and Subscriptions", chk, "1200.50", "Software")
    b.office = b.post(date(2026, 2, 15), "Office Expenses", chk, "300.25", "Office supplies")
    b.travel = b.post(date(2026, 2, 20), "Travel", chk, "99.99", "Flight")
    b.travel_reversal = posting.reverse(b.travel.id, entry_date=date(2026, 2, 21), actor=owner_user)
    return b


@pytest.fixture
def extra_accounts(session, owner_user, books) -> dict[str, Account]:
    """Accounts the default chart lacks, added to ``books.chart`` (and returned).

    * Materials (5000, cogs)
    * Interest Income (7000, other_income)
    * Penalties (8000, other_expense)
    * Equipment (1500, asset) and Accumulated Depreciation (1510, asset, contra)
    """
    service = AccountService(session)
    rows = [
        ("Materials", AccountType.COGS, "5000"),
        ("Interest Income", AccountType.OTHER_INCOME, "7000"),
        ("Penalties", AccountType.OTHER_EXPENSE, "8000"),
        ("Equipment", AccountType.ASSET, "1500"),
        ("Accumulated Depreciation", AccountType.ASSET, "1510"),
    ]
    added = {
        name: service.create(actor=owner_user, name=name, type=type_, number=number)
        for name, type_, number in rows
    }
    books.chart.update(added)
    return added


@pytest.fixture
def prior_year(books) -> Books:
    """``books`` plus two 2025 entries and no year-end close.

    * 2025-06-01 Business Checking / Services Revenue    3,000.00
    * 2025-07-01 Office Expenses / Business Checking       500.00

    2025 net income is 2,500.00; Business Checking at 2026-06-30 is 17,499.25.
    """
    books.post(date(2025, 6, 1), "Business Checking", "Services Revenue", "3000.00", "2025 sale")
    books.post(date(2025, 7, 1), "Office Expenses", "Business Checking", "500.00", "2025 office")
    return books
