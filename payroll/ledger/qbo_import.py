"""Import a chart of accounts exported from QuickBooks Online (CSV).

Two steps, both pure functions of the file and the current chart:

* :func:`parse_qbo_csv` reads the export (the Chart of Accounts export or the Account
  List report) into :class:`RawRow` values.
* :func:`plan_import` decides, per row, whether the account is created, already exists
  (and is skipped) or cannot be imported. The plan writes nothing; the confirm step
  re-plans from the same file and hands :attr:`ImportPlan.new_accounts` to
  :meth:`AccountService.create_batch`, so existing accounts are never edited.

QBO writes sub-accounts as ``Parent:Child`` in the name column; balances and
descriptions are ignored (opening balances arrive with the general ledger import).
"""

import csv
import io
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal

from sqlmodel import Session

from payroll.ledger.accounts import AccountService, NewAccount
from payroll.ledger.exceptions import InvalidAccountError
from payroll.ledger.models import Account, AccountType

MAX_BYTES = 512 * 1024
MAX_ROWS = 2000
MAX_HEADER_SCAN = 10  # the Account List report puts title rows above the header
MAX_NAME = 200
MAX_NUMBER = 20
MAX_DETAIL = 100

# QBO "Account type" (lower-cased) -> this app's type.
QBO_TYPES: dict[str, AccountType] = {
    "bank": AccountType.ASSET,
    "accounts receivable (a/r)": AccountType.ASSET,
    "other current assets": AccountType.ASSET,
    "other current asset": AccountType.ASSET,
    "fixed assets": AccountType.ASSET,
    "fixed asset": AccountType.ASSET,
    "other assets": AccountType.ASSET,
    "other asset": AccountType.ASSET,
    "accounts payable (a/p)": AccountType.LIABILITY,
    "credit card": AccountType.LIABILITY,
    "other current liabilities": AccountType.LIABILITY,
    "other current liability": AccountType.LIABILITY,
    "long term liabilities": AccountType.LIABILITY,
    "long term liability": AccountType.LIABILITY,
    "equity": AccountType.EQUITY,
    "income": AccountType.INCOME,
    "cost of goods sold": AccountType.COGS,
    "expenses": AccountType.EXPENSE,
    "expense": AccountType.EXPENSE,
    "other income": AccountType.OTHER_INCOME,
    "other expense": AccountType.OTHER_EXPENSE,
}

_HEADERS: dict[str, tuple[str, ...]] = {
    "number": ("account number", "account #", "account no", "account no.", "number", "acct #"),
    "name": ("account name", "account", "full name", "name"),
    "type": ("account type", "type"),
    "detail": ("detail type", "detail_type"),
}
_PLAIN_NUMBER = re.compile(r"^[A-Za-z0-9._-]+$")

Action = Literal["create", "exists", "error"]


@dataclass(frozen=True)
class RawRow:
    line: int  # 1-based line in the file, for messages
    number: str
    full_name: str
    qbo_type: str
    detail_type: str


@dataclass
class PlannedRow:
    row: RawRow
    action: Action
    reason: str = ""
    account_type: AccountType | None = None
    warning: str = ""
    selected: bool = True  # only meaningful for rows that would be created

    @property
    def key(self) -> str:
        """Lower-cased ``parent:child`` path; the preview uses it to tick children with parents."""
        return ":".join(p.lower() for p in _split(self.row.full_name))


@dataclass
class ImportPlan:
    rows: list[PlannedRow] = field(default_factory=list)
    new_accounts: list[NewAccount] = field(default_factory=list)

    def count(self, action: Action) -> int:
        return sum(1 for r in self.rows if r.action == action)

    @property
    def has_errors(self) -> bool:
        return self.count("error") > 0

    @property
    def selected_count(self) -> int:
        return len(self.new_accounts)


def decode_upload(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise InvalidAccountError(message="The file is too large (limit 512 KB)")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidAccountError(
            message="The file is not UTF-8 text; export or save it as CSV (UTF-8)"
        ) from exc


def _column_map(header: list[str]) -> dict[str, int] | None:
    normalised = [re.sub(r"\s+", " ", cell.strip().lower()) for cell in header]
    found: dict[str, int] = {}
    for key, aliases in _HEADERS.items():
        for alias in aliases:
            if alias in normalised:
                found[key] = normalised.index(alias)
                break
    return found if "name" in found and "type" in found else None


def parse_qbo_csv(text: str) -> list[RawRow]:
    if len(text.encode()) > MAX_BYTES:
        raise InvalidAccountError(message="The file is too large (limit 512 KB)")
    records = list(csv.reader(io.StringIO(text)))
    columns: dict[str, int] | None = None
    header_at = 0
    for index, record in enumerate(records[:MAX_HEADER_SCAN]):
        columns = _column_map(record)
        if columns is not None:
            header_at = index
            break
    if columns is None:
        raise InvalidAccountError(
            message="Not a QBO chart of accounts: no 'Account name' and 'Account type' columns"
        )

    def cell(record: list[str], key: str) -> str:
        position = columns.get(key) if columns else None
        if position is None or position >= len(record):
            return ""
        return record[position].strip()

    rows: list[RawRow] = []
    for offset, record in enumerate(records[header_at + 1 :], start=header_at + 2):
        name = cell(record, "name")
        if not name and not cell(record, "type"):
            continue  # blank line or a total/footer row
        rows.append(
            RawRow(
                line=offset,
                number=cell(record, "number"),
                full_name=name,
                qbo_type=cell(record, "type"),
                detail_type=cell(record, "detail"),
            )
        )
        if len(rows) > MAX_ROWS:
            raise InvalidAccountError(message=f"Too many accounts (limit {MAX_ROWS})")
    if not rows:
        raise InvalidAccountError(message="The file has no accounts")
    return rows


def _split(full_name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in full_name.split(":"))


def plan_import(
    session: Session, rows: list[RawRow], selected: Collection[int] | None = None
) -> ImportPlan:
    """Plan the import of ``rows``.

    ``selected`` holds the file lines the owner ticked; ``None`` selects every row that
    would be created. Only rows that would be created can be selected, and a selected
    sub-account needs its parent in the chart or selected too (otherwise
    :class:`InvalidAccountError`).
    """
    existing = AccountService(session).tree(include_inactive=True)
    by_path = {
        tuple(p.strip().lower() for p in n.full_name.split(":")): n.account for n in existing
    }
    by_number = {n.account.number for n in existing if n.account.number}

    # Shallowest first (file order within a level): a parent is settled before its children.
    planned = sorted((_assess(row) for row in rows), key=lambda it: (_depth(it), it.row.line))
    in_file: dict[tuple[str, ...], PlannedRow] = {}
    seen_numbers: dict[str, int] = {}

    broken: set[tuple[str, ...]] = set()  # paths of rows that cannot be imported
    for item in planned:
        row, parts = item.row, _split(item.row.full_name)
        path = tuple(p.lower() for p in parts)
        if item.action == "error":
            broken.add(path)
            continue
        if path in in_file:
            _fail(item, f"Duplicate of line {in_file[path].row.line}")
            continue
        if row.number and row.number in seen_numbers:
            _fail(item, f"Account number {row.number} is also on line {seen_numbers[row.number]}")
            broken.add(path)
            continue
        in_file[path] = item
        if row.number:
            seen_numbers[row.number] = row.line
        if row.number and row.number in by_number:
            item.action, item.reason = "exists", f"Number {row.number} is already in the chart"
        elif path in by_path:
            item.action, item.reason = "exists", "Already in the chart"
        elif len(parts) > 1:
            _check_parent(item, path[:-1], parts, by_path, in_file, broken)
            if item.action == "error":
                broken.add(path)
        if item.action == "create" and row.number and not _PLAIN_NUMBER.match(row.number):
            item.warning = "Unusual account number, kept as exported; edit it afterwards"

    return _finish(planned, by_path, None if selected is None else set(selected))


def _depth(item: PlannedRow) -> int:
    return len(_split(item.row.full_name))


def _check_parent(
    item: PlannedRow,
    parent_path: tuple[str, ...],
    parts: tuple[str, ...],
    by_path: dict[tuple[str, ...], Account],
    in_file: dict[tuple[str, ...], PlannedRow],
    broken: set[tuple[str, ...]],
) -> None:
    parent_row = in_file.get(parent_path)
    parent_account = by_path.get(parent_path)
    if parent_path in broken:
        _fail(item, f"Parent '{':'.join(parts[:-1])}' cannot be imported")
    elif parent_account is None and (parent_row is None or parent_row.action != "create"):
        _fail(item, f"Parent '{':'.join(parts[:-1])}' is not in the file or the chart")
    else:
        parent_type = parent_account.type if parent_account is not None else parent_row.account_type  # type: ignore[union-attr]
        if parent_type != item.account_type:
            _fail(item, "A sub-account must have the same type as its parent")


def _assess(row: RawRow) -> PlannedRow:
    item = PlannedRow(row=row, action="create")
    parts = _split(row.full_name)
    if not row.full_name or any(not part for part in parts):
        return _fail(item, "The account name is empty or has an empty sub-account level")
    if len(parts[-1]) > MAX_NAME:
        return _fail(item, f"The account name is longer than {MAX_NAME} characters")
    if len(row.number) > MAX_NUMBER:
        return _fail(item, f"The account number is longer than {MAX_NUMBER} characters")
    if len(row.detail_type) > MAX_DETAIL:
        return _fail(item, f"The detail type is longer than {MAX_DETAIL} characters")
    mapped = QBO_TYPES.get(re.sub(r"\s+", " ", row.qbo_type.lower()))
    if mapped is None:
        return _fail(item, f"Unknown account type '{row.qbo_type}'")
    item.account_type = mapped
    return item


def _fail(item: PlannedRow, reason: str) -> PlannedRow:
    item.action, item.reason = "error", reason
    return item


def _finish(
    planned: list[PlannedRow], by_path: dict[tuple[str, ...], Account], selected: set[int] | None
) -> ImportPlan:
    creatable = {it.row.line for it in planned if it.action == "create"}
    if selected is not None and (stray := sorted(selected - creatable)):
        raise InvalidAccountError(message=f"Line {stray[0]} cannot be imported")
    plan = ImportPlan(rows=sorted(planned, key=lambda it: it.row.line))
    index_of: dict[tuple[str, ...], int] = {}
    for item in planned:  # shallowest first, so a created parent already has an index
        if item.action != "create" or item.account_type is None:
            continue
        item.selected = selected is None or item.row.line in selected
        if not item.selected:
            continue
        parts = _split(item.row.full_name)
        path = tuple(p.lower() for p in parts)
        parent_id = parent_index = None
        if len(parts) > 1:
            if path[:-1] in index_of:
                parent_index = index_of[path[:-1]]
            elif path[:-1] in by_path:
                parent_id = by_path[path[:-1]].id
            else:
                raise InvalidAccountError(
                    message=(
                        f"'{item.row.full_name}' needs its parent "
                        f"'{':'.join(parts[:-1])}', which is not selected"
                    )
                )
        index_of[path] = len(plan.new_accounts)
        plan.new_accounts.append(
            NewAccount(
                name=parts[-1],
                type=item.account_type,
                number=item.row.number or None,
                detail_type=item.row.detail_type or None,
                parent_id=parent_id,
                parent_index=parent_index,
            )
        )
    return plan
