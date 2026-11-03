"""The default chart of accounts. Names match the existing ``ACCT_*`` payroll defaults."""

from dataclasses import dataclass

from sqlmodel import Session

from payroll.auth.models import User
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import AccountType, SystemRole


@dataclass(frozen=True)
class ChartRow:
    name: str
    type: AccountType
    number: str | None = None
    parent: str | None = None  # parent account's name (parents are top-level)
    role: SystemRole | None = None


A, L, E = AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY
INC, X = AccountType.INCOME, AccountType.EXPENSE

DEFAULT_CHART: list[ChartRow] = [
    ChartRow("Business Checking", A, "1000"),
    ChartRow("Accounts Receivable", A, "1100", role=SystemRole.ACCOUNTS_RECEIVABLE),
    ChartRow("Undeposited Funds", A, "1150", role=SystemRole.UNDEPOSITED_FUNDS),
    ChartRow("Due to Owner", L, "2000", role=SystemRole.DUE_TO_OWNER),
    ChartRow("Payroll Liabilities", L, "2100"),
    ChartRow("Federal Taxes (941/943/944)", L, parent="Payroll Liabilities"),
    ChartRow("GA Income Tax", L, parent="Payroll Liabilities"),
    ChartRow("S-Corp Health Insurance", L, parent="Payroll Liabilities"),
    ChartRow("Co. HSA", L, parent="Payroll Liabilities"),
    ChartRow("FUTA Payable", L, parent="Payroll Liabilities"),
    ChartRow("GA SUTA Payable", L, parent="Payroll Liabilities"),
    ChartRow("Common Stock", E, "3000"),
    ChartRow("Retained Earnings", E, "3100", role=SystemRole.RETAINED_EARNINGS),
    ChartRow("Shareholder Distributions", E, "3200"),
    ChartRow("Opening Balance Equity", E, "3900", role=SystemRole.OPENING_BALANCE_EQUITY),
    ChartRow("Services Revenue", INC, "4000"),
    ChartRow("Payroll Expenses", X, "6000"),
    ChartRow("Wages", X, parent="Payroll Expenses"),
    ChartRow("Taxes", X, parent="Payroll Expenses"),
    ChartRow("Health Insurance Expense", X, "6100"),
    ChartRow("HSA Contribution Expense", X, "6110"),
    ChartRow("Professional Fees", X, "6200"),
    ChartRow("Software and Subscriptions", X, "6210"),
    ChartRow("Office Expenses", X, "6220"),
    ChartRow("Travel", X, "6230"),
    ChartRow("Meals", X, "6240"),
    ChartRow("Bank Charges", X, "6250"),
    ChartRow("Other Expenses", X, "6900"),
]


def seed_default_chart(session: Session, *, actor: User) -> int:
    """Create the default accounts that do not exist yet and bind the system roles.

    Idempotent: matching is by full name (so inactive accounts count as present),
    a role that is already bound is left where it is. Returns how many were created.
    """
    svc = AccountService(session)
    existing = {n.full_name: n.account for n in svc.tree(include_inactive=True)}
    created = 0
    for row in DEFAULT_CHART:
        full = f"{row.parent}:{row.name}" if row.parent else row.name
        if full in existing:
            continue
        parent = existing[row.parent] if row.parent else None
        existing[full] = svc.create(
            actor=actor,
            name=row.name,
            type=row.type,
            number=row.number,
            parent_id=parent.id if parent else None,
        )
        created += 1
    for row in DEFAULT_CHART:
        if row.role is not None and svc.account_by_role(row.role) is None:
            svc.bind_role(row.role, existing[row.name].id, actor=actor)
    return created
