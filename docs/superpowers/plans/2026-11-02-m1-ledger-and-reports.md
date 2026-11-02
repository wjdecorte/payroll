# DCI Books M1 — Ledger and Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the double-entry ledger, chart of accounts, manual journals, period lock, and the standard reports (Trial Balance, General Ledger, Journal, Account Register, Profit & Loss, Balance Sheet) with presets, custom periods, comparison, drill-down and CSV/PDF export, so the owner can keep and read a real set of books in the app.

**Architecture:** New `payroll/ledger/` (models, services, routers, default chart, CLI) and `payroll/reports/` (period resolver, queries, statements, export) modules, per DESIGN §2.2: `ledger` depends only on `common`/`auth`/`audit`; `reports` reads the ledger tables only; nothing writes ledger tables except `ledger.services.LedgerPostingService`. Integrity is enforced twice: in the service and by Postgres triggers. Pages are server-rendered Jinja2 + HTMX inside the existing frontend, behind the M0 auth.

**Tech Stack:** Python 3.14, FastAPI, SQLModel, Alembic, Postgres 17, Jinja2 + HTMX, `weasyprint` (PDF; new dependency, DESIGN §12 item 4), `hypothesis` (dev, property tests), pytest, ruff.

**Spec:** [docs/SPEC.md](../../SPEC.md) (§3.1 F1–F4, §3.2 S1–S2, §4.1, §4.2, §4.7, §8 M1) and [docs/DESIGN.md](../../DESIGN.md) (§4.1, §7). Executors read both. Base: branch `feat/m1-ledger` created from `feat/m0-foundation` (PR 6 is not merged yet). Version becomes `2.1.0`.

## Global Constraints

- Everything in the M0 plan's Global Constraints still holds (Decimal money, `NUMERIC(14,2)`, Postgres only, `ruff` clean, role checks in routes AND services, CSRF, audit, coverage floor 96, commit after each task, do not touch the uncommitted `AGENTS.md` change).
- Floats never appear in ledger code. A money value with more than two decimal places is **rejected**, never silently rounded. Allowed magnitude per line: `0.01` to `999999999999.99`.
- Posted ledger rows are immutable. Corrections are reversal entries. Only one header change is ever allowed: `status` `posted` → `void`.
- Balances are computed from `journal_line` rows, never stored.
- `entry_no` is sequential and gap-free **per `source_type`**, assigned inside the posting transaction (a rolled-back post consumes no number).
- Mutations (accounts, entries, lock date, system roles) are owner-only in routes and in services; all three roles may read and run reports. The accountant's "optional post" in SEC7 is deferred.
- Dates are company-local. New setting `company_timezone` (default `America/New_York`) decides "today" for presets; the fiscal year is the calendar year.
- Every account, entry, lock-date and system-role mutation, and every report export, writes an audit row in the same transaction (export rows are committed by the export route).
- JSON API under `/payroll/api/v1`; pages at `/accounts`, `/journal`, `/reports/...`, `/settings/ledger`. JSON money values are strings.
- CSP stays `self`-only: no inline scripts or handlers; page scripts live in `payroll/frontend/static/js/`.
- Vendored libraries only; new Python dependencies via `uv add` and listed in the Task that adds them.

## Review Focus

Failure modes the spec implies but no feature task would test on its own, most likely first:

1. **Bad amounts:** an amount like `10.005`, `NaN`, `-5`, a line with both debit and credit, or `1e20` is rejected with a 422 (never a 500, never rounded). → Task 3
2. **Concurrent posting and numbering:** parallel posts get unique contiguous numbers; a failed post burns none; a post racing a lock-date change cannot slip into the locked period. → Tasks 3, 4
3. **Reversal edge cases:** reversing twice, voiding twice, reversing into a locked period, reversing a reversal. → Task 3
4. **Report date edges:** `last_quarter`/`last_month` in January, Feb 29, a ledger that is empty, an `as of` before any activity, `from` after `to`, a company-local "today" that differs from UTC. → Tasks 5, 6, 7
5. **Unclosed prior years and contra balances:** a Balance Sheet in year 2 still balances when year 1 was never closed (earnings flow into equity); a negative natural balance (contra account) shows with its sign, not as an absolute value. → Task 7
6. **Export safety:** a memo like `=HYPERLINK("http://x","y")` cannot execute in a spreadsheet; WeasyPrint cannot fetch URLs or local files; an XSS payload in a memo or account name is escaped in pages. → Tasks 9, 10, 11
7. **Authorization:** viewer/accountant can read every report and cannot post, edit accounts, or move the lock date; the route-enumeration test knows the new routes. → Task 12

## File Structure

```
payroll/
├── ledger/
│   ├── models.py        NEW  Account, JournalEntry, JournalLine, EntryCounter, PeriodLock + enums
│   ├── exceptions.py    NEW  ledger errors (AppBaseError subclasses)
│   ├── schemas.py       NEW  pydantic request/response models (Decimal money -> strings)
│   ├── accounts.py      NEW  AccountService (chart of accounts, system roles)
│   ├── default_chart.py NEW  DEFAULT_CHART data + seed_default_chart()
│   ├── services.py      NEW  LedgerPostingService (post/reverse/void), JournalService (manual entries)
│   ├── period_lock.py   NEW  PeriodLockService
│   ├── routers.py       NEW  JSON API
│   └── cli.py           NEW  seed-chart, seed-demo
├── reports/
│   ├── periods.py       NEW  Period, resolve_period, previous_period, previous_year
│   ├── basis.py         NEW  Basis enum + recognition hook (identity until M2)
│   ├── queries.py       NEW  balance/line SQL aggregates
│   ├── statements.py    NEW  ReportService: trial balance, GL, journal, register, P&L, balance sheet
│   ├── schemas.py       NEW  report models
│   ├── export.py        NEW  to_csv(), to_pdf()
│   └── routers.py       NEW  JSON API + CSV/PDF endpoints
├── frontend/
│   ├── ledger_routers.py   NEW  pages: accounts, journal, settings/ledger
│   ├── report_routers.py   NEW  pages: reports
│   ├── templates/{accounts,journal,reports,settings}/...  NEW
│   ├── static/js/journal.js NEW  live debit/credit totals, add/remove line
│   └── templates/base.html  MODIFY  top navigation
├── migrations/versions/a1c5e8d3b7f2_ledger_core.py  NEW
tests/{ledger,reports}/   NEW
```

---

### Task 1: Ledger schema and database integrity triggers

**Files:**
- Create: `payroll/ledger/__init__.py`, `payroll/ledger/models.py`, `payroll/migrations/versions/a1c5e8d3b7f2_ledger_core.py`
- Modify: `payroll/migrations/env.py` (import the new models so autogenerate and `SQLModel.metadata` see them, following how `audit.models` is imported)
- Test: `tests/ledger/__init__.py`, `tests/ledger/test_schema.py`, `tests/migrations/test_ledger_migration.py`

**Interfaces:**
- Produces (`payroll/ledger/models.py`, all `table=True` on `AppBaseModel` unless noted):
  - `class AccountType(StrEnum)`: `asset, liability, equity, income, cogs, expense, other_income, other_expense`; `NORMAL_DEBIT: frozenset[AccountType]` = `{asset, cogs, expense, other_expense}`.
  - `class SystemRole(StrEnum)`: `accounts_receivable, undeposited_funds, due_to_owner, retained_earnings, opening_balance_equity`.
  - `class SourceType(StrEnum)`: `manual, invoice, payment, deposit, expense, reimbursement, payroll_run, tax_payment, opening_balance, year_end_close`.
  - `Account`: `number: str | None`, `name: str`, `parent_id: int | None` (FK `account.id`), `type: AccountType`, `detail_type: str | None`, `is_active: bool = True`, `system_role: SystemRole | None`.
  - `JournalEntry`: `entry_no: int`, `source_type: SourceType`, `source_id: str | None`, `entry_date: date`, `memo: str | None`, `status: str` (`posted|void`), `reverses_entry_id: int | None` (FK self), `posted_at: datetime`, `posted_by: int` (FK `app_user.id`).
  - `JournalLine`: `entry_id` (FK), `line_no: int`, `account_id` (FK), `debit: Decimal`, `credit: Decimal` (`money_field()`), `memo: str | None`.
  - `EntryCounter` (PK `source_type`, `last_no: int`) and `PeriodLock` (single row, PK `id=1`, `lock_date: date | None`, `set_by: int | None`, `set_at: datetime | None`).

**Constraints and triggers (migration, exact):**
- `account`: partial unique index on `number` where not null; unique index on `(coalesce(parent_id, 0), lower(name))`; partial unique index on `system_role` where not null.
- `journal_entry`: unique `(source_type, entry_no)`; partial unique index on `reverses_entry_id` where not null; index on `entry_date`; `CHECK status IN ('posted','void')`.
- `journal_line`: unique `(entry_id, line_no)`; index on `account_id`; FKs `ON DELETE RESTRICT`; `CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))`.
- Deferred constraint trigger `journal_line_balanced` (AFTER INSERT on `journal_line`, `DEFERRABLE INITIALLY DEFERRED`): at commit, raises unless the entry has ≥ 2 lines and Σdebit = Σcredit.
- `journal_line_immutable`: BEFORE UPDATE OR DELETE (row) and BEFORE TRUNCATE (statement) raise `journal_line is append-only`.
- `journal_entry_guard`: BEFORE UPDATE raises unless the only change is `status` going `posted` → `void`; BEFORE DELETE and TRUNCATE raise.
- Downgrade drops the triggers, functions, then tables.

- [ ] **Step 1: Write failing tests in `tests/ledger/test_schema.py`** (use the `session` fixture and `make_user`). Test names and assertions: `test_line_with_both_sides_is_rejected` (IntegrityError), `test_line_with_neither_side_is_rejected`, `test_unbalanced_entry_fails_at_commit` (debit 10.00 / credit 9.99 → commit raises), `test_single_line_entry_fails_at_commit`, `test_balanced_entry_commits`, `test_line_update_and_delete_rejected`, `test_entry_update_other_than_void_rejected` (change `memo` → error), `test_entry_status_posted_to_void_allowed`, `test_entry_void_back_to_posted_rejected`, `test_entry_delete_and_truncate_rejected`, `test_duplicate_reversal_rejected` (two entries with the same `reverses_entry_id`), `test_account_name_unique_per_parent_case_insensitively`, `test_account_number_unique_when_present_but_nulls_repeat`, `test_system_role_unique`, `test_account_with_lines_cannot_be_deleted` (FK RESTRICT).
- [ ] **Step 2: Run** `uv run pytest tests/ledger/test_schema.py -v` — expect FAIL (no module).
- [ ] **Step 3: Implement the models and the Alembic revision** `a1c5e8d3b7f2` (down_revision `7c1f3a5e8b92`) following `7c1f3a5e8b92_audit_log.py` for style; the migration seeds the single `period_lock` row (`id=1`, `lock_date` null).
- [ ] **Step 4: Migration test** `tests/migrations/test_ledger_migration.py` (scratch-DB pattern from `test_audit_migration.py`): upgrade from `7c1f3a5e8b92` creates the tables and triggers and the `period_lock` row; downgrade removes them; existing `payroll_run` data is untouched.
- [ ] **Step 5: Run** `uv run pytest tests/ledger tests/migrations -v` and `uv run ruff check . && uv run ruff format --check .` — expect PASS.
- [ ] **Step 6: Commit** `feat(ledger): ledger tables with balance, immutability and numbering constraints`

---

### Task 2: Chart of accounts service, default chart and API

**Files:**
- Create: `payroll/ledger/accounts.py`, `payroll/ledger/default_chart.py`, `payroll/ledger/exceptions.py`, `payroll/ledger/schemas.py`, `payroll/ledger/routers.py`
- Modify: `payroll/app.py` (include the ledger router under the API prefix)
- Test: `tests/ledger/conftest.py`, `tests/ledger/test_accounts.py`, `tests/ledger/test_accounts_api.py`

**Interfaces:**
- Consumes: `AppBaseError` (`payroll/exceptions.py`), `ensure_role`/`OWNER` and `require_owner`/`require_reader`, `record_audit`, models from Task 1.
- Produces (`ledger/exceptions.py`, dataclass `AppBaseError` subclasses with `message` and `http_code`): `AccountNotFoundError(404)`, `AccountConflictError(409)` (duplicate number/name, role already bound), `InvalidAccountError(422)` (parent type mismatch, cycle, role/type mismatch, type change with activity), `AccountInUseError(409)`.
- Produces (`ledger/accounts.py`): `class AccountService(session)` with
  `create(*, actor, name, type, number=None, parent_id=None, detail_type=None) -> Account`,
  `update(account_id, *, actor, name=None, number=None, parent_id=None, detail_type=None, type=None) -> Account`,
  `set_active(account_id, active: bool, *, actor) -> Account`,
  `bind_role(role: SystemRole, account_id: int | None, *, actor) -> None` (None unbinds),
  `get(account_id) -> Account`, `list(include_inactive=False) -> list[Account]`,
  `tree(include_inactive=False) -> list[AccountNode]` (each node: account, `depth`, `full_name` joined with `:`),
  `account_by_role(role) -> Account | None`, `normal_balance(account) -> "debit" | "credit"`.
- Produces (`ledger/default_chart.py`): `DEFAULT_CHART: list[ChartRow]` and `seed_default_chart(session, *, actor) -> int` (creates missing rows, binds roles, returns count created; idempotent).
- Produces (`ledger/conftest.py` in tests): fixture `chart` returning `dict[str, Account]` keyed by full name after seeding the default chart.

**Rules:** a child must have the same `type` as its parent; no cycles; `type` may change only while the account has no lines; accounts are never deleted (no delete route), only deactivated; an account with a non-zero balance cannot be deactivated (`AccountInUseError`); system-role binding requires a type match (`accounts_receivable`→asset, `undeposited_funds`→asset, `due_to_owner`→liability, `retained_earnings`→equity, `opening_balance_equity`→equity) and an active account.

**Default chart (names match the existing `ACCT_*` defaults so M4 can map them):** assets `1000 Business Checking`, `1100 Accounts Receivable` (role), `1150 Undeposited Funds` (role); liabilities `2000 Due to Owner` (role), `2100 Payroll Liabilities` with children `Federal Taxes (941/943/944)`, `GA Income Tax`, `S-Corp Health Insurance`, `Co. HSA`, `FUTA Payable`, `GA SUTA Payable`; equity `3000 Common Stock`, `3100 Retained Earnings` (role), `3200 Shareholder Distributions`, `3900 Opening Balance Equity` (role); income `4000 Services Revenue`; expenses `6000 Payroll Expenses` with children `Wages`, `Taxes`, plus top-level `6100 Health Insurance Expense`, `6110 HSA Contribution Expense`, `6200 Professional Fees`, `6210 Software and Subscriptions`, `6220 Office Expenses`, `6230 Travel`, `6240 Meals`, `6250 Bank Charges`, `6900 Other Expenses`.

- [ ] **Step 1: Write failing tests** `test_accounts.py`: `test_create_sets_full_name_and_normal_balance`; `test_child_must_match_parent_type`; `test_cycle_rejected`; `test_duplicate_number_and_duplicate_sibling_name_rejected` (case-insensitive); `test_type_change_blocked_once_account_has_lines` (insert a posted entry directly); `test_deactivate_nonzero_balance_rejected`; `test_bind_role_type_mismatch_rejected`; `test_bind_role_moves_binding_and_unbind`; `test_seed_default_chart_is_idempotent` (second call returns 0, role accounts bound, tree depth of `Payroll Liabilities:GA Income Tax` is 1); `test_mutations_require_owner` (accountant/viewer → `PermissionDeniedError`); `test_every_mutation_is_audited` (action names `account.create|update|activate|deactivate|bind_role`).
- [ ] **Step 2: Write failing API tests** `test_accounts_api.py`: owner can `POST /payroll/api/v1/ledger/accounts` (201) and `PATCH`/`POST .../{id}/deactivate`; viewer gets 200 on `GET .../ledger/accounts` and `GET .../ledger/accounts/tree` but 403 on every mutation; `PUT .../ledger/system-roles/{role}` binds; unknown ids → 404 with the structured error body; unauthenticated → 401.
- [ ] **Step 3: Run** `uv run pytest tests/ledger -v` — expect FAIL.
- [ ] **Step 4: Implement** the exceptions, `AccountService`, the default chart, schemas and router (`APIRouter(prefix="/ledger", dependencies=[Depends(require_reader)])`, mutating routes also `Depends(require_owner)`; services re-check the role with `ensure_role` and commit once per call).
- [ ] **Step 5: Run** `uv run pytest tests/ledger -v` — expect PASS; ruff clean.
- [ ] **Step 6: Commit** `feat(ledger): chart of accounts service, default chart and API`

---

### Task 3: Posting service — post, reverse, void, manual journals

**Files:**
- Create: `payroll/ledger/services.py`
- Modify: `payroll/ledger/exceptions.py`, `payroll/ledger/schemas.py`, `payroll/ledger/routers.py`
- Test: `tests/ledger/test_posting.py`, `tests/ledger/test_posting_concurrency.py`, `tests/ledger/test_journal_api.py`

**Interfaces:**
- Consumes: `AccountService`, models (Task 1), `PeriodLock` row (Task 4 supplies the setter; this task only reads `lock_date`).
- Produces (`ledger/exceptions.py`): `InvalidEntryError(422)`, `UnbalancedEntryError(422)`, `InactiveAccountError(422)`, `PeriodLockedError(409)`, `EntryNotFoundError(404)`, `AlreadyReversedError(409)`.
- Produces (`ledger/services.py`):
  - `@dataclass(frozen=True) LineDraft(account_id: int, debit: Decimal = Decimal("0"), credit: Decimal = Decimal("0"), memo: str | None = None)`
  - `@dataclass(frozen=True) EntryDraft(entry_date: date, lines: list[LineDraft], memo: str | None = None, source_type: SourceType = SourceType.MANUAL, source_id: str | None = None)`
  - `class LedgerPostingService(session)`: `post(draft, *, actor) -> JournalEntry`, `reverse(entry_id, *, entry_date: date, actor, memo: str | None = None) -> JournalEntry`, `void(entry_id, *, entry_date: date, actor, memo: str | None = None) -> JournalEntry`. **None of these commit** (callers own the transaction; they flush and write the audit row).
  - `class JournalService(session)`: `create_manual(draft, *, actor) -> JournalEntry`, `reverse_manual(...)`, `void_manual(...)` — owner-checked wrappers that commit. Also `get(entry_id) -> JournalEntry` and `list(start=None, end=None, source_type=None, limit=100, offset=0) -> list[JournalEntry]`.

**Algorithm (`post`), in this order:** (1) amounts: each is a `Decimal`, finite, `exponent >= -2`, within the allowed range, exactly one side > 0; (2) ≥ 2 lines and Σdebit = Σcredit; (3) every account exists and is active; (4) `entry_date` is after `period_lock.lock_date` (read the lock row with `FOR SHARE`); (5) take the `EntryCounter` row for `source_type` with `FOR UPDATE` (insert-if-missing first), increment, assign `entry_no`; (6) insert entry + lines, flush; (7) audit `ledger.post` with the entry id, number, date, totals. Steps 1–4 happen before the counter is touched.
- `reverse`: loads the entry, errors `AlreadyReversedError` if one exists, builds a draft with debit/credit swapped, same `source_type`/`source_id`, memo `Reversal of <source_type> #<no>` (or the given memo), `reverses_entry_id` set; posts it. `void` = `reverse` plus setting the original's `status` to `void` (the only permitted header update). A reversal can itself be reversed once.
- API: `POST /ledger/journal-entries` (201), `GET /ledger/journal-entries`, `GET /ledger/journal-entries/{id}`, `POST .../{id}/reverse` and `.../{id}/void` (body `{ "entryDate": "YYYY-MM-DD", "memo": null }`). Responses show lines with account number/name and the entry's reversal link.

- [ ] **Step 1: Write failing tests** `test_posting.py` (names/assertions): `test_post_balanced_entry_assigns_entry_no_1_then_2`; `test_numbers_are_per_source_type` (manual #1 and opening_balance #1); `test_failed_post_consumes_no_number` (unbalanced attempt then valid → #1); `test_rejects_three_decimal_amount` (`Decimal("10.005")`), `test_rejects_nan_and_infinity`, `test_rejects_negative_and_zero`, `test_rejects_both_sides_on_one_line`, `test_rejects_amount_over_max` (`Decimal("1e20")` → `InvalidEntryError`, not a DB error), `test_rejects_single_line_and_unbalanced`, `test_rejects_inactive_and_unknown_account`, `test_rejects_date_on_or_before_lock_date` (lock 2026-03-31: 03-31 rejected, 04-01 accepted), `test_reverse_swaps_sides_and_links` (net balance zero, original stays `posted`), `test_reverse_twice_rejected`, `test_void_marks_original_void_and_reversal_posts`, `test_void_twice_rejected`, `test_reverse_into_locked_period_rejected`, `test_reversal_of_reversal_allowed_once`, `test_post_writes_audit_row`, `test_manual_create_requires_owner`.
- [ ] **Step 2: Concurrency tests** `test_posting_concurrency.py` (real separate sessions/threads on the test engine): `test_parallel_posts_get_contiguous_unique_numbers` (20 threads, numbers == 1..20), `test_parallel_posts_with_failures_leave_no_gaps`, `test_post_waits_for_lock_date_change` (a transaction holding the lock-row update blocks a post, which then rejects if the new lock covers its date).
- [ ] **Step 3: API tests** `test_journal_api.py`: create via JSON with string amounts → 201 and body money as strings; 422 body for an unbalanced entry carries the structured error; viewer 403 on create/reverse/void, 200 on GET; reverse/void round-trips; list filters by date range and source type.
- [ ] **Step 4: Run** `uv run pytest tests/ledger -v` — expect FAIL.
- [ ] **Step 5: Implement** services, schemas, routes.
- [ ] **Step 6: Run** `uv run pytest tests/ledger -v` — expect PASS; ruff clean.
- [ ] **Step 7: Commit** `feat(ledger): posting service with reversal, void and gap-free numbering`

---

### Task 4: Period lock

**Files:**
- Create: `payroll/ledger/period_lock.py`
- Modify: `payroll/ledger/exceptions.py`, `payroll/ledger/schemas.py`, `payroll/ledger/routers.py`
- Test: `tests/ledger/test_period_lock.py`

**Interfaces:**
- Produces: `class PeriodLockService(session)`: `get() -> date | None`; `set(lock_date: date | None, *, password: str, actor: User) -> None`. Owner only. The current user's password must be re-verified with `payroll.auth.security.verify_password` (Argon2 verify goes through the existing concurrency semaphore; verify before taking any row lock, as in the M0 login flow). New error `LockPasswordError(403)`. A failed confirmation writes audit action `ledger.lock.denied`; success writes `ledger.lock.set` with before/after dates and commits.
- API: `GET /ledger/period-lock` (any role), `PUT /ledger/period-lock` body `{ "lockDate": "2026-03-31", "password": "…" }` (owner).

- [ ] **Step 1: Write failing tests:** `test_set_requires_correct_password`; `test_wrong_password_is_audited_and_changes_nothing`; `test_set_lock_blocks_posting_on_and_before_date` (uses `LedgerPostingService.post`); `test_lock_can_be_moved_earlier_and_cleared_with_password`; `test_non_owner_cannot_set` (403 even with the right password); `test_password_not_in_audit_row_or_logs` (caplog and audit JSON do not contain the password); API tests for GET (viewer ok) and PUT (owner ok, accountant 403).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS, ruff clean.
- [ ] **Step 5: Commit** `feat(ledger): password-confirmed period lock date`

---

### Task 5: Period resolver and company timezone

**Files:**
- Create: `payroll/reports/__init__.py`, `payroll/reports/periods.py`
- Modify: `payroll/__init__.py` (setting `company_timezone: str = "America/New_York"`, validated with `zoneinfo.ZoneInfo`), `.env.example`
- Test: `tests/reports/__init__.py`, `tests/reports/test_periods.py`

**Interfaces:**
- Produces (`reports/periods.py`):
  - `@dataclass(frozen=True) Period(start: date, end: date, label: str)`
  - `PRESETS: tuple[str, ...] = ("this_month", "last_month", "this_quarter", "last_quarter", "this_year", "last_year", "ytd", "trailing_12")`
  - `company_today(now: datetime | None = None) -> date` (company-timezone date)
  - `resolve_period(preset: str, today: date | None = None, *, start: date | None = None, end: date | None = None) -> Period` — preset `"custom"` requires `start` and `end`.
  - `previous_period(period: Period) -> Period` and `previous_year(period: Period) -> Period`.
  - `InvalidPeriodError(AppBaseError, 422)` for unknown preset, missing custom dates, or `start > end`.

**Definitions** (today = 2026-11-02): `this_month` 2026-11-01..2026-11-30; `last_month` 2026-10-01..2026-10-31; `this_quarter` 2026-10-01..2026-12-31; `last_quarter` 2026-07-01..2026-09-30; `this_year` 2026-01-01..2026-12-31; `last_year` 2025-01-01..2025-12-31; `ytd` 2026-01-01..2026-11-02; `trailing_12` = one year back plus one day through today → 2025-11-03..2026-11-02. `previous_period` of a whole calendar month/quarter/year is the prior calendar month/quarter/year; any other period is the same number of days ending the day before `start`. `previous_year` shifts both ends back one year (Feb 29 → Feb 28).

- [ ] **Step 1: Write failing tests:** parametrized table above; `test_january_wraps_to_prior_year` (today 2026-01-15: `last_month` 2025-12-01..2025-12-31, `last_quarter` 2025-10-01..2025-12-31); `test_leap_day` (today 2028-02-29: `trailing_12` start 2027-03-01, `this_month` ends 2028-02-29, `previous_year` of Feb 2028 is 2027-02-01..2027-02-28); `test_custom_requires_both_dates_and_order`; `test_previous_period_calendar_vs_arbitrary` (Q3 → Q2; 2026-03-10..2026-03-20 → 2026-02-27..2026-03-09); `test_company_today_uses_company_timezone` (a UTC instant of 2026-11-03T02:00Z with `America/New_York` is 2026-11-02); `test_unknown_preset_rejected`; `test_invalid_timezone_setting_fails_fast`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (pure functions, no I/O). **Step 4: Run** — PASS. **Step 5: Commit** `feat(reports): period presets, comparison periods and company timezone`

---

### Task 6: Ledger queries — Trial Balance, General Ledger, Journal, Account Register

**Files:**
- Create: `payroll/reports/queries.py`, `payroll/reports/statements.py` (ReportService skeleton with these four), `payroll/reports/schemas.py`, `payroll/reports/routers.py`
- Modify: `payroll/app.py` (include router)
- Test: `tests/reports/conftest.py`, `tests/reports/test_ledger_reports.py`, `tests/reports/test_reports_api.py`

**Interfaces:**
- Consumes: models, `AccountService.tree`, `LedgerPostingService.post` (to build fixtures).
- Produces (`reports/statements.py`): `class ReportService(session)`:
  - `trial_balance(as_of: date, *, include_zero=False) -> TrialBalance` — rows `(account_id, number, name, debit, credit)` where each account's net balance is in exactly one column; totals; `balanced: bool`.
  - `account_register(account_id: int, start: date, end: date) -> Register` — opening balance row, each line with `entry_id`, `entry_no`, `source_type`, memo, debit, credit, running balance (natural sign), closing balance.
  - `general_ledger(start: date, end: date, account_id: int | None = None) -> GeneralLedger` — one register per account with activity or an opening balance.
  - `journal(start: date, end: date) -> list[JournalReportEntry]` ordered by `entry_date, source_type, entry_no`.
- Natural sign: asset/cogs/expense/other_expense = debits − credits; liability/equity/income/other_income = credits − debits. Voided originals and their reversals are both included (they net to zero) and rows carry `is_void`/`is_reversal`.
- Fixture `tests/reports/conftest.py::books` posts a small known year (opening equity 10,000.00; two income entries 4,000.00 and 2,500.00; expenses 1,200.50 and 300.25; a reversed entry) so expected numbers are literal.
- API: `GET /ledger/reports/trial-balance?asOf=`, `/general-ledger?start=&end=&accountId=`, `/journal?start=&end=`, `/account-register/{id}?start=&end=` under `/payroll/api/v1`; any role.

- [ ] **Step 1: Write failing tests:** `test_trial_balance_columns_and_totals_literal` (exact debit/credit per account from `books`); `test_trial_balance_empty_ledger_is_balanced_zero`; `test_trial_balance_as_of_before_first_entry_is_empty`; `test_register_running_balance_and_opening_row` (period starting mid-way shows the opening balance from earlier entries); `test_contra_balance_keeps_its_sign` (an expense account with a net credit shows negative); `test_general_ledger_skips_accounts_without_activity`; `test_journal_orders_and_lists_all_lines`; `test_void_and_reversal_both_appear_and_net_zero`; API tests for each endpoint (viewer 200, anonymous 401, `start > end` → 422 `InvalidPeriodError`, unknown account → 404).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** with set-based SQL aggregates (`GROUP BY account_id`), not per-row Python loops. **Step 4: Run** — PASS. **Step 5: Commit** `feat(reports): trial balance, general ledger, journal and account register`

---

### Task 7: Profit & Loss, Balance Sheet, comparison, basis toggle, invariants

**Files:**
- Create: `payroll/reports/basis.py`
- Modify: `payroll/reports/statements.py`, `payroll/reports/schemas.py`, `payroll/reports/routers.py`, `payroll/reports/queries.py`
- Test: `tests/reports/test_statements.py`, extend `tests/reports/test_reports_api.py`

**Interfaces:**
- Produces (`reports/basis.py`): `class Basis(StrEnum)`: `cash`, `accrual`; default `Basis.CASH` (owner's decision). `def recognized_lines(basis: Basis) -> ColumnElement[bool] | None` — the SQL predicate restricting which `journal_line`/entry rows count at their entry date. **Returns `None` for both bases in M1** because no document-backed (invoice/expense) sources exist yet; M2 adds the invoice-to-payment attribution here. Every query in `queries.py` passes through this hook, so M2 changes one function.
- Produces (`reports/statements.py`): `ReportService.profit_and_loss(period: Period, *, basis=Basis.CASH, compare: str | None = None) -> Statement` and `balance_sheet(as_of: date, *, basis=Basis.CASH, compare: str | None = None) -> Statement`, where `compare` is `None | "previous_period" | "previous_year"`.
- Produces (`reports/schemas.py`): `Statement(title, basis, columns: list[ColumnHeader], rows: list[StatementRow], invariants: list[Invariant], basis_note: str)`; `ColumnHeader(label, start, end)`; `StatementRow(kind: "section"|"account"|"subtotal"|"total", label, account_id, depth, amounts: list[Decimal], change: Decimal | None, pct_change: Decimal | None)`; `Invariant(name, ok, detail)`.

**Layout and rules**
- P&L sections in order: Income; Cost of Goods Sold; **Gross Profit**; Expenses; **Net Operating Income**; Other Income; Other Expenses; **Net Income**. Parent accounts show a rolled-up subtotal of their children plus their own postings. Zero-balance accounts without activity in any column are hidden.
- Balance Sheet: Assets, Liabilities, Equity. Equity = equity accounts + synthesized `Prior Years Earnings` (Σ of all income/expense/other lines dated before the fiscal-year start of `as_of`, natural sign income − expense — correct whether or not a year-end close exists, because closed years net to zero in P&L accounts) + synthesized `Net Income` (fiscal-year-to-date as of `as_of`). Totals: Total Assets, Total Liabilities, Total Equity, Liabilities + Equity.
- Invariants always computed: `assets_equal_liabilities_plus_equity`, `trial_balance_balanced`; failure sets `ok=False` (the report still renders).
- Comparison adds a column per `compare` and sets `change = current − compare`, `pct_change = change / |compare| × 100` rounded half-up to 0.1, `None` when the comparison amount is 0.
- `basis_note` for both bases in M1: "Cash and accrual agree until invoices are entered; the difference starts with Receivables (M2)." — text lives in one constant.

- [ ] **Step 1: Write failing tests (literal expectations from the `books` fixture):** `test_pnl_net_income_equals_income_minus_expenses` (6,500.00 − 1,500.75 = 4,999.25); `test_pnl_sections_and_gross_profit_with_cogs`; `test_pnl_parent_rolls_up_children`; `test_pnl_hides_zero_accounts`; `test_balance_sheet_balances_with_current_year_earnings`; `test_balance_sheet_year_two_includes_unclosed_prior_year_earnings` (entries in 2025 and 2026, no close; BS as of 2026-06-30 still balances and shows Prior Years Earnings); `test_balance_sheet_balances_after_year_end_close_entry` (manual closing entry posted as `year_end_close`: same totals); `test_contra_asset_shows_negative`; `test_compare_previous_period_and_year_columns_and_pct` (change literal, `pct_change` None when prior is 0, rounding half-up 0.1); `test_empty_ledger_statements_have_zero_totals_and_ok_invariants`; `test_basis_default_is_cash_and_accrual_equal_in_m1`; `test_invariant_failure_is_reported_not_raised` (monkeypatch a query to unbalance); API: `GET /ledger/reports/profit-and-loss?preset=last_quarter&basis=cash&compare=previous_period`, `GET .../balance-sheet?asOf=...`, unknown basis/compare → 422.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(reports): profit and loss, balance sheet, comparison and basis toggle`

---

### Task 8: Navigation, chart-of-accounts pages, ledger settings

**Files:**
- Create: `payroll/frontend/ledger_routers.py`, `payroll/frontend/templates/accounts/{list.html,form.html,register.html}`, `payroll/frontend/templates/settings/ledger.html`
- Modify: `payroll/frontend/templates/base.html` (top navigation: Payroll, Accounts, Journal, Reports, Settings; current-page highlight), `payroll/app.py` (include the router)
- Test: `tests/frontend/test_ledger_pages.py`

**Interfaces:**
- Produces pages (full pages, `include_in_schema=False`, router-level `require_reader`, mutating routes `require_owner`): `GET /accounts` (tree, inactive toggle, balances as of today), `GET/POST /accounts/new`, `GET/POST /accounts/{id}/edit`, `POST /accounts/{id}/active`, `GET /accounts/{id}/register?start=&end=` (the Account Register with links to `/journal/{entry_id}`), `GET /settings/ledger` and `POST /settings/ledger/roles`, `POST /settings/ledger/lock` (lock date + password field).
- Templates use existing filters `currency`, `amount`, `nonzero`; forms carry the CSRF token via `csrf_context`; validation errors re-render the form with the entered values and status 422; `AppBaseError` shows through the existing `_error.html` partial pattern.

- [ ] **Step 1: Write failing tests:** owner can create a parent and a child account via forms and sees them nested; duplicate number re-renders with the error and keeps typed values; viewer sees the pages but the create/edit/deactivate/role/lock controls are absent and POSTs return 403; `<script>alert(1)</script>` as an account name is escaped in list, form and register; register page shows opening balance, running balance and entry links; settings page changes the lock date only with the right password; navigation shows all five links and marks the active one; pages return `Cache-Control: no-store`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5: Commit** `feat(frontend): chart of accounts, account register and ledger settings pages`

---

### Task 9: Journal pages — list, entry editor, view, reverse, void

**Files:**
- Create: `payroll/frontend/templates/journal/{list.html,form.html,view.html}`, `payroll/frontend/static/js/journal.js`
- Modify: `payroll/frontend/ledger_routers.py`
- Test: `tests/frontend/test_journal_pages.py`

**Interfaces:**
- Pages: `GET /journal` (filters: date range, source type; paging), `GET /journal/new`, `POST /journal` (create), `GET /journal/{id}` (entry with lines, reversal/void links, status badge), `POST /journal/{id}/reverse` and `/void` (date field, default today in company timezone).
- `journal.js` (no inline script; loaded with `<script src>`): adds/removes line rows, shows live Σdebit, Σcredit and the difference, disables Submit until the entry balances and has ≥ 2 filled lines; server validation is authoritative and re-renders with the entered rows on error. Amount parsing in the POST handler accepts `1234.56` and `1,234.56` and a leading `$`, rejects anything else (including 3 decimals) with a field error.

- [ ] **Step 1: Write failing tests:** creating a balanced two-line entry via the form redirects (303) to the entry page and shows the number; an unbalanced post re-renders 422 with the rows preserved and the error text; `1,234.56` and `$10.00` are accepted, `10.005` and `abc` rejected; view page for a reversed entry shows the link both ways; reverse and void forms work and double-reverse shows a 409 message; viewer sees list/view but no buttons and POST → 403; memo containing `<img src=x onerror=alert(1)>` is escaped everywhere; page contains no inline `<script>` bodies or `on*=` attributes (CSP test pattern from `tests/frontend/test_csp_compat.py`); `journal.js` is served from `/static/js/`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5: Commit** `feat(frontend): journal list, entry editor, reverse and void`

---

### Task 10: Report pages with period picker, comparison and drill-down

**Files:**
- Create: `payroll/frontend/report_routers.py`, `payroll/frontend/templates/reports/{index.html,statement.html,trial_balance.html,general_ledger.html,journal.html,_picker.html}`
- Modify: `payroll/app.py` (include the router)
- Test: `tests/frontend/test_report_pages.py`

**Interfaces:**
- Pages (any role): `GET /reports` (index of reports), `GET /reports/profit-and-loss`, `/reports/balance-sheet`, `/reports/trial-balance`, `/reports/general-ledger`, `/reports/journal`. Query params: `preset` (one of `PRESETS` or `custom`), `start`, `end`, `asOf` (balance sheet / trial balance; defaults to company today), `basis` (`cash` default | `accrual`), `compare` (`previous_period` | `previous_year`). The picker is a plain GET form (works without JS); HTMX only swaps the report body.
- Drill-down: every account row links to `/accounts/{id}/register?start=&end=` for the same period (balance sheet rows use the fiscal-year start to `asOf`); register rows link to `/journal/{entry_id}`. Rows for synthesized equity lines have no link. A visible invariant banner (green "Balanced" / red with the detail). The basis toggle shows `basis_note`.

- [ ] **Step 1: Write failing tests:** default P&L renders cash basis for the preset `this_month` with literal numbers from `books` (freeze "today" by monkeypatching `company_today`); switching to `last_quarter`/custom changes the heading dates; `compare=previous_year` shows the extra column and `% change`; an invalid custom range shows an inline error (422), not a stack trace; account rows link to the register with the right query string, and following the link shows the same total; balance sheet banner turns red when an invariant fails (monkeypatched); all report pages are reachable by viewer and accountant and carry `Cache-Control: no-store`; anonymous → 303 to `/login`; account name with HTML is escaped.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5: Commit** `feat(frontend): report pages with presets, comparison and drill-down`

---

### Task 11: CSV and PDF export

**Files:**
- Create: `payroll/reports/export.py`, `payroll/frontend/templates/reports/print.html`
- Modify: `payroll/reports/routers.py`, `payroll/frontend/report_routers.py` (export links), `pyproject.toml`/`uv.lock` (`uv add weasyprint`), `Dockerfile` (runtime libs `libpango-1.0-0 libpangoft2-1.0-0`), `.github/workflows/test.yml` and `build.yml` if it runs tests (same apt packages), `README.md`
- Test: `tests/reports/test_export.py`

**Interfaces:**
- Produces: `to_csv(report) -> str` (UTF-8, `\r\n`, one table per report with a title/period header block), `to_pdf(html: str) -> bytes`, `csv_safe(value: str) -> str`.
- Routes (any role): the report page URLs plus `.csv` / `.pdf` (e.g. `/reports/profit-and-loss.csv?preset=ytd`) and API twins under `/ledger/reports/...`. Each export writes an audit row `report.export` (`entity` = report name, `after` = parameters and format) and commits; `Content-Disposition: attachment; filename="<report>-<period>.<ext>"`, `Cache-Control: no-store`.
- `csv_safe`: a text cell whose first character is `=`, `+`, `-`, `@`, tab or CR gets a leading `'`. Numeric cells (money strings) are written as-is and are **not** touched, so `-12.00` stays `-12.00`.
- PDF: `weasyprint.HTML(string=html, url_fetcher=<function that always raises>)`, so no network or `file://` fetch is possible.

- [ ] **Step 1: Write failing tests:** `test_csv_has_title_period_columns_and_literal_totals`; `test_csv_neutralizes_formula_cells` (memo `=HYPERLINK("http://x","y")` → starts with `'=`; account name `@SUM(1)` likewise; amount `-12.00` untouched); `test_csv_quotes_commas_quotes_and_newlines`; `test_pdf_starts_with_pdf_header_and_has_content_disposition`; `test_pdf_cannot_fetch_external_or_local_resources` (HTML with `<img src="file:///etc/hosts">` and an `http://127.0.0.1:1/x.png` renders without any fetch attempt — assert via the fetcher raising and no network call); `test_export_requires_login`; `test_viewer_can_export_and_it_is_audited`; `test_filenames_contain_no_user_input_beyond_dates`. PDF tests use `pytest.importorskip` style skip when the native Pango libraries are missing locally (catch `OSError` on import), and CI installs them so they run there.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (`uv add weasyprint`; print template extends nothing, inline `<style>` allowed because it is never served to a browser). **Step 4: Run** — PASS; also `docker build .` succeeds and `docker run --rm <image> python -c "import weasyprint; weasyprint.HTML(string='<p>x</p>').write_pdf()"` exits 0.
- [ ] **Step 5: Commit** `feat(reports): CSV and PDF export with formula-injection and fetch protection`

---

### Task 12: Hardening, demo data, docs, release 2.1.0

**Files:**
- Create: `payroll/ledger/cli.py`, `tests/ledger/test_properties.py`, `tests/ledger/test_cli.py`
- Modify: `tests/auth/test_route_protection.py` (new routes + raise `MIN_OWNER_ONLY_UNSAFE_ENDPOINTS` to the new count), `pyproject.toml` (`uv add --dev hypothesis`, version `2.1.0`), `payroll/__init__.py`, `.env.example` (`APP_VERSION=2.1.0`, `COMPANY_TIMEZONE`), `README.md`, `docs/RUNBOOK.md`, `docs/SPEC.md` (milestone M1 status, cash/accrual note), `/Users/jason/Documents/ProjectOS/01 Projects/Payroll App.md` (Decision Log, Next Steps, Resume block, Agent Activity Log, frontmatter `updated`)

**Interfaces:**
- Produces CLI (`python -m payroll.ledger.cli`): `seed-chart --email OWNER` (default chart, idempotent) and `seed-demo --email OWNER` (a fictitious year of journals for exploring the reports; **refuses** when any entry exists or when the DB is not empty of real data unless `--force`; prints what it created). Passwords are never prompted; the actor is looked up by owner email.

- [ ] **Step 1: Property tests (Hypothesis, real Postgres, small example counts):** `test_random_valid_entries_keep_trial_balance_balanced`; `test_assets_equal_liabilities_plus_equity_for_any_valid_sequence` (random balanced entries across all account types and random dates over two years, no close); `test_reverse_then_original_nets_to_zero`; `test_void_and_repost_equals_single_post_in_reports`; `test_report_totals_independent_of_posting_order`.
- [ ] **Step 2: Authorization sweep:** extend `test_route_protection.py` so the new routes are enumerated; add `test_viewer_and_accountant_can_read_all_reports_but_cannot_mutate_ledger` (parametrized over every new unsafe route: expect 403 for both roles, 401/303 anonymous).
- [ ] **Step 3: CLI tests:** seed-chart twice → second run creates 0; seed-demo on an empty ledger posts entries, all reports balance; seed-demo with existing entries exits non-zero without `--force`.
- [ ] **Step 4: Docs and version:** README (new modules, pages, reports, CLI, timezone setting, PDF libraries); RUNBOOK (M1 migration is additive, no data conversion: `pg_dump` still first; how to seed the chart; locking a closed year); SPEC milestones table marks M1 done and records that cash/accrual differences arrive with M2; bump version to `2.1.0`.
- [ ] **Step 5: Full verification:** `uv run ruff check . && uv run ruff format --check . && uv run pytest` (coverage ≥ 96, all passing); `docker compose -f compose.test.yml up -d db`; `docker build .`; start the app against a scratch database, run `seed-chart` and `seed-demo`, and walk Accounts → Journal → each report → CSV/PDF with the browser tools, saving a screenshot of the P&L and Balance Sheet.
- [ ] **Step 6: ProjectOS:** add decisions (cash/accrual deferred to M2; owner-only mutations; derived normal balance; account numbers optional; timezone setting; WeasyPrint added), Agent Activity Log entry, Next Steps → M2 plan.
- [ ] **Step 7: Commit** `feat: M1 ledger and reports, release 2.1.0` then push `feat/m1-ledger` and open a PR against `feat/m0-foundation`.
