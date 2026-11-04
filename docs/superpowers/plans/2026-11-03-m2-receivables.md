# DCI Books M2 — Receivables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add customers, items, invoices, customer payments and deposits, customer credits and refunds, and the A/R Aging report, and make the cash-basis Profit & Loss and Balance Sheet real, so the owner can bill and collect in the app and see correct cash and accrual books.

**Architecture:** New `payroll/receivables/` package (models, one service per document type, derived-state and aging queries, invoice PDF). Every document posts exactly one journal entry through the M1 `LedgerPostingService`; the ledger stays the only source of truth for balances. Subledger tables hold the document facts the ledger cannot (customer, invoice lines, which payment paid which invoice). Cash basis is a second **row source** for the report queries: the ledger's own lines minus invoice entries, plus one stored "application" row pair per payment-to-invoice application recognized on the date the cash and the invoice both exist. Pages are Jinja2 + HTMX behind the M0 auth; no new JSON API in M2.

**Tech Stack:** Python 3.14, FastAPI, SQLModel, Alembic, Postgres 17, Jinja2 + HTMX, WeasyPrint (already present), `hypothesis` (dev, already present), pytest, ruff. No new dependencies.

**Spec:** [docs/SPEC.md](../../SPEC.md) (§3.1 F5–F7, §3.2 S6 partly, §4.3, §4.7, §8 M2) and [docs/DESIGN.md](../../DESIGN.md) (§4.2, §5, §7). Executors read both; where this plan and DESIGN §7 disagree about cash basis, **this plan wins** and Task 12 corrects DESIGN. Base: `master` (M0, M1, QBO chart import already merged). Version becomes `2.2.0`.

## Decisions to confirm before building

These are rulings I made where the spec is silent or too thin. Each says what it costs if you disagree. Change any of them before execution and the plan follows.

1. **Cash-basis method.** An invoice entry never appears on cash basis. A payment's cash entry is untouched (cash in, the credit sits in A/R as customer credit). Each *application* of a payment to an invoice adds, on its **recognition date** (the later of the payment date and the invoice date), a debit to A/R and credits to the invoice's income accounts split in proportion to the invoice lines (rounded to the cent, last account takes the remainder). Unapplied money stays as a credit balance in A/R on cash basis (it is not income). *If wrong:* the Task 9 row source changes; documents and ledger are unaffected.
2. **Applications are append-only and refused in a locked period.** An application whose recognition date is on or before the lock date is refused (it would change a closed period on cash basis). Misapplied money is fixed by voiding the payment and re-recording it.
3. **No JSON API in M2** (pages only). Importers in M6 call the services directly. *If wrong:* about one task of routers and schemas to add.
4. **Invoice rules.** Integer numbers assigned when an invoice is issued, from a settable "next number" (must be above every number used; voided numbers are never reused, so gaps appear after a void). Draft → Open → Void stored; Partial, Paid and Overdue are derived. An open invoice can be edited only while nothing is applied to it (edit = reverse the old entry and post the new one on the same date, same number). Void needs no applied payments and takes a date (default today; after the lock).
5. **Terms are net days only** (due date = invoice date + days; no discounts, late fees, credit memos or sales tax: all out of scope in SPEC §3.4).
6. **Refunds of unapplied credit** use a new journal source type `refund` (no migration needed; stored as text).
7. **A/R role is frozen** once an invoice has been issued (rebinding it would strand the cash-basis rows). The Undeposited Funds role can still move.
8. **Reports in M2:** A/R Aging with an open-invoices detail. Customer Statement and attachments stay for later (SPEC S4, S6).
9. **Owner-only mutations; accountants and viewers read everything** (same as M1).

## Global Constraints

- Everything in the M0 and M1 plans' Global Constraints still holds: Decimal money, `NUMERIC(14,2)`, Postgres only, `ruff` clean, role checks in routes **and** services, CSRF, audit rows in the same transaction, coverage floor 96, one commit per task.
- Floats never appear in receivables code. Money input with more than two decimal places is **rejected**, never rounded; quantities have at most three decimal places. Allowed money magnitude: `0.01` to `999999999999.99`. Line amounts are `round_money(qty × unit_price)` (half-up).
- Posted ledger rows stay immutable. Documents never edit an entry; they reverse (`LedgerPostingService.void`) and post a new one in the same transaction. Receivables services call the posting service and **commit through their own wrapper that rolls back on any error** (the M1 `JournalService._commit` pattern), so row locks never outlive a failure.
- Every document produces exactly one live journal entry. `source_type` is `invoice`, `payment`, `deposit` or `refund`; `source_id` is the document's integer id as text. Reversal entries copy both.
- Balances are never stored. Invoice paid amount, balance, status, customer credit and aging are computed.
- Dates are company-local; "today" is `company_today()`. Supported date range stays 1900-01-01..2200-12-31.
- Pages: `/customers`, `/items`, `/invoices`, `/payments`, `/deposits`, `/reports/ar-aging`, `/settings/receivables`. CSP stays `self`-only; page scripts live in `payroll/frontend/static/js/`.
- Text from users (names, addresses, descriptions, memos) is escaped in pages, neutralized in CSV, and never fetched or interpreted in PDFs.
- Implementers never edit the ProjectOS note; the controller updates it at the end (Task 12 lists what goes in).

## Review Focus

Failure modes the spec implies but no feature task tests on its own, most likely first:

1. **Over-application and races:** two payments (or two requests) applied to the same invoice at once cannot exceed its total; a payment cannot be applied beyond its amount; zero, negative, three-decimal and huge amounts are rejected with 422, never 500. → Tasks 1, 5
2. **Order of undoing:** voiding an invoice that has a live payment applied, voiding a deposited payment, voiding a payment inside a locked period, voiding twice, editing a paid invoice. Each refuses with a clear 409/422 and changes nothing. → Tasks 4, 5, 6
3. **Cash-basis arithmetic:** a three-way split that does not divide evenly, partial then final payment, overpayment, a payment dated before its invoice, a payment voided after it was applied, an invoice voided before any payment, an application recognized in a locked period. The cash Balance Sheet balances and cash net income never differs from accrual by more than open receivables. → Task 9
4. **Aging edges:** `as of` before the invoice exists, due date equal to `as of`, the 30/31 and 90/91 bucket boundaries, a customer with only credit, a voided invoice and a voided payment, and a manual journal that touches A/R (reported as a difference, never raised). → Task 8
5. **Numbering:** concurrent issues give unique contiguous numbers; a failed issue burns none; the next-number setting cannot go to or below a used number; starting at 1043 works. → Tasks 2, 4
6. **Hostile text:** a customer named `<script>`, a line description `=HYPERLINK(...)`, a 5,000-character address, and a memo with newlines in pages, CSV and PDF. → Tasks 2, 3, 8, 11
7. **Authorization:** viewer and accountant can read every new page and report and cannot create, edit, issue, void, apply, deposit or refund; the route-enumeration test knows the new routes. → Task 12

## File Structure

```
payroll/
├── receivables/
│   ├── models.py       NEW  Customer, Item, Invoice, InvoiceLine, Payment, PaymentApplication,
│   │                        ApplicationIncome, Deposit, CustomerRefund, ReceivablesSettings
│   ├── exceptions.py   NEW  receivables errors (AppBaseError subclasses)
│   ├── settings.py     NEW  ReceivablesSettingsService (next invoice number, company details)
│   ├── customers.py    NEW  CustomerService
│   ├── items.py        NEW  ItemService
│   ├── invoices.py     NEW  InvoiceService (draft, issue, replace, void), line math
│   ├── payments.py     NEW  PaymentService (record, apply, void, oldest-first allocation)
│   ├── deposits.py     NEW  DepositService
│   ├── credits.py      NEW  customer_credits(), RefundService
│   ├── balances.py     NEW  invoice_states(), ar_aging()
│   └── invoice_pdf.py  NEW  render_invoice_pdf()
├── reports/
│   ├── basis.py        MODIFY  cash-basis row source (replaces the M1 identity hook)
│   ├── queries.py      MODIFY  line_selection reads the basis row source
│   ├── statements.py   MODIFY  basis-aware register; notes
│   └── runs.py, export.py  MODIFY  A/R Aging run, CSV, PDF
├── ledger/models.py    MODIFY  SourceType.REFUND
├── ledger/accounts.py  MODIFY  freeze the A/R role once invoices exist
├── frontend/
│   ├── receivable_routers.py  NEW  customers, items, invoices, payments, deposits, settings
│   ├── templates/{customers,items,invoices,payments,deposits}/...  NEW
│   ├── templates/settings/receivables.html, receivables/_tabs.html  NEW
│   ├── templates/reports/ar_aging.html  NEW
│   └── static/js/{invoice.js,deposit.js}  NEW
├── migrations/versions/c3d9f1a7e2b4_receivables.py  NEW   (down_revision a1c5e8d3b7f2)
tests/{receivables,reports,frontend}/   NEW / EXTEND
```

---

### Task 1: Receivables schema, models and database integrity

**Files:**
- Create: `payroll/receivables/__init__.py`, `models.py`, `exceptions.py`, `payroll/migrations/versions/c3d9f1a7e2b4_receivables.py`, `tests/receivables/__init__.py`, `tests/receivables/conftest.py`, `tests/receivables/test_schema.py`
- Modify: `payroll/ledger/models.py` (add `SourceType.REFUND = "refund"`), `payroll/migrations/alembic_runner.py` (`_HEAD_REVISION`), wherever the app registers table models for metadata (follow how `ledger.models` is imported)

**Interfaces:**
- Produces (`models.py`, all `AppBaseModel` tables, money via `money_field()`):
  - `Customer(name, email, phone, billing_address, terms_days=30, is_active=True)`
  - `Item(name, description, unit_price, income_account_id → account.id, is_active=True)`
  - `Invoice(number: int | None, customer_id, invoice_date, due_date, terms_days, status: "draft"|"open"|"void", memo, total, entry_id → journal_entry.id | None, void_date: date | None)`
  - `InvoiceLine(invoice_id, line_no, item_id, description, qty NUMERIC(12,3), unit_price, amount, income_account_id)` (income account is a snapshot taken at issue)
  - `Payment(customer_id, received_date, method, reference, memo, amount, deposit_account_id → account.id, entry_id, status: "posted"|"void", void_date, deposit_id → deposit.id | None)`
  - `PaymentApplication(payment_id, invoice_id, amount, recognized_date, ar_account_id → account.id)`
  - `ApplicationIncome(application_id, account_id → account.id, amount)` (the stored income split of an application)
  - `Deposit(deposit_date, bank_account_id, memo, total, entry_id, status: "posted"|"void", void_date)`
  - `CustomerRefund(customer_id, refund_date, method, reference, memo, amount, bank_account_id, entry_id, status, void_date)`
  - `ReceivablesSettings(id=1, next_invoice_no=1001, company_name, company_address, remit_to, invoice_footer)`
- Produces (`exceptions.py`, `AppBaseError` dataclasses like `ledger/exceptions.py`): `CustomerNotFoundError(404)`, `ItemNotFoundError(404)`, `InvoiceNotFoundError(404)`, `PaymentNotFoundError(404)`, `DepositNotFoundError(404)`, `RefundNotFoundError(404)`, `InvalidCustomerError(422)`, `CustomerConflictError(409)`, `InvalidItemError(422)`, `ItemConflictError(409)`, `InvalidInvoiceError(422)`, `InvoiceStateError(409)`, `InvoiceNumberError(409)`, `InvalidPaymentError(422)`, `OverApplicationError(409)`, `DepositError(409)`, `InvalidRefundError(422)`, `ARNotConfiguredError(422)`.

**Schema rules (migration + models):**
- Unique indexes: `lower(customer.name)`, `lower(item.name)`, `invoice.number` where not null. FKs `RESTRICT`. `(payment_id, invoice_id)` unique on applications; `(application_id, account_id)` unique on application income.
- CHECKs: `customer.terms_days BETWEEN 0 AND 365`; `item.unit_price >= 0`; `invoice.status IN ('draft','open','void')`; an `open` invoice has `number` and `entry_id`; `invoice_line.qty > 0`, `unit_price >= 0`, `amount >= 0`; `payment.amount > 0`, `payment.status IN ('posted','void')`; `payment_application.amount > 0`; `application_income.amount > 0`; `deposit.total > 0`; `customer_refund.amount > 0`; `receivables_settings.id = 1`. Migration inserts the settings row (`next_invoice_no = 1001`).
- Triggers: `payment_application` and `application_income` are **append-only** (reject UPDATE and DELETE); a **deferred constraint trigger** on `payment_application` rejects a commit when Σ applications of a payment exceeds `payment.amount`, or when Σ applications to an invoice from `posted` payments exceeds `invoice.total`.
- `downgrade()` drops everything it created (tables, functions, enum-free).

- [ ] **Step 1: Write failing tests** `test_schema.py` (names): `test_migration_upgrade_and_downgrade_roundtrip` (use the scratch-DB pattern of the M1 schema test); `test_models_match_migration` (no metadata drift); `test_settings_row_seeded_with_1001`; `test_duplicate_customer_name_case_insensitive_rejected`; `test_duplicate_item_name_rejected`; `test_invoice_number_unique_when_set_and_null_allowed_many_times`; `test_open_invoice_requires_number_and_entry`; `test_check_constraints` (qty 0, negative amounts, terms 366, payment amount 0, status 'paid'); `test_application_rows_cannot_be_updated_or_deleted`; `test_commit_rejected_when_payment_over_applied` and `test_commit_rejected_when_invoice_over_applied` (raw SQL inserts, commit raises); `test_application_to_invoice_ignores_void_payments_in_limit` (void payment's application does not count toward the invoice limit); `test_source_type_refund_numbers_independently` (post a refund-typed draft through `LedgerPostingService`: `entry_no` 1).
- [ ] **Step 2:** `conftest.py` fixtures: `chart` (default chart seeded, with A/R and Undeposited Funds roles bound; returns accounts by name), `customer`, `item` (income account Services Revenue), `bank` (Business Checking). Reuse `tests/ledger/conftest.py` helpers.
- [ ] **Step 3: Run** `uv run pytest tests/receivables -v` — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS; also the full `tests/ledger` and `tests/reports` still pass; ruff clean.
- [ ] **Step 6: Commit** `feat(receivables): schema, models and integrity triggers`

---

### Task 2: Receivables settings, customers and items

**Files:**
- Create: `payroll/receivables/settings.py`, `customers.py`, `items.py`, `payroll/frontend/receivable_routers.py`, `payroll/frontend/templates/receivables/_tabs.html`, `templates/customers/{list,form}.html`, `templates/items/{list,form}.html`, `templates/settings/receivables.html`, `tests/receivables/test_customers.py`, `test_items.py`, `test_settings.py`, `tests/frontend/test_receivable_pages.py`
- Modify: `payroll/app.py` (include the new router the way `ledger_routers` is included), `payroll/frontend/templates/base.html` (nav entry "Sales" → `/customers` for now; Task 3 repoints it to `/invoices`)

**Interfaces:**
- Produces (`customers.py`): `CustomerService(session)`: `get(id)`, `list(include_inactive=False)`, `create(*, actor, name, email=None, phone=None, billing_address=None, terms_days=30) -> Customer`, `update(id, *, actor, name, email, phone, billing_address, terms_days) -> Customer`, `set_active(id, active: bool, *, actor) -> Customer`. Mutations owner-only, audited (`customer.create/update/activate`), commit.
- Produces (`items.py`): `ItemService(session)`: `get`, `list(include_inactive=False)`, `create(*, actor, name, income_account_id, unit_price=Decimal("0"), description=None)`, `update(id, *, actor, ...)`, `set_active`. The income account must be active and of type `income` or `other_income`.
- Produces (`settings.py`): `ReceivablesSettingsService(session)`: `get() -> ReceivablesSettings`, `set_next_invoice_number(n: int, *, actor)`, `update_company(*, actor, company_name, company_address, remit_to, invoice_footer)`.
- Pages: `/customers` (list, show inactive toggle), `/customers/new`, `/customers/{id}/edit`, `/customers/{id}/active`; `/items` likewise; `/settings/receivables` (next invoice number, company details). `_tabs.html` renders Invoices | Payments | Deposits | Customers | Items tabs (links to pages added later are plain anchors).

**Rules:** names trimmed, 1–200 chars; email optional, must contain one `@` and no spaces, ≤ 200; phone ≤ 50; address ≤ 500; `terms_days` integer 0–365; item `unit_price` Decimal with ≤ 2 places, 0 to the max; description ≤ 500. `set_next_invoice_number` requires an integer `> max(invoice.number)` (or ≥ 1 when none) and ≤ 99,999,999, locks the settings row `FOR UPDATE`, audits `receivables.next_number`. Company text fields ≤ 500 each. Non-owners get the usual 403 from routes and `PermissionDeniedError` from services; pages hide controls for viewers/accountants.

- [ ] **Step 1: Write failing tests (names):** customers: `test_create_customer_defaults_terms_30`, `test_name_unique_case_insensitive_409`, `test_rejects_bad_email_and_terms_and_long_text`, `test_deactivate_hides_from_default_list`, `test_owner_only_and_audited`; items: `test_item_needs_active_income_type_account` (asset and expense accounts rejected, inactive rejected), `test_unit_price_rejects_three_decimals_negative_nan_huge`, `test_item_name_unique`; settings: `test_next_number_must_exceed_used_numbers` (insert an issued invoice 1042 directly: 1042 rejected, 1043 accepted), `test_next_number_rejects_zero_negative_huge`, `test_company_details_roundtrip`; pages: `test_owner_creates_customer_and_item_via_forms`, `test_form_errors_rerender_with_typed_values_422`, `test_xss_in_customer_name_is_escaped`, `test_viewer_sees_lists_but_no_controls_and_gets_403_on_post`, `test_settings_page_owner_only_for_posts`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** services, router, templates, nav. **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): customers, items and receivables settings`

---

### Task 3: Invoice drafts and invoice pages

**Files:**
- Create: `payroll/receivables/invoices.py`, `payroll/frontend/templates/invoices/{list,form,view}.html`, `payroll/frontend/static/js/invoice.js`, `tests/receivables/test_invoice_drafts.py`
- Modify: `payroll/frontend/receivable_routers.py` (invoice pages), `templates/base.html` (nav "Sales" → `/invoices`)

**Interfaces:**
- Produces (`invoices.py`):
  - `@dataclass(frozen=True) InvoiceLineInput(item_id: int, description: str, qty: Decimal, unit_price: Decimal)`
  - `def line_amount(qty: Decimal, unit_price: Decimal) -> Decimal` = `round_money(qty * unit_price)` (pure)
  - `class InvoiceService(session)`: `get(id)`, `lines(id) -> list[InvoiceLine]`, `list(status=None, customer_id=None, limit=100, offset=0) -> list[Invoice]`, `create_draft(*, actor, customer_id, invoice_date, lines, terms_days=None, memo=None) -> Invoice`, `update_draft(invoice_id, *, actor, customer_id, invoice_date, lines, terms_days=None, memo=None) -> Invoice`, `discard_draft(invoice_id, *, actor) -> Invoice` (status `void`, no entry, no number). Task 4 adds `issue`, `replace`, `void` to this class.
- `terms_days` defaults to the customer's; `due_date = invoice_date + terms_days days` (must stay in the supported range). Draft invoices have no number, no entry and never touch the ledger.
- Page `invoices/form.html` + `invoice.js`: add/remove line rows, picking an item fills description and unit price (item data in `data-` attributes), live line amounts and total in integer cents, "Save draft" always enabled, server re-validates everything (progressive enhancement, like `journal.js`). `/invoices` lists number (blank for drafts), customer, dates, total and the stored status; `/invoices/new`, `/invoices/{id}` (read-only view with lines), `/invoices/{id}/edit` (draft only), `/invoices/{id}/discard` (draft only).

**Rules:** 1–100 lines; `qty` > 0, ≤ 999999.999, ≤ 3 decimals; `unit_price` ≥ 0, ≤ 2 decimals; item active; description 1–500 (defaults to the item's); customer active; total = Σ line amounts, > 0 and ≤ max; `invoice_date` within the supported range. Editing a non-draft invoice raises `InvoiceStateError`.

- [ ] **Step 1: Write failing tests (names):** `test_line_amount_rounds_half_up` (`0.333 × 10.00 = 3.33`, `0.5 × 0.01 = 0.01`, `2.5 × 1.01 = 2.53`); `test_create_draft_computes_total_and_due_date` (terms from customer, override terms 45); `test_draft_has_no_number_entry_or_ledger_effect`; `test_rejects_zero_lines_over_100_lines_zero_qty_negative_price_three_decimal_price_four_decimal_qty_huge_total`; `test_rejects_inactive_customer_and_inactive_item`; `test_rejects_date_out_of_range_and_due_date_overflow` (2200-12-31 + 30 days); `test_update_draft_replaces_lines`; `test_discard_draft_sets_void_without_number`; `test_cannot_edit_or_discard_non_draft` (insert an open row directly); `test_owner_only_and_audited`; pages: `test_create_invoice_form_roundtrip`, `test_invalid_form_rerenders_422_with_typed_lines`, `test_line_description_xss_escaped`, `test_list_filters_by_status_and_customer`, `test_viewer_reads_but_cannot_post`, `test_invoice_js_served_and_referenced`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): invoice drafts and invoice pages`

---

### Task 4: Issue, replace and void invoices (posting)

**Files:**
- Modify: `payroll/receivables/invoices.py`, `payroll/ledger/accounts.py` (freeze A/R role), `payroll/frontend/receivable_routers.py`, `templates/invoices/view.html`
- Test: `tests/receivables/test_invoice_posting.py`, `tests/receivables/test_invoice_concurrency.py`, extend `tests/frontend/test_receivable_pages.py`

**Interfaces:**
- Consumes: `LedgerPostingService.post/void`, `EntryDraft`, `LineDraft`, `AccountService.account_by_role`, `PeriodLock` reads, `ReceivablesSettings`.
- Produces (`InvoiceService`): `issue(invoice_id, *, actor) -> Invoice`; `replace(invoice_id, *, actor, customer_id, invoice_date, lines, terms_days=None, memo=None) -> Invoice`; `void(invoice_id, *, actor, void_date: date, memo: str | None = None) -> Invoice`. All commit through a wrapper that rolls back on any error.
- Produces (`AccountService.bind_role`): binding `ACCOUNTS_RECEIVABLE` to a different account (or unbinding it) raises `InvalidAccountError("Accounts receivable cannot change after invoices have been issued")` once any invoice has `status = 'open'` or an entry. Rebinding other roles is unchanged.

**Posting rule (`issue`):** require the A/R role bound (else `ARNotConfiguredError`: "Bind Accounts receivable in Settings > Ledger first"). Lock the invoice row, check status `draft`; re-check customer and items active; snapshot each line's `income_account_id` from its item; take the next number from `ReceivablesSettings` (`FOR UPDATE`, then increment); post one entry dated `invoice_date`, `source_type=invoice`, `source_id=str(invoice.id)`, memo `Invoice #<n> <customer name>` cut to 500: line 1 debit A/R for the total, then one credit line per income account (amounts summed per account, ordered by account number then id) with the line descriptions joined in the line memo (cut to 500). Set `status=open`, `entry_id`, audit `invoice.issue`. A failed issue (locked period, inactive account) rolls back everything including the number.
**`replace`:** status `open`, **no live application** (live = payment `posted`); `LedgerPostingService.void(old_entry, entry_date=old_entry.entry_date)` then post the new entry on the new `invoice_date`; number unchanged; lines replaced; one transaction; audit `invoice.replace` with before/after totals. **`void`:** status `open`, no live application; reversal dated `void_date` via `void()`; `status=void`, `void_date` set; audit `invoice.void`. A draft is discarded (Task 3), not voided.

- [ ] **Step 1: Write failing tests (names):** `test_issue_posts_dr_ar_cr_income_and_assigns_1001`; `test_issue_two_lines_same_income_account_makes_one_credit_line`; `test_issue_lines_to_two_accounts_makes_two_credit_lines_summing_to_total`; `test_second_issue_gets_1002_and_numbers_follow_next_number_setting` (set 1043 first); `test_issue_requires_ar_role_bound`; `test_issue_in_locked_period_rejected_and_burns_no_number`; `test_issue_twice_rejected`; `test_replace_reverses_old_entry_and_posts_new_same_number` (old entry `void`, reversal dated the old date, one live entry, ledger A/R equals new total); `test_replace_with_new_date_moves_income`; `test_replace_refused_with_live_application` (insert application directly); `test_replace_allowed_when_only_a_voided_payment_was_applied`; `test_void_posts_reversal_on_given_date_and_marks_void`; `test_void_refused_with_live_application`; `test_void_in_locked_period_rejected` and `test_void_date_in_locked_period_rejected`; `test_void_twice_rejected`; `test_voided_number_is_not_reused`; `test_unbind_or_rebind_ar_role_refused_after_issue_but_allowed_before`; `test_owner_only_and_audited`. Concurrency (`test_invoice_concurrency.py`, real threads): `test_parallel_issues_get_unique_contiguous_numbers` (10 drafts, numbers 1001..1010), `test_failed_issues_leave_no_gaps` (mix of locked-date failures). Pages: `test_issue_button_and_void_form_on_detail`, `test_void_form_defaults_to_today`, `test_edit_link_hidden_for_paid_invoice`, `test_viewer_gets_403_on_issue_void_replace`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; full `tests/ledger` still passes; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): issue, replace and void invoices with ledger posting`

---

### Task 5: Customer payments and applications

**Files:**
- Create: `payroll/receivables/payments.py`, `payroll/frontend/templates/payments/{list,form,view}.html`, `tests/receivables/test_payments.py`, `tests/receivables/test_payment_concurrency.py`
- Modify: `payroll/frontend/receivable_routers.py`, `templates/invoices/view.html` (paid/balance panel and payment links)
- Test: extend `tests/frontend/test_receivable_pages.py`

**Interfaces:**
- Produces (`payments.py`): `@dataclass(frozen=True) ApplicationInput(invoice_id: int, amount: Decimal)`; `METHODS = ("check","ach","cash","card","other")`; `class PaymentService(session)`: `get(id)`, `list(customer_id=None, limit=100, offset=0)`, `record(*, actor, customer_id, received_date, amount, deposit_account_id, method, reference=None, memo=None, applications: Sequence[ApplicationInput] = ()) -> Payment`, `apply(payment_id, *, actor, applications) -> Payment`, `void(payment_id, *, actor, void_date: date, memo=None) -> Payment`; module functions `allocate_oldest_first(session, customer_id: int, amount: Decimal) -> list[ApplicationInput]` (open invoices by due date then number, each up to its live balance; leftover unapplied) and `unapplied(session, payment_id) -> Decimal` (amount − Σ applications; 0 for void payments).
- `deposit_account_id` must be an active asset account: either the Undeposited Funds role account or any other asset account (a bank; the payment is then "straight to bank").

**Posting rule (`record`):** one entry dated `received_date`, `source_type=payment`, `source_id=str(payment.id)`, memo `Payment <method> <reference> <customer>` cut to 500; **exactly two lines, in this order**: line 1 debit the deposit account for `amount`, line 2 credit A/R for `amount`. Applications are not posted to the ledger: money not applied simply stays as a credit on A/R.
**Application rules (`record`/`apply`, one transaction):** lock the payment and every target invoice `FOR UPDATE` in id order. Each invoice must be `open`, belong to the payment's customer, and have a live balance ≥ the amount; Σ applications ≤ the payment's unapplied amount; amounts are 2-decimal Decimals > 0; one row per (payment, invoice). `recognized_date = max(received_date, invoice.invoice_date)`; refuse with `PeriodLockedError` if that date is on or before the lock date. Store `ar_account_id` = the A/R role account, and an `ApplicationIncome` split: for each income account on the invoice, `round_money(amount × account_total / invoice_total)` for all accounts but the last (ordered by account number then id), which takes the remainder so the slices sum exactly to `amount`. Audit `payment.record` / `payment.apply`.
**`void`:** payment `posted`, not in a live deposit (else `DepositError("Void the deposit first")`); `LedgerPostingService.void(entry, entry_date=void_date)`; `status=void`, `void_date`; its applications stay (history) but no longer count; audit `payment.void`.
- Pages: `/payments` (list), `/payments/new?customer_id=` (step 1 pick customer by GET; step 2 form: date, amount, method, reference, deposit to (Undeposited Funds default), the customer's open invoices each with an optional amount box, and a checked "apply oldest first" box used when no amounts are typed), `/payments/{id}` (view, applications, unapplied credit, Void form with date), `/payments/{id}/apply` (apply credit to open invoices), `/payments/{id}/void`.

- [ ] **Step 1: Write failing tests (names):** `test_record_posts_dr_deposit_cr_ar_two_lines_in_order`; `test_record_to_undeposited_funds_and_straight_to_bank`; `test_full_payment_applies_and_marks_paid` (balance via live sums); `test_partial_payment_leaves_balance`; `test_split_payment_across_two_invoices`; `test_overpayment_leaves_unapplied_credit_in_ar` (ledger A/R shows the credit; `unapplied` = 200.00); `test_apply_unapplied_credit_to_later_invoice_posts_nothing` (entry count unchanged; `recognized_date` = invoice date when later); `test_application_income_split_sums_exactly` (invoice 100.00 in three equal income lines, payment 33.33 → slices 11.11 + 11.11 + 11.11; payment 0.01 → one slice 0.01 and no zero rows; 3-way uneven 100.00 split 33.34/33.33/33.33 with payment 99.99); `test_recognized_date_is_later_of_payment_and_invoice_date`; `test_application_in_locked_period_rejected_and_nothing_written`; `test_rejects_over_application_to_invoice_and_beyond_payment_amount`; `test_rejects_other_customers_invoice_draft_invoice_void_invoice`; `test_rejects_zero_negative_three_decimal_nan_huge_amounts`; `test_rejects_non_asset_and_inactive_deposit_account`; `test_void_payment_reverses_entry_releases_balance_and_keeps_applications`; `test_void_refused_when_deposited`; `test_void_in_locked_period_rejected`; `test_void_twice_rejected`; `test_allocate_oldest_first` (due-date order, leftover credit); `test_owner_only_and_audited`. Concurrency: `test_two_payments_racing_for_one_invoice_cannot_exceed_total` (threads; exactly one succeeds or both partial within the total; ledger and subledger agree), `test_same_payment_applied_twice_concurrently_cannot_exceed_amount`. Pages: `test_record_payment_form_oldest_first`, `test_typed_amounts_override_oldest_first`, `test_step_one_customer_picker_lists_open_invoices`, `test_void_form_defaults_to_today`, `test_xss_in_reference_and_memo_escaped`, `test_viewer_gets_403_on_record_apply_void`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): customer payments, applications and customer credit`

---

### Task 6: Bank deposits

**Files:**
- Create: `payroll/receivables/deposits.py`, `payroll/frontend/templates/deposits/{list,form,view}.html`, `payroll/frontend/static/js/deposit.js`, `tests/receivables/test_deposits.py`
- Modify: `payroll/frontend/receivable_routers.py`
- Test: extend `tests/frontend/test_receivable_pages.py`

**Interfaces:**
- Produces (`deposits.py`): `class DepositService(session)`: `get(id)`, `list(limit=100, offset=0)`, `available(session) -> list[Payment]` (posted payments received into the Undeposited Funds account and not in a live deposit, oldest first), `create(*, actor, deposit_date, bank_account_id, payment_ids: Sequence[int], memo=None) -> Deposit`, `void(deposit_id, *, actor, void_date: date, memo=None) -> Deposit`.
- Posting: entry dated `deposit_date`, `source_type=deposit`, `source_id=str(deposit.id)`: line 1 debit the bank for the total, line 2 credit Undeposited Funds for the total. Each selected payment gets `deposit_id`. Rules: 1–200 payments; each `posted`, deposited to the Undeposited Funds account, not already in a live deposit, `received_date <= deposit_date`; the bank must be an active asset account other than Undeposited Funds; total ≤ max. `void`: reversal dated `void_date` via `void()` (the lock covers the original date), `status=void`, `void_date`, payments' `deposit_id` cleared (they become available again). Payments recorded straight to a bank never appear.
- Pages: `/deposits` (list), `/deposits/new` (tick payments, pick bank and date; `deposit.js` keeps a live total in integer cents and disables Save at zero), `/deposits/{id}` (view, Void form).

- [ ] **Step 1: Write failing tests (names):** `test_create_deposit_moves_undeposited_to_bank` (entry lines; Undeposited Funds balance falls to zero; bank rises); `test_deposit_groups_several_payments_one_entry`; `test_available_excludes_bank_direct_void_and_already_deposited_payments`; `test_rejects_payment_dated_after_deposit_date`; `test_rejects_same_payment_in_two_deposits_even_concurrently`; `test_rejects_bank_equal_to_undeposited_funds_or_inactive_or_non_asset`; `test_rejects_empty_selection_and_over_200`; `test_void_deposit_reverses_and_frees_payments`; `test_void_deposit_in_locked_period_rejected`; `test_void_twice_rejected`; `test_owner_only_and_audited`; pages: `test_new_deposit_page_lists_only_available_payments`, `test_create_via_form_redirects_to_view`, `test_deposit_js_served`, `test_viewer_gets_403`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): bank deposits of undeposited customer payments`

---

### Task 7: Customer credit, refunds and the customer page

**Files:**
- Create: `payroll/receivables/credits.py`, `payroll/frontend/templates/customers/view.html`, `payroll/frontend/templates/payments/refund_form.html`, `tests/receivables/test_credits.py`
- Modify: `payroll/frontend/receivable_routers.py`, `templates/customers/list.html` (link to the customer page)
- Test: extend `tests/frontend/test_receivable_pages.py`

**Interfaces:**
- Produces (`credits.py`): `customer_credits(session, as_of: date | None = None, customer_id: int | None = None) -> dict[int, Decimal]` = for each customer, Σ amounts of payments that are live as of `as_of` (`received_date <= as_of` and not voided on or before `as_of`) − Σ their applications recognized on or before `as_of` − Σ refunds live as of `as_of`; customers with a zero balance are omitted; `as_of=None` means "now" (everything live). `class RefundService(session)`: `get`, `list`, `record(*, actor, customer_id, refund_date, amount, bank_account_id, method, reference=None, memo=None) -> CustomerRefund`, `void(refund_id, *, actor, void_date, memo=None)`.
- Posting (`record`): entry dated `refund_date`, `source_type=refund`, `source_id=str(refund.id)`: line 1 debit A/R, line 2 credit the bank. `amount` ≤ the customer's credit as of `refund_date` (re-checked under a lock on the customer's payments); bank is an active asset account; methods as for payments. `void`: reversal dated `void_date` via `void()`.
- Page `/customers/{id}`: customer details, open invoices with balances, payments with unapplied amounts and an "Apply credit" link, credit balance, refunds with a "Refund credit" form (`/customers/{id}/refund`), links to record a payment or create an invoice for them.

- [ ] **Step 1: Write failing tests (names):** `test_credit_is_overpayment_minus_applications_minus_refunds`; `test_credit_as_of_dates` (before receipt 0; after receipt full; after application reduced); `test_voided_payment_has_no_credit_but_counts_before_its_void_date`; `test_refund_posts_dr_ar_cr_bank_and_reduces_credit`; `test_refund_over_credit_rejected` and `test_refund_when_no_credit_rejected`; `test_refund_rejects_bad_amounts_and_bad_bank`; `test_concurrent_refunds_cannot_exceed_credit`; `test_void_refund_restores_credit`; `test_refund_in_locked_period_rejected`; `test_refund_entries_are_numbered_independently_of_payments`; `test_owner_only_and_audited`; pages: `test_customer_page_shows_credit_and_open_invoices`, `test_refund_form_roundtrip_and_422`, `test_viewer_reads_customer_page_but_cannot_refund`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): customer credit, refunds and customer page`

---

### Task 8: Derived invoice state and the A/R Aging report

**Files:**
- Create: `payroll/receivables/balances.py`, `payroll/frontend/templates/reports/ar_aging.html`, `tests/receivables/test_balances.py`, `tests/reports/test_ar_aging.py`
- Modify: `payroll/reports/runs.py` (`run_ar_aging`), `payroll/reports/export.py` (CSV for the aging report), `payroll/frontend/report_routers.py` (page, `.csv`, `.pdf`), `templates/reports/index.html` (link), `templates/invoices/list.html` and `view.html` (derived status, paid, balance), `payroll/receivables/invoices.py` only if a helper is needed

**Interfaces:**
- Produces (`balances.py`):
  - `@dataclass(frozen=True) InvoiceState(invoice: Invoice, paid: Decimal, balance: Decimal, status: Literal["draft","open","partial","paid","overdue","void"], days_overdue: int)`
  - `invoice_states(session, as_of: date | None = None, customer_id: int | None = None) -> list[InvoiceState]` — as of `as_of` (default `company_today()`): an invoice exists if issued with `invoice_date <= as_of` and not voided on or before `as_of`; `paid` = Σ applications of payments live as of `as_of` with `recognized_date <= as_of`; `status`: `void`/`draft` as stored; else `paid` when balance is 0; `overdue` when balance > 0 and `due_date < as_of`; `partial` when paid > 0 (and not overdue); otherwise `open`. `days_overdue = max(0, (as_of − due_date).days)`.
  - `@dataclass(frozen=True) AgingRow(customer_id, customer_name, current, d1_30, d31_60, d61_90, d91_plus, total, credit)`; `@dataclass(frozen=True) ARAging(as_of, rows, totals: AgingRow, invoices: list[InvoiceState], ledger_ar_balance: Decimal, difference: Decimal, invariants: list[Invariant])`; `ar_aging(session, as_of: date) -> ARAging`. Buckets by `days_overdue`: current = not past due (including due today), 1–30, 31–60, 61–90, 91+. `credit` = `customer_credits` as of. `ledger_ar_balance` = accrual (ledger as-is) balance of the A/R role account as of `as_of`; `difference` = (Σ balances − Σ credits) − ledger. Invariant `ar_subledger_matches_ledger` is `ok` when the difference is 0; a manual journal that touches A/R makes it `ok=False` with the amount in `detail` (reported, never raised).
- Page `/reports/ar-aging?asOf=YYYY-MM-DD` (default today): the customer table, the open-invoices table with days overdue (links to invoices), credits, the invariant banner; `.csv` and `.pdf` through the M1 `runs.py`/`export.py` pattern (CSV cells neutralized with `csv_safe`; PDF through `to_pdf`).
- Invoice list and view show the derived status, paid and balance; the list gains filters for Overdue, Partial, Paid.

- [ ] **Step 1: Write failing tests (names):** `test_status_open_partial_paid_overdue_void_draft`; `test_due_today_is_not_overdue_but_next_day_is`; `test_status_as_of_before_invoice_date_excludes_invoice`; `test_status_as_of_before_payment_date_shows_open`; `test_voided_invoice_excluded_after_void_date_and_included_before`; `test_voided_payment_stops_counting_on_its_void_date`; `test_buckets_boundaries` (30 days past due in 1–30, 31 in 31–60, 90 in 61–90, 91 in 91+); `test_customer_with_only_credit_shows_credit_and_zero_invoices`; `test_subledger_matches_ledger_for_invoices_payments_refunds_and_deposits`; `test_manual_journal_to_ar_reported_as_difference_not_raised`; `test_empty_books_aging_is_zero_and_ok`; `test_aging_as_of_in_the_past_matches_ledger_as_of_then`; report: `test_csv_neutralizes_formula_customer_name`, `test_pdf_endpoint_returns_pdf` (skip without Pango like M1), `test_page_renders_for_viewer`, `test_invalid_as_of_is_422_not_500`; list: `test_list_shows_derived_status_and_filters`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(receivables): derived invoice status and A/R aging report`

---

### Task 9: Cash-basis row source (Profit & Loss and Balance Sheet)

**Files:**
- Modify: `payroll/reports/basis.py`, `payroll/reports/queries.py`, `payroll/reports/statements.py` (only where callers must change), `payroll/reports/schemas.py` (note text)
- Test: `tests/reports/test_cash_basis.py`; keep every existing `tests/reports` test green

**Interfaces:**
- Replaces the M1 identity hook `recognized_lines(basis)` with a basis-specific **row source**: `line_selection(..., basis=...)` selects from a source whose rows have the shape of `journal_line ⋈ journal_entry` (line id, entry id, line no, account id, debit, credit, line memo, entry date, source type, entry no, source id, entry memo, status). For `Basis.ACCRUAL` (and the ledger-as-is reports, which keep `LEDGER_BASIS = ACCRUAL`) the source is the plain join, unchanged. For `Basis.CASH` the source is the union of:
  1. every ledger line whose entry has `source_type <> 'invoice'` (invoice entries, their reversals and replacements are excluded; reversal entries copy the original's `source_type`, so this removes them all), and
  2. **application rows**, one pair per `payment_application`: on `recognized_date`, a **debit** of `amount` to `ar_account_id` and a **credit** of each `application_income` slice to its income account; and, if the payment is `void`, the mirror pair (credit A/R, debit each slice) on the payment's `void_date`. Application rows carry the payment's `entry_id`, `source_type='payment'`, a null line id, and the memo `Invoice #<number> paid`.
  The implementer chooses the mechanism (a SQLAlchemy subquery/CTE that callers select columns from, or a `ClauseAdapter`), but all report aggregates (`account_totals`, `debit_credit_totals`, `earnings`, `opening_balances`) must read through it, and the row source must stay a single place.
- `BASIS_NOTE` becomes: "Cash basis recognizes income when an invoice is paid. Money received but not yet applied to an invoice stays in Accounts Receivable as a credit; accrual basis recognizes income when the invoice is issued." (text lives in one constant). Trial Balance, General Ledger, Journal and the plain register stay ledger-as-is (M1 ruling).

- [ ] **Step 1: Write failing tests (names; literal numbers):** invoice 1,000.00 on 2026-01-10 with income A 600.00 and B 400.00, paid in part 500.00 on 2026-02-05: `test_accrual_pnl_recognizes_invoice_in_january`; `test_cash_pnl_january_income_is_zero_and_february_is_500_split_300_200`; `test_cash_balance_sheet_has_no_receivable_after_full_payment`; `test_accrual_balance_sheet_shows_open_receivable_500`; `test_cash_pnl_final_payment_completes_income_to_1000`; `test_uneven_split_sums_to_payment` (100.00 over three accounts, payments 33.33 + 33.33 + 33.34: cash income per account equals invoice line total, no cent lost); `test_overpayment_is_not_income_on_cash_basis` (payment 1,200 on 1,000: income 1,000; A/R shows −200.00 on both bases); `test_payment_before_invoice_date_recognized_on_invoice_date` (cash balance sheet between the two dates shows the credit in A/R and no income); `test_payment_voided_after_application_reverses_cash_income_on_void_date` (income present before void date, gone after; accrual unaffected by the application); `test_invoice_voided_before_any_payment_has_no_cash_effect_and_zero_accrual_net`; `test_voided_invoice_replacement_counts_once_on_accrual`; `test_cash_balance_sheet_balances_with_prior_year_earnings` (payment in a later year than the invoice); `test_cash_and_accrual_net_income_differ_exactly_by_open_receivable_change` (property-style over a small fixed scenario); `test_trial_balance_and_ledger_reports_ignore_basis`; `test_cash_invariants_ok_and_basis_note_text`; `test_comparison_columns_work_on_cash_basis`; `test_manual_journal_to_income_is_cash_recognized_on_its_date` (no invoice: both bases equal).
- [ ] **Step 2: Run** — new tests FAIL. **Step 3: Implement** (inspect that reversal entries copy `source_type`; if not, identify invoice reversals through `reverses_entry_id`). **Step 4: Run** `uv run pytest tests/reports tests/receivables tests/ledger -q` — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(reports): cash basis recognizes income when invoices are paid`

---

### Task 10: Cash-basis drill-down and statement pages

**Files:**
- Modify: `payroll/reports/queries.py` (`period_lines`/opening balances can read a basis), `payroll/reports/statements.py` (register takes `basis`), `payroll/frontend/report_routers.py`, `payroll/frontend/ledger_routers.py` (register route), `templates/reports/*.html` and `templates/accounts/register.html`
- Test: `tests/reports/test_cash_drilldown.py`, extend `tests/frontend/test_report_pages.py`

**Interfaces:**
- The account register (`/accounts/{id}/register`, report JSON counterpart if any) accepts `basis=cash|accrual|ledger` (default `ledger`, today's behavior). P&L and Balance Sheet cell links now carry the report's basis. With `basis=cash` the register lists the cash row source for that account and date range: opening balance from the cash source, ordinary lines as before, application rows labeled `Invoice #<n> paid` linking to the payment page and invoice page. Register headers state the basis ("Cash basis view").
- Drill-down figure rule: the sum of the register rows for `(account, start, end)` on a basis equals the P&L cell for that account and period on the same basis.

- [ ] **Step 1: Write failing tests (names):** `test_register_cash_basis_lists_application_rows_for_income_account`; `test_register_cash_total_equals_pnl_cell` and `test_register_accrual_total_equals_pnl_cell`; `test_register_default_is_ledger_as_before`; `test_ar_register_cash_shows_credit_only`; `test_pnl_page_cells_link_with_basis`; `test_basis_toggle_keeps_period_and_compare`; `test_unknown_basis_is_422`; `test_register_opening_balance_cash_vs_accrual`; `test_application_row_links_resolve_to_payment_and_invoice` (200 for owner and viewer).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/reports tests/frontend -q` — PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(reports): cash-basis drill-down from statements to payments`

---

### Task 11: Invoice PDF

**Files:**
- Create: `payroll/receivables/invoice_pdf.py`, `payroll/frontend/templates/invoices/pdf.html`, `tests/receivables/test_invoice_pdf.py`
- Modify: `payroll/frontend/receivable_routers.py` (`/invoices/{id}.pdf`), `templates/invoices/view.html` (Download PDF)

**Interfaces:**
- Produces: `render_invoice_pdf(session, invoice_id: int) -> bytes` and the HTML builder `invoice_html(session, invoice_id) -> str` (tested without Pango). Only issued invoices (`open`, or `void` with a "VOID" mark); drafts → 409. Content: company name and address and remit-to and footer from `ReceivablesSettings`, logo (`static/logo-icon.png` embedded as a data URI, because the PDF fetcher refuses every URL), invoice number, dates, terms, bill-to (customer name and address), lines with qty, unit price, amount, total, paid, **balance due** (derived), memo. Uses `reports.export.to_pdf` and its refusing `url_fetcher`. Response `application/pdf`, `Content-Disposition: attachment; filename="Invoice-<number>.pdf"`, audit row `invoice.pdf` committed by the route. Text is escaped and wraps; very long customer text and 100 lines paginate.

- [ ] **Step 1: Write failing tests (names):** `test_html_contains_number_customer_lines_total_and_balance_due`; `test_html_escapes_markup_and_wraps_long_text`; `test_html_shows_paid_and_balance_after_partial_payment`; `test_void_invoice_html_is_marked_void`; `test_draft_invoice_pdf_is_409`; `test_pdf_endpoint_returns_pdf_bytes_and_filename` (skip without Pango, as in M1); `test_pdf_cannot_fetch_urls` (an `<img src="http://...">` in a description is not fetched; reuse the M1 blocked-fetch helper); `test_viewer_can_download_pdf_and_it_is_audited`; `test_missing_invoice_404`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; ruff clean. Check the PDF in Docker once (`docker build .`, render an invoice).
- [ ] **Step 5: Commit** `feat(receivables): send-ready invoice PDF`

---

### Task 12: Hardening, demo data, docs, release 2.2.0

**Files:**
- Modify: `tests/auth/test_route_protection.py` (new routes enumerated; raise the owner-only unsafe-endpoint minimum), `payroll/ledger/cli.py` (`seed-demo` adds receivables), `pyproject.toml` and `payroll/__init__.py` and `.env.example` (version `2.2.0`), `uv.lock` (via `uv lock`), `README.md`, `docs/RUNBOOK.md`, `docs/SPEC.md` (M2 done; §4.3 rulings), `docs/DESIGN.md` (§4.2 data model as built, §7 cash-basis method as built), `/Users/jason/Documents/ProjectOS/01 Projects/Payroll App.md`
- Create: `tests/receivables/test_properties.py`, extend `tests/ledger/test_cli.py`

**Interfaces:** `seed-demo` (still refuses when any entry exists) additionally creates 4 customers, 3 items, and over 2026 a mix of invoices (open, partial, paid, overdue, one voided), payments (to Undeposited Funds and straight to bank, one overpayment), one deposit and one refund, so every report and the aging page have something to show.

- [ ] **Step 1: Property tests (Hypothesis, real Postgres, small example counts):** `test_random_receivable_activity_keeps_trial_balance_balanced`; `test_ar_subledger_always_equals_ledger_for_random_activity` (random invoices, partial payments, voids of payments/invoices where allowed, deposits, refunds); `test_cash_balance_sheet_balances_for_random_activity`; `test_cash_income_total_equals_sum_of_applications_not_voided`; `test_accrual_income_total_equals_sum_of_live_invoices`; `test_voiding_everything_returns_every_report_to_zero`.
- [ ] **Step 2: Authorization sweep:** extend `test_route_protection.py` so every new page route (GET and POST) is enumerated; add `test_viewer_and_accountant_read_all_receivable_pages_but_cannot_mutate` (parametrized over every new unsafe route: 403 for both roles, 401/303 anonymous); `test_receivable_routes_need_csrf`.
- [ ] **Step 3: Demo and CLI tests:** `seed-demo` twice refuses the second time; after it, aging invariant ok, cash and accrual statements balance, invoice statuses cover open/partial/paid/overdue/void.
- [ ] **Step 4: Docs and version:** README (new pages, workflow: customers → items → invoices → payments → deposits, cash vs accrual meaning, A/R Aging); RUNBOOK ("Upgrading to v2.2.0": the migration adds tables, triggers and one settings row and changes no existing data; `pg_dump` first anyway; steps: stop Watchtower, backup, merge, pull, check `alembic_version` is `c3d9f1a7e2b4`, start Watchtower; first use: bind A/R and Undeposited Funds in Settings > Ledger, set the next invoice number to continue QBO's sequence in Settings > Receivables, enter company details); SPEC (M2 done; rulings from this plan's "Decisions" section recorded in §4.3); DESIGN §4.2 and §7 corrected to what was built (application rows, `recognized_date`, `void_date` columns, append-only applications, `customer_refund`); version `2.2.0`.
- [ ] **Step 5: Full verification:** `uv run ruff check . && uv run ruff format --check . && uv run pytest` (coverage ≥ 96, all passing); `docker compose -f compose.test.yml up -d db`; `docker build .` and render an invoice PDF in the image; start the app on a scratch database, run `seed-chart` then `seed-demo`, and walk Customers → Items → create and issue an invoice → record a payment → deposit → refund → A/R Aging → Profit & Loss on both bases → drill into an income account on cash basis → invoice PDF, with the browser tools, saving screenshots of the aging report and both P&Ls.
- [ ] **Step 6: ProjectOS:** add the decisions (the nine from this plan), an Agent Activity Log entry, Next Steps → M3 plan, status, `updated`, and a row in `07 Time Tracking/Time Log.md`.
- [ ] **Step 7: Commit** `feat: M2 receivables, release 2.2.0` then push the branch and open a PR against `master` (Auto-fix on).
