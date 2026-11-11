# M4 Payroll to Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Saved payroll runs become drafts that Approve posts to the ledger, with FUTA and Georgia SUTA calculated, tax payments recorded, and a Payroll liability report, released as v2.4.0.

**Architecture:** The existing `payroll/payroll_run/` package keeps the calculator and gains a run lifecycle (draft, approved, void, imported) and a posting service that posts exactly one journal entry per approved run through `LedgerPostingService`, in the pattern of `payroll/expenses/`. A new pure function computes FUTA/SUTA; a settings table holds per-year rates; a `tax_payment` document (new module `payroll/payroll_run/tax_payments.py`) posts Dr payable / Cr bank; a derived liability report ages unpaid periods. Integrity is enforced in the database (CHECKs, triggers) as in M2/M3. Pages extend `payroll/frontend/routers.py` and new routers.

**Tech Stack:** FastAPI, SQLModel, Alembic, Postgres 17, Jinja2 + HTMX, Python 3.14, uv, ruff, pytest, Hypothesis, WeasyPrint.

**Spec:** `docs/superpowers/specs/2026-11-11-m4-payroll-to-ledger-design.md` (binding; this plan argues from it). Also `docs/SPEC.md` F9, F10, §4.5, §4.6, §7.

## Global Constraints

- Python 3.14, `uv` only; `ruff check` and `ruff format` clean; `pre-commit` passing.
- Run the full suite before a task is done: `uv run pytest` (test DB localhost:5433, container `payroll-test-db-1`; `docker start payroll-test-db-1` if stopped). Never run two pytest runs at once. Coverage must not drop.
- **The tax engine is frozen:** `payroll/tax/engine.py`, `constants.py` and the 73 engine tests plus `tests/tax/golden_cases.json` must pass unmodified at every commit. Figures on runs saved before M4 must not change.
- Money is `Decimal`, 2 dp via `money_field`; amounts parsed strictly (finite, range before quantize, as `payroll/common/documents.py checked_amount`). Enum-like columns are plain strings with CHECKs.
- Every mutation: owner-only (`ensure_role(actor, OWNER)` in the service), audited (`record_audit`), CSRF on routes, commits through `commit_or_rollback`, validation errors are 422 never 500 (parse ids/dates/enums/amounts in the route before any service call; whole-argument None must never reach a service).
- Posting through `LedgerPostingService`; reversals via `LedgerPostingService.void`; void date on or after the document date (`check_void_date`); service parameters are `pay_date`, `payment_date`, `void_date` (never `date`).
- Locking order for approve and void: payroll role accounts FOR SHARE (id order) and the net-pay account FOR SHARE, then all `payroll_run` rows of the tax year FOR UPDATE in id order (`lock_rows` style), then the period lock FOR SHARE, then the per-source entry counter. Tax payments: role account then bank account FOR SHARE, then payment row FOR UPDATE on void.
- Use the shared helpers in `payroll/common/documents.py` (`commit_or_rollback`, `locked_role_account`, `lock_rows`, `lock_accounts`, `checked_amount`, `clean_text`, `is_int`, `check_void_date`, `void_memo`, `PAYMENT_TYPES`, `RESERVED_ROLES`); never copy them.
- Memo cap 500 characters, control characters refused, CSV through `csv_safe`.
- Every new POST route is added to the route-protection sweep (`tests/auth/test_route_protection.py`) in the task that adds it, with viewer, accountant, anonymous and no-CSRF cases.
- Do not push, open PRs, or edit ProjectOS inside a task; the controller does that.
- Commit after each task with a conventional message ending `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Behaviors the spec implies and a person would hit, each pinned by a named test in its owning task:

1. Two approvals racing in one tax year, and approve racing a void: the chain rule holds (Task 6, real two-session tests, mutation-checked).
2. Runs saved before M4 come out of the migration byte-identical, marked `imported`, still feed the year-to-date chain, and still show their QuickBooks entries (Task 2 and Task 5).
3. FUTA/SUTA at the wage-base boundaries: crossing the base mid-run, already over it, zero wages, rate 0, half-up rounding (Task 1).
4. The posted entry balances for health in and out of income tax, HSA zero and non-zero, net pay to bank and to Due to Owner (Task 6, property in Task 12).
5. Void in reverse order only, then re-save and re-approve the same period (Task 6).
6. The liability report: ledger balance equals unpaid periods plus the `other` line; a manual journal shows up as `other`, not hidden (Task 8).

---

### Task 1: Unemployment tax function

**Files:**
- Create: `payroll/tax/unemployment.py`
- Test: `tests/tax/test_unemployment.py`

**Interfaces:**
- Produces `UnemploymentRates` (frozen dataclass: `futa_rate`, `futa_wage_base`, `suta_rate`, `suta_wage_base`, all `Decimal`) and `UnemploymentResult` (frozen dataclass: `futa_wages`, `futa`, `suta_wages`, `suta`, all `Decimal` 2 dp), and `calc_unemployment(wages_before: Decimal, wages_after: Decimal, rates: UnemploymentRates) -> UnemploymentResult`.

- [ ] **Step 1:** Write `tests/tax/test_unemployment.py`: `wages_before=0, after=5000` gives futa_wages 5000.00 and futa 30.00 at 0.006; crossing the base (`before=6500, after=8500`, base 7000) gives futa_wages 500.00; already over (`before=7500, after=9500`) gives 0.00 and 0.00; zero wages; rate 0 gives 0.00 amounts; half-up rounding (`futa_wages=123.45` x 0.006 = 0.7407 -> 0.74; a case at exactly .005 rounds up); SUTA base 9500 with rate 0.0264 across the base; `after < before` or negative inputs raise `ValueError`; results are quantized to 2 dp; the module imports nothing from `payroll.payroll_run`.
- [ ] **Step 2:** Run it; expect FAIL (module missing).
- [ ] **Step 3:** Implement per spec §5 using `round_money` from `payroll/common/money.py`.
- [ ] **Step 4:** Run `uv run pytest tests/tax` (engine and golden included); expect all PASS with the golden cases untouched.
- [ ] **Step 5:** Commit `feat(tax): unemployment tax calculation`.

### Task 2: Schema, models, exceptions

**Files:**
- Create: `payroll/migrations/versions/e5f1b3c9d7a2_payroll_ledger.py`, `payroll/payroll_run/ledger_models.py` (PayrollSettings, TaxPayment), `tests/payroll_run/test_ledger_schema.py`, `tests/payroll_run/conftest.py` additions
- Modify: `payroll/payroll_run/models.py` (new `PayrollRun` columns), `payroll/payroll_run/exceptions.py`, `payroll/migrations/env.py` (import new models), `payroll/migrations/alembic_runner.py` (`_HEAD_REVISION`), `tests/receivables/test_properties.py` and `tests/expenses/test_properties.py` TRUNCATE lists if the new tables need them

**Interfaces:**
- Produces model columns per spec §4: on `PayrollRun`: `status`, `pay_date`, `net_pay_source`, `net_pay_account_id`, `entry_id`, `void_entry_id`, `void_date`, `futa_wages`, `futa`, `suta_wages`, `suta`, `suta_rate`; `PayrollSettings(tax_year, futa_rate, futa_wage_base, suta_rate, suta_wage_base)` unique per `tax_year`; deposit rule columns on a second singleton model `PayrollDepositRules` (`irs_941_cadence`, `irs_941_due_day`, `ga_dor_cadence`, `ga_dor_due_day`, `futa_cadence`, `futa_due_day`, `ga_dol_cadence`, `ga_dol_due_day`, cadence in `monthly|quarterly`, due day 1..31 or 0 meaning end of month); `TaxPayment` per spec §4; exceptions (`AppBaseError` dataclasses): `PayrollRunStateError` (409), `InvalidPayrollRunError` (422), `PayrollSettingsMissingError` (422), `PayrollRoleNotBoundError` (422), `TaxPaymentNotFoundError` (404), `InvalidTaxPaymentError` (422), `TaxPaymentError` (409).
- conftest fixtures: `draft_run` (a saved draft built through the service later; for this task build rows directly), `payroll_roles` (binds the eight roles to accounts it creates in the test chart).

- [ ] **Step 1:** Write `test_ledger_schema.py`: each CHECK and trigger in spec §4 by raw SQL with specific error matches (status values; approved needs `pay_date`, `entry_id`, `net_pay_source`; `net_pay_source='owner'` iff no account; void needs void fields and `void_date >= pay_date`; draft and imported carry no entry or void fields; non-negative FUTA/SUTA; the immutability trigger refuses UPDATE of an input or calculated column on non-draft rows but allows status `approved -> void` with void fields; delete of a non-draft row refused; the partial unique index allows one non-void run per `pay_period` and a second after the first is void; tax_payment amount, status, void rules); the data step marks pre-existing rows `imported` with figures byte-identical (build the pre-migration state by upgrading a scratch DB to `d4e0a2b8c6f1`, inserting a run row with raw SQL, then upgrading; compare every pre-existing column before and after); models match the migration; alembic head equals `_HEAD_REVISION`; downgrade refuses with a clear `RuntimeError` when any run is approved or void or has an entry, or any tax payment exists, and otherwise drops everything cleanly (assert `alembic_version` after).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement the migration and models. Triggers use `FOR NO KEY UPDATE` reasoning where they read other rows. The immutability trigger lists the allowed-to-change columns explicitly.
- [ ] **Step 4:** Run `uv run pytest tests/payroll_run tests/migrations tests/receivables/test_schema.py tests/expenses/test_schema.py`; expect PASS.
- [ ] **Step 5:** Commit `feat(payroll): run lifecycle schema, settings and tax payment tables`.

### Task 3: Payroll roles

**Files:**
- Modify: `payroll/ledger/models.py` (`SystemRole` values), `payroll/ledger/accounts.py` (`ROLE_TYPES`, `FROZEN_ROLES`, `_payroll_activity`), `payroll/ledger/default_chart.py` (bind the default accounts), `payroll/frontend/ledger_routers.py` (`ROLE_LABELS`), the migration data step in `e5f1b3c9d7a2` (bind roles on an existing install only when the exact default accounts exist and the role is unbound)
- Test: `tests/payroll_run/test_payroll_roles.py`

**Interfaces:**
- Produces `SystemRole` members (values) `PAYROLL_WAGES = "payroll_wages"`, `PAYROLL_TAX_EXPENSE = "payroll_tax_expense"`, `PAYROLL_FEDERAL_PAYABLE = "payroll_federal_payable"`, `PAYROLL_GA_WH_PAYABLE = "payroll_ga_wh_payable"`, `PAYROLL_FUTA_PAYABLE = "payroll_futa_payable"`, `PAYROLL_SUTA_PAYABLE = "payroll_suta_payable"`, `PAYROLL_HEALTH_PAYABLE = "payroll_health_payable"`, `PAYROLL_HSA_PAYABLE = "payroll_hsa_payable"`; `PAYROLL_ROLES: tuple[SystemRole, ...]` (all eight) in `accounts.py`; `ROLE_TYPES` types per spec §4 (wages and tax expense `EXPENSE`, the rest `LIABILITY`).
- Consumes: the freeze machinery (`FROZEN_ROLES`, `bind_role`, `_owner_activity` pattern from M3).

- [ ] **Step 1:** Write tests: each role binds only to an account of its type; the default chart binds all eight on a fresh seed; `seed-chart` on a chart that already has the accounts but no roles binds them, and leaves an already-bound role alone; rebind of any payroll role is refused once any run is `approved`/`void` or any tax payment exists (message names the role), allowed before; first bind and no-op rebind allowed; a real two-session race of `bind_role` against a run approval's role lock (use a plain `locked_role_account` plus a run row insert as the issuer, to be re-pointed in Task 6); Settings > Ledger page lists all eight with labels (page test).
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement. Role strings stay <= 30 characters.
- [ ] **Step 4:** Run `uv run pytest tests/ledger tests/payroll_run tests/frontend`; expect PASS.
- [ ] **Step 5:** Commit `feat(payroll): payroll account roles`.

### Task 4: Payroll settings service

**Files:**
- Create: `payroll/payroll_run/settings_service.py`
- Test: `tests/payroll_run/test_settings_service.py`

**Interfaces:**
- Consumes: Task 2 models. Produces `PayrollSettingsService(session)` with `get_year(tax_year) -> PayrollSettings | None`, `require_year(tax_year) -> PayrollSettings` (raises `PayrollSettingsMissingError` naming Settings > Payroll), `set_year(*, actor, tax_year, futa_rate, futa_wage_base, suta_rate, suta_wage_base) -> PayrollSettings` (upsert, audited), `deposit_rules() -> PayrollDepositRules`, `set_deposit_rules(*, actor, **fields) -> PayrollDepositRules`, `rates(tax_year) -> UnemploymentRates`.

- [ ] **Step 1:** Write tests: defaults row for deposit rules exists (monthly day 15 for 941 and GA DOR; quarterly end-of-month for FUTA and GA DOL); upsert creates then updates one row per year; rates validated (0 <= rate <= 0.2 at 6 dp, base 0 < base <= 1,000,000, strict Decimal, no NaN/Infinity/1E+30); cadence enum and due-day 0..31 validated; owner-only (viewer and accountant refused); `require_year` error text; audit rows written.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS. **Step 5:** Commit `feat(payroll): unemployment rates and deposit rule settings`.

### Task 5: Run service: drafts, FUTA/SUTA, delete rule

**Files:**
- Modify: `payroll/payroll_run/services.py`, `payroll/payroll_run/schemas.py` (add `status`, `pay_date`, `entry_id`, `futa`, `suta`, `futa_wages`, `suta_wages` to the run result and summary), `payroll/payroll_run/routers.py` (delete refusal), the OpenAPI schema test
- Test: `tests/payroll_run/test_service.py` (update), `tests/payroll_run/test_drafts.py`, `tests/payroll_run/test_routers.py` (update)

**Interfaces:**
- Consumes: `calc_unemployment`, `PayrollSettingsService.rates`, Task 2 columns. Behaviour: `calculate(payload, *, actor)` with `save_run` creates a run with `status='draft'`, computes FUTA/SUTA from `ytd_fica_before` and `ytd_fica_after` and the year's settings (missing settings with `save_run` -> `PayrollSettingsMissingError`; an unsaved calculation still works without settings and reports FUTA/SUTA only when settings exist), stores `suta_rate` used; one non-void run per period (existing refusal text kept); `_get_previous_ytd_fica` considers only runs in states `approved`, `imported`, `draft` of the same year (void runs excluded); `delete_run` allowed only for `draft` (refusal `PayrollRunStateError` naming the state); results include the new fields; QuickBooks `_build_journal_entries` is unchanged.

- [ ] **Step 1:** Write tests: save creates a draft with FUTA/SUTA equal to `calc_unemployment` on the stored YTD values; settings missing -> 422 message; a saved draft's non-new figures equal a pre-change golden snapshot (compute the snapshot values from the engine for fixed inputs and assert equality); an `imported` row (insert with status imported) can't be deleted (409) while a draft can, in the service, the JSON API (status code and error type) and via `DELETE /htmx/runs/{id}` later in Task 9; the YTD chain skips void runs and includes imported ones; second saved run for a period refused unless the first is void; unsaved calculation unchanged for any role; the OpenAPI snapshot test updated for the added fields; the QuickBooks entries for a saved run are identical before and after (compare against `_build_journal_entries` output from main's fixture values).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement (update the existing tests that assumed any saved run is deletable). **Step 4:** Run `uv run pytest tests/payroll_run tests/tax`; expect PASS. **Step 5:** Commit `feat(payroll): saved runs are drafts with FUTA and SUTA`.

### Task 6: Approve and void posting

**Files:**
- Create: `payroll/payroll_run/posting.py`
- Test: `tests/payroll_run/test_posting.py`, `tests/payroll_run/test_posting_concurrency.py`

**Interfaces:**
- Produces `PayrollPostingService(session)`: `approve(run_id, *, actor, pay_date, net_pay_source: Literal["account","owner"], net_pay_account_id: int | None) -> PayrollRun`, `void(run_id, *, actor, void_date, memo=None) -> PayrollRun`, `preview(run_id) -> list[PreviewLine]` (the section 6 lines, no writes; `PreviewLine(account_label, debit, credit, memo)`), `build_lines(run, accounts) -> list[LineDraft]` pure and public for the property test.
- Posting per spec §6 table: source `PAYROLL_RUN`, source id = run id, memos copied from the existing builder; zero lines dropped; the service asserts debits == credits and raises `InvalidPayrollRunError` (nothing written) otherwise; missing role -> `PayrollRoleNotBoundError` naming the role; the net-pay account must be an active asset not in `RESERVED_ROLES` (or Due to Owner when `net_pay_source='owner'`); `pay_date` on or after the first day of the pay period (the month start) and after the lock; chain rule (every earlier run in the tax year `approved`/`imported`/`void`, none `draft`); `void` requires status `approved`, void date rules, and no later approved run in the tax year; the void flushes `status='void'` with its void fields and the reversal link.

- [ ] **Step 1:** Write tests: the entry for the Review Focus 4 matrix (health in/out of income tax x HSA zero/non-zero x net pay bank/owner) asserted line by line against stored figures with the exact accounts; drop of zero lines; balanced assertion refusal using a corrupted stored figure (set via raw SQL on a draft before approve); missing role named in the error and nothing written; pay-date rules; chain rule with an earlier draft refused, earlier imported and void allowed; void out of order refused with the later run named; void then re-save and re-approve the same period (Review Focus 5); second approve and second void refused; imported and void runs cannot be approved; viewer and accountant refused for approve, void; zero rows or entries left after every rejection. Concurrency (real two-session, lock-wait probe like `tests/expenses/test_expense_concurrency.py`, not sleeps): two approvals of consecutive months where the earlier is still a draft (exactly one outcome that respects order; mutation-check by removing the tax-year row lock and show it fails), approve vs void of the previous month, approve vs `bind_role` of a payroll role (re-point the Task 3 race test at the real service), approve vs lock-date change.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement following `payroll/expenses/reimbursements.py` (role locks, row locks, `commit_or_rollback`, audit snapshot, readable void memo "Void of payroll run 2026-03"). **Step 4:** Run `uv run pytest tests/payroll_run tests/ledger`; expect PASS. **Step 5:** Commit `feat(payroll): approve and void payroll runs`.

### Task 7: Tax payments

**Files:**
- Create: `payroll/payroll_run/tax_payments.py`
- Test: `tests/payroll_run/test_tax_payments.py`, `tests/payroll_run/test_tax_payment_concurrency.py`

**Interfaces:**
- Produces `AGENCY_ROLES: dict[str, SystemRole]` (`irs_941`, `irs_futa`, `ga_dor`, `ga_dol` per spec §7) and `TaxPaymentService(session)` with `create(*, actor, payment_date, agency, bank_account_id, amount, memo=None) -> TaxPayment`, `void(id, *, actor, void_date, memo=None) -> TaxPayment`, `get(id)`, `list(agency=None, limit=50, offset=0)`.
- Posting: Dr the agency role account, Cr bank (asset, active, not in `RESERVED_ROLES`); `SourceType.TAX_PAYMENT`; source id = payment id; same validation order, error classes and lock behavior as `payroll/expenses/transfers.py`.

- [ ] **Step 1:** Write tests mirroring `tests/expenses/test_transfers.py`: the four agency-to-role mappings post to the right accounts; unbound role named; overpayment accepted (payable account goes to a debit balance); bad amounts (0.00, 3 decimals, 1E+30, NaN, bool, None), bad agency, bound-role bank account, revenue account, inactive account, lock date, unsupported date, viewer and accountant refused for create and void, void date before the payment, second void refused, zero rows/entries left after rejections; two racing voids post exactly one reversal (lock-wait probe).
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS. **Step 5:** Commit `feat(payroll): tax payments to agencies`.

### Task 8: Liability report

**Files:**
- Create: `payroll/payroll_run/liability.py`
- Test: `tests/payroll_run/test_liability.py`

**Interfaces:**
- Produces frozen dataclasses `PeriodLiability(pay_period, due_date, liability, paid, unpaid, overdue)`, `AgencyLiability(agency, label, balance, periods, other, next_due)`, `LiabilityReport(as_of, agencies)` and `payroll_liability_report(session, as_of: date) -> LiabilityReport`.
- Per spec §8: balance = ledger credit minus debit on the role account at `as_of`; periods from `approved` runs dated `<= as_of` (federal liability = withholding + employee SS + employee Medicare + employer SS + employer Medicare; GA DOR = Georgia withholding; FUTA; SUTA); posted tax payments for the agency dated `<= as_of` applied oldest period first; `other = balance - sum(unpaid)`; due date from `PayrollSettingsService.deposit_rules()` (monthly: the due day of the next month, 0 = month end; quarterly: due day in the month after the quarter end), overdue is `due < as_of`; an unbound role gives the agency with `balance = 0.00` and no periods.

- [ ] **Step 1:** Write tests through the real services (Review Focus 6): clean data gives `other == 0.00` and balance equals unpaid sum; a partial payment ages oldest first across two months; an overpayment shows a negative `other`; a manual journal crediting the payable shows `other`; voided runs and imported runs create no period; as-of in the past ignores later runs and payments; due-date rules including December runs due in January, quarter ends, month-end day 0, day 31 in a 30-day month clamps; overdue flag; four agencies independent.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS. **Step 5:** Commit `feat(payroll): payroll liability report`.

### Task 9: Run pages: approve, void, preview, history

**Files:**
- Modify: `payroll/frontend/routers.py`, templates `partials/_results.html` and `partials/_history.html`, `payroll/frontend/static/js/` as needed, `tests/auth/test_route_protection.py`
- Test: `tests/frontend/test_payroll_run_pages.py`

**Interfaces:**
- Routes (owner-only POST with CSRF; GET any signed-in role): `POST /htmx/runs/{id}/approve` (form: `pay_date`, `net_pay_source`, `net_pay_account_id`), `POST /htmx/runs/{id}/void` (form: `void_date`, `memo`); `DELETE /htmx/runs/{id}` refuses non-drafts with the service message as a 409 partial. The run panel (`htmx_get_run`) shows a status badge, for drafts the ledger preview (from `PayrollPostingService.preview`) and an Approve form (pay date defaulting to the period's last day, net pay from bank or Due to Owner, bank picker of active asset accounts that are not reserved roles), for approved runs a link to the ledger entry and a Void form; history rows show status. The QuickBooks entries panel and CSV export stay.
- Parsing: ids digits 1..2^31-1 via a shared `_path_id`-style parser (reuse `payroll/frontend/expense_routers.py` helpers by importing them from a neutral module if they are not already shared: move them to `payroll/frontend/parsing.py` first in this task, behaviour unchanged, `expense_routers.py` updated); dates strict `datetime.date`; enums validated; errors are 422 partials with typed values kept.

- [ ] **Step 1:** Write page tests: draft panel shows the preview lines and the Approve form; approve posts and the panel flips to approved with a working ledger-entry link; void flips back and a new draft for the period can then be saved; delete of an approved run is a 409 partial; viewer and accountant see status and preview but no forms and get 403 on POSTs; garbage ids, dates, enums and amounts are 422 not 500 on both new routes (parametrized); XSS payload (`<img src=x onerror=alert(1)>`) in run notes renders escaped; the new POST routes are in the sweep with CSRF cases; the existing calculator page tests stay green.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest tests/frontend tests/auth tests/payroll_run`; PASS. **Step 5:** Commit `feat(payroll): approve and void runs from the payroll page`.

### Task 10: Tax payment and settings pages

**Files:**
- Create: `payroll/frontend/payroll_ledger_routers.py`, templates `payroll/tax_payments/{list,form,detail}.html`, `settings/payroll.html`
- Modify: `payroll/app.py` (include router), `payroll/frontend/templates/base.html` and the payroll tabs partial (nav), `tests/auth/test_route_protection.py`
- Test: `tests/frontend/test_tax_payment_pages.py`, `tests/frontend/test_payroll_settings_pages.py`

**Interfaces:**
- Routes (GET any signed-in role; POST owner-only + CSRF): `GET /payroll/tax-payments` (paged 50), `GET/POST /payroll/tax-payments/new`, `GET /payroll/tax-payments/{id}` (entry link, void form), `POST /payroll/tax-payments/{id}/void`; `GET /settings/payroll`, `POST /settings/payroll/year` (tax year, FUTA/SUTA rates and wage bases), `POST /settings/payroll/deposit-rules`. Same parsing rules as Task 9; amounts via `parse_amount`.

- [ ] **Step 1:** Write page tests: tax payment create/void/list with validation errors as 422 pages (bad agency, bound-role bank, 0.00, 3 decimals, 1E+30, bad dates, garbage ids); overpayment accepted and shown; settings pages save the year and the rules and show saved values, bad rates and cadences are 422; viewers read and get 403 on POST; sweeps and the route-list-matches-app test updated; escaping test on memos.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest tests/frontend tests/auth`; PASS. **Step 5:** Commit `feat(payroll): tax payment and payroll settings pages`.

### Task 11: Liability report page, exports, drill-down

**Files:**
- Modify: `payroll/frontend/report_routers.py`, `payroll/reports/runs.py` (`PAGE_RUNNERS`), `payroll/reports/export.py`, `payroll/frontend/report_export.py`, templates `reports/payroll_liability.html` and `reports/print.html`, `payroll/frontend/source_links.py` and the `document_label` helper (link `payroll_run` entries to the payroll page `/` with `?run={id}` if the page supports it, else to `/`; link `tax_payment` entries to `/payroll/tax-payments/{id}`), the reports index template
- Test: `tests/payroll_run/test_liability_report_pages.py`, `tests/reports/test_payroll_drilldown.py`

**Interfaces:**
- Consumes: Task 8 `payroll_liability_report`. Routes `GET /reports/payroll-liability` (+ `.csv`, `.pdf`), any signed-in role, strict `as_of` (default today), 422 page on bad input; no API twin (as the Owner report). CSV rows per agency and period plus the `other` line, every text cell through `csv_safe`; PDF through the existing export layer with a Pango-gated test that asserts real PDF bytes.

- [ ] **Step 1:** Write tests: figures equal the service; CSV neutralisation; escaping; bad `as_of` -> 422 on page, `.csv`, `.pdf`; viewers and accountants can GET; anonymous refused; each source type's anchor (class `source-link`, exact href) appears next to the entry in the GL, journal report, registers, journal list and entry view with a mutation check (breaking the branch fails the tests), legacy non-numeric ids render without a 500; the heavy-route count test updated for the new sync `.csv`/`.pdf` routes.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run `uv run pytest tests/reports tests/payroll_run tests/frontend tests/auth`; PASS. **Step 5:** Commit `feat(reports): payroll liability report and payroll drill-down`.

### Task 12: Properties and demo data

**Files:**
- Create: `tests/payroll_run/test_properties.py`
- Modify: `payroll/ledger/cli.py` (`seed-demo`)

**Interfaces:** seed-demo adds a year's payroll through the real services: settings for the demo year, twelve runs saved and approved in order (one with health in income tax and HSA, others plain; net pay from bank, one to Due to Owner), tax payments for several months, one voided and re-approved month; idempotent like the M2/M3 parts (skips with a notice when runs exist).

- [ ] **Step 1:** Write Hypothesis tests (12-25 examples, deadline off, DB reset with a TRUNCATE list covering the new tables): (1) for any generated run inputs the posted entry balances, every line equals the stored figures and the posted net pay equals `net_pay`; (2) `calc_unemployment` summed over a year of generated monthly wages never exceeds the base times the rate and equals the one-shot figure for the year's total (up to cent rounding per run, assert the exact rule you implement); (3) a random sequence of save, approve, void, delete-draft, tax payment, void payment operations never violates: trial balance balanced; each payable role balance equals the unpaid periods plus `other` with `other == 0.00` when no manual journals; no run approved while an earlier run of its year is a draft; no void succeeds while a later run of the year is approved; refused operations leave a ledger snapshot unchanged; only expected service errors are tolerated per operation (narrow per-kind tuples, as `tests/expenses/test_properties.py`); a deterministic test proves each operation kind succeeds at least once.
- [ ] **Step 2:** Run; FAIL where seed is missing. **Step 3:** Implement the seed. **Step 4:** Run the new tests plus `seed-demo` twice on a scratch DB (second run changes nothing) and assert the demo books reconcile (liability `other == 0.00`, trial balance balanced, A/R aging and owner summary still tie).
- [ ] **Step 5:** Commit `test(payroll): properties; feat(ledger): demo payroll`.

### Task 13: Docs, version, release checks

**Files:**
- Modify: `README.md`, `docs/RUNBOOK.md` ("Upgrading to v2.4.0" per spec §10, including that existing runs become imported and cannot be deleted, entering FUTA/SUTA rates before the next save, confirming deposit rules, and the downgrade refusal), `docs/SPEC.md` (§4.5 and §4.6 rulings numbered, M4 marked Done (v2.4.0) in §8), `docs/DESIGN.md` (payroll tables, roles, the posting table), version `2.4.0` in `pyproject.toml`, `payroll/__init__.py`, `.env.example`, `uv.lock` (`uv lock`; only the project's version line changes)
- Test: the version-consistency test already in the suite

- [ ] **Step 1:** Update docs and versions. State the role name `payroll_ga_wh_payable`, that the QuickBooks account names in `PayrollConfig` stay until M7, and the M6 follow-up about approved runs made during the parallel run. Read every claim against the code before writing it.
- [ ] **Step 2:** Run `uv run ruff check . && uv run ruff format --check . && uv run pytest`; expect all green and `git diff --stat` showing only docs, config and version files.
- [ ] **Step 3:** Commit `docs: M4 payroll to ledger, release 2.4.0`.
- The controller then runs the whole-branch review, the Docker check, the browser check and the ProjectOS updates.
