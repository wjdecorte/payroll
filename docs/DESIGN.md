# DCI Books — Technical Design

| | |
|---|---|
| **Status** | Draft v0.2 (updated for owner decisions, 2026-10-31) |
| **Date** | 2026-10-30 |
| **Implements** | [SPEC.md](SPEC.md) |

Conventions follow `AGENTS.md`: Python 3.14, `uv`, `ruff`, `pytest`, FastAPI +
SQLModel + Alembic, and the existing package layout (`models.py`, `schemas.py`,
`services.py`, `routers.py` per domain).

## 1. Approach and alternatives

| Option | Verdict |
|---|---|
| **A. Extend the existing app with a ledger core (recommended)** | Reuses the tax engine, Docker/Synology setup and UI; payroll posts straight into the ledger; full control over payroll-tax returns, which no off-the-shelf tool does for Georgia. |
| B. Adopt open source (GnuCash, Beancount/Fava, Akaunting, ERPNext) | Fine for books, but none do S-Corp owner payroll + GA/federal returns, and the owner wants one app. Not chosen; GnuCash remains a fallback for the books alone. |
| C. Keep QBO's cheaper tier and stop here | Doesn't meet the goal (cancel the subscription). |

**Decision:** Option A, as a *modular monolith*: one deployable, strict module
boundaries, a single posting gateway into the ledger.

## 2. Architecture

```
Browser (LAN/VPN) ──TLS──▶ Synology reverse proxy / Tailscale serve
                               │
                        ┌──────▼───────────────────────────────┐
                        │ app container (FastAPI, gunicorn)    │
                        │  web UI  (Jinja2 + HTMX)  /          │
                        │  JSON API                /api/v1     │
                        │   ── auth middleware, CSRF, audit ── │
                        │  modules: ledger · accounts ·        │
                        │   receivables · expenses · payroll · │
                        │   payroll_tax · reports · imports    │
                        └──────┬───────────────────────────────┘
                               │ (internal network only)
                        ┌──────▼──────┐        ┌──────────────┐
                        │ Postgres 17 │◀─ nightly encrypted ──▶ │ backup volume
                        └─────────────┘   pg_dump               │ + off-site copy
```

### 2.1 Merge the frontend into the app

Today a separate `frontend` container renders HTML and calls the API over HTTP
(`payroll/frontend/client.py`). With login that would mean authenticating twice
and forwarding sessions between services. **Proposal:** serve the HTML from the
same FastAPI app, calling services in-process, and keep the JSON API behind the
same session auth for scripting. One container, one auth layer, no internal
HTTP hop. This is a restructure the owner should approve explicitly (§12).

### 2.2 Module layout

```
payroll/                       (package name kept to avoid churn; can rename later)
├── auth/          users, sessions, MFA, roles, rate limit, deps (require_role)
├── audit/         append-only audit log writer + viewer
├── ledger/        accounts, journal entries, lines, posting service, period lock
├── receivables/   customers, items, invoices, payments, deposits
├── expenses/      expenses, owner-paid flow, attachments
├── payroll_run/   existing; adds run lifecycle and posting, FUTA/SUTA settings, tax payments,
│                  liability report (M4); employee still to come
├── tax/           existing pure engine; constants versioned per year
├── payroll_tax/   returns (941/GA-7M/DOL-4N/940/W-2); planned for M5 (liabilities and tax
│                  payments were built inside payroll_run in M4)
├── reports/       report queries, period presets, CSV/PDF export
├── imports/       QBO CSV importers (chart, customers, items, GL by date range,
│                  payroll history), opening balances, historical backfill
├── frontend/      templates + routers (in-process service calls)
└── common/        base model, money type, errors, middleware
```

**Dependency rule:** domain modules (`receivables`, `expenses`, `payroll_run`,
`payroll_tax`) depend on `ledger`; `ledger` depends on nothing but `common`.
`reports` reads the ledger only. No module writes ledger tables except through
`ledger.posting`.

## 3. Money and database

- **Postgres only** in production *and* tests (tests use a throwaway schema or
  `testcontainers`/a compose test DB). SQLite cannot hold exact decimals
  reliably and its `Numeric` coerces to float, which is not acceptable for a
  ledger. The existing `payroll.db`/`test_*.db` SQLite files go away.
- `Money = Numeric(14, 2)` / Python `Decimal`; rates `Numeric(9, 6)`.
  Pydantic fields use `Decimal`; JSON serializes as strings.
- Rounding: one helper, `round_money()` (half-up to cents), applied at defined
  points in the tax engine (per IRS/agency rule per line), never ad hoc.
- **Float→Decimal migration of the existing `payroll_run` and `payroll_config`
  tables** (columns are `Float` today) is an Alembic migration with a data
  conversion step (`round(x, 2)`); the engine constants become `Decimal`.
  A golden-file test (all existing 73 tests' expected values) guards behavior.
  This is a schema change on production data → needs explicit approval and a
  `pg_dump` taken first (§12).

## 4. Data model

All tables have `id` (UUID or bigint), `created_at`, `updated_at`; business
tables also have `created_by`. Ledger tables are append-only (void/reverse,
never UPDATE/DELETE on posted rows).

### 4.1 Ledger core

```
account
  id, number (unique), name, full_name (path), parent_id,
  type  (asset|liability|equity|income|cogs|expense|other_income|other_expense),
  detail_type, normal_balance (debit|credit), is_active, system_role (nullable, unique)

journal_entry
  id, entry_no (gap-free per company), entry_date, memo,
  source_type (manual|invoice|payment|deposit|expense|reimbursement|
               payroll_run|tax_payment|opening_balance|year_end_close),
  source_id, status (posted|void), reverses_entry_id (nullable),
  posted_at, posted_by

journal_line
  id, entry_id → journal_entry, line_no, account_id,
  debit  Numeric(14,2) default 0,  credit Numeric(14,2) default 0,
  memo, customer_id (nullable), payee (nullable), cleared_in_reconciliation_id (nullable)
  CHECK ((debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0))

period_lock           (single row)  lock_date, set_by, set_at
audit_log             (append-only) ts, user_id, action, entity, entity_id, before JSONB, after JSONB, ip
```

**Balance enforcement (defense in depth):**
1. `posting.post(entry)` validates ≥ 2 lines, per-line CHECK, Σdebit = Σcredit, lock date, account active.
2. A Postgres **deferred constraint trigger** on `journal_line` rejects a commit
   if any touched entry is unbalanced, so even a buggy script cannot corrupt
   the ledger.
3. A trigger blocks `UPDATE`/`DELETE` on lines of a `posted` entry.

**Edit semantic for documents:** editing an invoice/expense re-posts inside one
transaction: the prior entry is voided with a reversal and a new entry is
posted; the audit log keeps both. (Simpler and safer than in-place mutation.)

### 4.2 Receivables (built in M2, v2.2.0)

```
customer       id, name, email, phone, billing_address, terms_days, is_active
item           id, name, description, unit_price, income_account_id, is_active
invoice        id, number (integer, assigned on issue), customer_id, invoice_date,
               due_date, terms_days, status (draft|open|void), memo, total,
               entry_id, void_date
invoice_line   id, invoice_id, line_no, item_id, description, qty (3 dp),
               unit_price, amount, income_account_id (copied from the item)
payment        id, customer_id, received_date, method, reference, memo, amount,
               deposit_account_id (bank | Undeposited Funds), entry_id,
               status (posted|void), void_date, deposit_id
payment_application  id, payment_id, invoice_id, amount, recognized_date,
               ar_account_id
application_income   id, application_id, account_id, amount
deposit        id, deposit_date, bank_account_id, memo, total, entry_id,
               status (posted|void), void_date
customer_refund id, customer_id, refund_date, method, reference, memo, amount,
               bank_account_id, entry_id, status (posted|void), void_date
receivables_settings  one row: next_invoice_no, company_name, company_address,
               remit_to, invoice_footer
```

- Every document (invoice, payment, deposit, refund) owns exactly one live journal entry
  (`source_type` `invoice`, `payment`, `deposit`, `refund`; `source_id` the document id).
  Editing or voiding reverses the entry through `LedgerPostingService`; the document row
  never rewrites a posted line.
- A deposit moves the payments it lists from Undeposited Funds to a bank account
  (`payment.deposit_id`). A payment recorded straight to a bank account is never deposited.
- `payment_application` rows are **append-only** (database trigger). Each stores its
  `recognized_date` (the later of the payment date and the invoice date) and the A/R account
  it was made against; `application_income` stores its split across the invoice's income
  accounts. Slices are allocated **cumulatively** with a cap per account, so once an invoice is
  fully paid each of its accounts has received exactly its own income and no rounding drifts.
  Applications are refused on or before the period-lock date.
- The A/R system role is frozen once any invoice, payment or refund has been recorded, voided
  ones included (they all post to A/R, and rebinding would split the subledger from the ledger
  and strand the cash-basis rows); Undeposited Funds can still be rebound.
- Documents that are voided keep a `void_date` (the date of the reversing entry). A void date
  is never before the document's own date.

Derived at query time (never stored): invoice paid amount (applications whose payment is
posted), balance and display status (`partial`, `paid`, `overdue`); a customer's unapplied
credit = payments received − applications − refunds. Credit can never go negative on any past
date: refunds, applications and payment voids check the minimum over their event dates.

### 4.3 Expenses (built in M3, v2.3.0)

Migration `d4e0a2b8c6f1`. Money is `NUMERIC(14,2)`; enum-like columns are plain strings guarded by
CHECKs. Documents store their facts; the money movement is the linked journal entry posted
through `LedgerPostingService`.

```
vendor         id, name (unique case-insensitively), note, active
expense        id, date, vendor_id (nullable), payee_text (nullable), memo,
               source (account|owner), paid_from_account_id (null iff source = owner),
               total, status (posted|void), entry_id, void_entry_id, void_date,
               reimbursement_id (nullable, set once)
expense_line   id, expense_id, line_no, account_id, memo, amount   (unique expense_id+line_no)
transfer       id, date, from_account_id, to_account_id, amount, memo,
               status, entry_id, void_entry_id, void_date
reimbursement  id, date, paid_from_account_id, total, memo,
               status, entry_id, void_entry_id, void_date
attachment     id, owner_type ('expense'), owner_id, filename, content_type, sha256,
               stored_name, size, uploaded_at, uploaded_by   (append-only)
```

Integrity beyond the foreign keys is in the database. CHECK constraints: exactly one of
`vendor_id` / `payee_text`; a posted document carries no void fields and a void one carries all
of them, with the void date not before the document date; a reimbursement link only on a posted
owner-paid expense. Triggers: expense lines sum to the expense total and a reimbursement's
expenses to its total (deferred constraint triggers, checked at COMMIT); `expense.reimbursement_id`
is set-once (and only to a posted reimbursement); expense lines and attachments are append-only
(UPDATE and DELETE refused). Posting rules:

- Expense, account source: Dr each line's expense account / Cr the paid-from asset or liability
  account. Owner source: Cr the Due to Owner role account instead.
- Reimbursement: Dr Due to Owner / Cr an asset account, for the total of whole open owner-paid
  expenses. Transfer: Dr to-account / Cr from-account (asset or liability, not a bound role).
- Voids post a reversing entry dated the void date (not before the document, after the lock).

Locking order: payment/role rows first (Due to Owner FOR SHARE; `bind_role` takes it FOR UPDATE),
then document rows by id, then the period lock FOR SHARE, then the per-source entry counter.

### 4.4 Payroll (built in M4, v2.4.0)

Migration `e5f1b3c9d7a2`. The tax engine (`tax/engine.py`) is untouched; FUTA and SUTA come from a
separate pure function, `calc_unemployment` (`tax/unemployment.py`). There is still no employee table.

```
payroll_run     (existing columns) + status (draft|approved|void|imported), pay_date,
                net_pay_source (account|owner), net_pay_account_id, entry_id, void_entry_id,
                void_date, futa_wages, futa, suta_wages, suta, suta_rate
payroll_settings       one row per tax_year: futa_rate, futa_wage_base, suta_rate, suta_wage_base
payroll_deposit_rules  single row (id = 1): per agency (irs_941, ga_dor, futa, ga_dol) a cadence
                       (monthly|quarterly) and a due day (1..31, or 0 = last day of the month)
payroll_config  (existing) — QuickBooks account-name strings, kept for the QBO entries until M7
```

Integrity is in the database. CHECKs keep `status` and its fields consistent (an approved run has a pay
date, an entry and a net pay source; a void run also has its reversal entry and a void date not before the
pay date; draft and imported runs carry none of these; FUTA and SUTA figures are not negative). A partial
unique index allows one non-void run per `pay_period`. A trigger refuses any UPDATE of a non-draft run
except approved to void (status, void fields, `modify_date`) and any DELETE of a non-draft run. Existing
runs were set to `imported` by the migration. The downgrade refuses once any run is approved or void or
any tax payment exists.

New `SystemRole` values, frozen once any run is approved or void or any tax payment exists:
`payroll_wages` and `payroll_tax_expense` (expense), and `payroll_federal_payable`,
`payroll_ga_wh_payable`, `payroll_futa_payable`, `payroll_suta_payable`, `payroll_health_payable` and
`payroll_hsa_payable` (liability). The default chart binds them to Wages, Taxes, Federal Taxes
(941/943/944), GA Income Tax, FUTA Payable, GA SUTA Payable, S-Corp Health Insurance and Co. HSA;
`seed-chart` and the migration bind them on an existing install only when those accounts exist under
Payroll Expenses or Payroll Liabilities, are active and the role is unbound.

Locking order for approve and void: the eight role accounts FOR SHARE (id order) and the net pay
account FOR SHARE, then every run of the tax year FOR UPDATE in id order, then the period lock FOR
SHARE, then the per-source entry counter.

### 4.5 Payroll tax (built in M4, v2.4.0)

```
tax_payment     id, payment_date, agency (irs_941|irs_futa|ga_dor|ga_dol), bank_account_id,
                amount (> 0), memo, status (posted|void), entry_id, void_entry_id, void_date
```

A payment posts Dr the agency's payable account / Cr the bank (`payroll_federal_payable`,
`payroll_futa_payable`, `payroll_ga_wh_payable`, `payroll_suta_payable`). The liability per agency is
derived, not stored (`payroll_run/liability.py`): the ledger balance of the payable account, the
approved runs' liability by pay period with the payments applied oldest first, and an Other line for the
rest of the balance. Return snapshots (`return_snapshot`) are still planned for M5.

### 4.6 Auth

```
user         id, email (unique), display_name, password_hash (argon2id), role,
             totp_secret_enc, totp_enabled, failed_logins, locked_until, is_active
recovery_code  id, user_id, code_hash, used_at
session      id (random 256-bit, stored hashed), user_id, created_at, last_seen_at,
             expires_at, ip, user_agent
```

## 5. Posting rules

One `PostingService` per source type builds a `JournalEntryDraft` and calls
`ledger.post()`. System roles resolve to accounts at posting time.

| Source | Debit | Credit |
|---|---|---|
| Invoice | Accounts Receivable | Income (per line item's account) |
| Payment received → Undeposited Funds | Undeposited Funds | Accounts Receivable |
| Payment received → Bank direct | Bank | Accounts Receivable |
| Bank deposit (group of payments) | Bank | Undeposited Funds |
| Expense paid from bank/card | Expense account(s) | Bank / Credit card |
| Expense paid personally by owner | Expense account(s) | Due to Owner |
| Reimburse owner | Due to Owner | Bank |
| Payroll run (approved) | `payroll_wages`: gross + health addback (when health is in income tax) + HSA; `payroll_tax_expense`: employer Social Security and Medicare + FUTA + SUTA | `payroll_federal_payable`: federal withholding + employee and employer Social Security and Medicare; `payroll_ga_wh_payable`: Georgia withholding; `payroll_futa_payable`; `payroll_suta_payable`; `payroll_health_payable`: premium (when in income tax); `payroll_hsa_payable`: HSA; net pay to the chosen bank account or Due to Owner. Zero lines are dropped. |
| Payroll tax payment | The agency's payable account (one of four roles) | Bank |
| Opening balances | Each balance-sheet account | Each balance-sheet account; difference to Opening Balance Equity |
| Year-end close | Income accounts; Retained Earnings | Expense accounts; Retained Earnings |

The payroll row is built from the run's stored figures by `build_lines` in
`payroll_run/posting.py`; the service refuses to post (422, nothing written) if the lines do
not balance. The QuickBooks text-line builder in `payroll_run/services.py` and its account-name
config are kept as they were until M7.

## 6. Payroll and returns

### 6.1 Calculation
`tax/engine.py` stays a pure function (no I/O) — converted to `Decimal`.
YTD figures (SS wage base tracking) come from approved runs for the employee
instead of the manually typed `ytd_fica_before`, removing a manual-entry error.
In M4 (v2.4.0) the run's `use_previous_ytd_fica` option reads the latest earlier non-void run
of the same tax year (draft, approved or imported), and approval requires the stored start to match
that run's end (see the run rules in the RUNBOOK, "Upgrading to v2.4.0").

### 6.2 Versioned tax constants
`tax/constants_2026.py`, `constants_2027.py`, … selected by the run's tax year,
so old runs recompute identically. Each January: add a new file from the
Pub 15-T / SSA / Georgia DOR figures, run the golden tests.

### 6.3 Returns
`payroll_tax/returns/<form>.py` each expose
`build(period) -> ReturnWorksheet` (typed lines: label, box/line id, value,
formula text, contributing run IDs). Calculation reads approved payroll runs
and tax payments only. A "finalize" step stores a `return_snapshot` so a filed
figure never changes silently when data is later edited (edits after a
snapshot raise a warning on that period). Form definitions are kept as data
beside each builder, with the agency instruction year recorded for the yearly
verification checklist (SPEC §6).

## 7. Reports

A single `ReportEngine` runs SQL aggregates over `journal_line` joined to
`account`:

- **Period resolver:** maps presets (`this_month`, `last_quarter`, `ytd`,
  `trailing_12`, …) and `custom(from, to)` to `(start, end)` using the company
  fiscal year (calendar year).
- **P&L:** Σ(credit − debit) for income types minus expense types over the
  period; **Balance Sheet:** Σ up to the as-of date for asset/liability/equity,
  plus *current-year earnings* synthesized into equity so it balances before
  year-end close.
- **Cash basis (default):** `payroll.reports.basis.row_source` is the single place that
  decides which rows a basis reads. Accrual and every ledger-as-is report (trial balance,
  registers, journal) read the plain `journal_line` joined to `journal_entry`. Cash reads the
  same columns from every ledger line **except those of invoice entries** (reversals and
  replacements copy `source_type='invoice'`, so they go too), **plus per-application row
  pairs**: each `payment_application` adds, on its `recognized_date`, a debit of its amount to
  its A/R account and a credit of each `application_income` slice to that income account. When
  the payment is void, a mirror pair is added on the payment's `void_date`, dated
  the later of the void date and the application's `recognized_date`, so an application voided
  before it was recognized never reaches income. Each pair balances on its own date, so cash-basis debits equal credits on
  every date and the Balance Sheet balances. Money received but not applied stays in A/R as a
  credit; it is not income. **Cash is the default view** (owner's decision); accrual is the
  toggle. Drill-down on cash basis lists these rows (memo `Invoice #n paid`).
- **Owner reimbursement report (M3):** `/reports/owner-reimbursement` shows, as of a date, the
  Due to Owner balance, the open owner-paid expenses and the reimbursements in a range; CSV and
  PDF go through the same export layer (formula-neutralized). Expense, transfer and
  reimbursement entries in the GL, P&L and Balance Sheet detail link to their documents.
- **Comparison columns** run the same query twice and diff.
- **Drill-down:** report rows carry `(account_id, start, end)` links to the
  account register, whose rows link to `source_type/source_id` documents.
- **Export:** CSV via `csv`; PDF via print-optimized HTML + WeasyPrint (adds
  system libs to the Dockerfile; chosen over headless Chrome for size).
- Report totals are covered by invariant tests: A = L + E, TB balanced, and
  P&L net income = change in retained earnings after close.

### 7.1 Importers and split-quarter returns

- The QBO general-ledger importer accepts any date range and is idempotent
  (each imported row keeps a source key; re-running skips duplicates), so
  prior years can be added later without disturbing locked periods.
- Imported payroll runs are stored in `payroll_run` with `status=approved`,
  `origin=imported`, taking amounts straight from the QBO payroll reports, and
  linked to imported journal entries. Return builders read approved runs
  regardless of origin, so split-quarter 941/DOL-4N and the year's 940/W-2 need
  no special case. The worksheet flags imported runs for tie-out to QBO.
- The cutover opening balance is an entry dated the day before cutover, offset
  to Opening Balance Equity, replaced by real history when earlier years load.

## 8. Authentication and security design

### 8.1 Why build it in rather than use an external IdP

For one or two users on a LAN/VPN, a self-hosted identity provider
(Authelia/Authentik/Keycloak) adds two more services to run and secure for
little gain. Built-in auth with the pieces below is small, well understood, and
fully testable. Revisit if more users or SSO are ever needed; the `auth` module
isolates this behind `require_user()` / `require_role()` dependencies, so
swapping to OIDC forward-auth later changes one module.

### 8.2 Flow
1. `POST /login` (email + password). Constant-time verify (Argon2id via
   `argon2-cffi`); on failure increment counter, lock after 5 (15 min, doubling).
2. If TOTP is enabled (mandatory after first login), challenge for the 6-digit
   code (`pyotp`, 30 s window ±1) or a recovery code. First login forces TOTP
   enrollment with QR and 10 recovery codes (shown once, stored hashed).
3. On success: create a `session` row; set an opaque, random, `HttpOnly`,
   `Secure`, `SameSite=Lax` cookie. Only a hash of the ID is stored. Rotate the
   ID on login and privilege changes.
4. Every request: middleware loads the session, enforces idle (30 min) and
   absolute (12 h) expiry, updates `last_seen_at`, attaches `request.state.user`.
5. State-changing requests require a CSRF token (double-submit/synchronizer
   token validated in middleware; HTMX sends it via a header).
6. `POST /logout` deletes the session. Password change/MFA reset revokes all sessions.

### 8.3 Other controls
- **Authorization:** `require_role()` dependency on every router; service
  methods take an `actor` and check permissions too.
- **Headers:** CSP (self only), `X-Frame-Options: DENY`, `Referrer-Policy`,
  HSTS (behind the TLS proxy), `Cache-Control: no-store` on reports.
- **Field encryption:** SSN and bank numbers via AES-256-GCM
  (`cryptography`), key from `ENCRYPTION_KEY` (Docker secret); masked output,
  reveal action audited.
- **Audit:** login success/failure, MFA changes, report exports, SSN reveals,
  every ledger mutation.
- **Network:** DB on an internal Docker network only; app bound to the proxy.
  First-run bootstrap: `uv run python -m payroll.auth.create_owner` creates
  the initial owner (no default credentials, no open sign-up).
- **Later:** passkeys via `py_webauthn`, replacing password + TOTP.

## 9. Frontend

Server-rendered Jinja2 + HTMX (already in use), with a consistent layout:
sidebar (Dashboard, Sales, Expenses, Payroll, Reports, Accounting, Settings).
Forms: invoice editor with line items, payment application grid,
expense form with receipt upload, journal entry editor with live
debit/credit totals, report pages with period picker and drill-down.
Mobile width must work. Visual design reuses the existing DCI branding;
a dedicated design pass can use the `impeccable` skill at implementation time.

## 10. Deployment, backup, operations

- **Compose:** `api` + `db` services, on an internal network; DB port
  unpublished; secrets mounted. Receipts live in `ATTACHMENT_DIR` (`/data/attachments`): a named
  volume `attachments` in `compose.yml`, the bind mount
  `/volume1/docker/payroll/attachments` in `compose.synology.yml`. The Dockerfile creates the
  directory owned by `appuser` (uid 10001) with mode 0700; a Synology bind mount must be
  `chown`ed to 10001 on the host (RUNBOOK).
- **Attachments:** files are stored as `yyyy/mm/<random 32 hex>` and written to a temporary name,
  fsynced and renamed; the type is decided from magic numbers (PDF, PNG, JPEG, HEIC), 10 MB per
  file, 10 files and 50 MB per expense. Downloads need sign-in (any role) and send
  `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and
  `Cache-Control: private, no-store`; `?inline=1` is honored only for PNG and JPEG.
- **Upload limit:** `MAX_REQUEST_BODY_BYTES` (1 MiB) applies everywhere except the two upload
  routes (`POST /expenses/new`, `POST /expenses/{id}/receipts`), which allow a signed-in owner up
  to `MAX_UPLOAD_BODY_BYTES` (64 MiB; the middleware publishes the ceiling and `verify_csrf`
  raises the limit once the session is known to be an owner's). Over the limit: 422 on the
  upload routes, 413 elsewhere.
- **Migrations:** Alembic, run at start (existing `alembic_runner.py`).
- **Backups (planned, M7):** today the real backup is the DSM scheduled `pg_dump` plus Hyper
  Backup, and the attachments folder must be added to that Hyper Backup selection by hand. The
  planned design, not built yet: a `backup` sidecar runs nightly `pg_dump`, encrypts it
  (`age`) with a public key (private key kept offline), keeps 30 dailies + 12
  monthlies, and copies to off-site storage (e.g. Hyper Backup target /
  cloud bucket). Attachments are included. A `restore.sh` and a documented
  quarterly drill.
- **Observability:** existing structured logging; healthcheck; audit log viewer.
- **Versioning:** the existing `/info` version display continues; semantic
  version bumps per milestone (v2.0.0 at M0 given the breaking structure).

## 11. Testing strategy

- **Unit:** tax engine (existing), posting builders (given a document, assert
  exact lines), period resolver, rounding.
- **Property tests (Hypothesis):** any sequence of valid operations leaves
  the TB balanced and Assets = Liabilities + Equity; voiding then re-posting
  nets to the expected balance.
- **Integration (real Postgres):** each router through the HTTP client with a
  logged-in test user; deferred balance trigger rejects an unbalanced insert;
  immutable-line trigger rejects UPDATE.
- **Auth:** parametrized test that every route in the OpenAPI schema except
  the allow-list returns 401 unauthenticated; lockout, MFA, CSRF, session expiry.
- **Golden / reconciliation:** 2026 monthly payroll runs equal the QBO figures;
  imported QBO TB equals app TB at the cutover date.
- **Restore test:** CI-able script restores a dump into a fresh DB and compares
  report totals.

## 12. Decisions needing owner approval (per `AGENTS.md`)

1. **Restructure** into a single app container (merge frontend, §2.1).
2. **Schema migration of production data** (including an `origin` column on payroll runs for imports): float→Decimal on `payroll_run` /
   `payroll_config`, and dropping SQLite (§3). A `pg_dump` precedes it.
3. **Package/product rename** from `payroll` to something like `dci_books` (optional; deferred).
4. New dependencies: `argon2-cffi`, `pyotp`, `cryptography`, `weasyprint`,
   `hypothesis` (dev), `qrcode`.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Payroll or return figures wrong → penalties | Parallel run vs. QBO, CPA review of year-end, versioned snapshots, annual verification checklist |
| Ledger corruption | DB-level balance and immutability triggers; property tests; nightly backups |
| Losing QBO history on cancel | Mandatory export archive before cancelling (SPEC §7) |
| Lock-in on auth mistakes / lockout | Recovery codes + documented offline `reset_owner` CLI |
| Scope creep (A/P, bank feeds, etc.) | "Could have/Out of scope" lists in the spec; milestones M0–M7 ordered by value |
| Cash-basis report complexity | Cash is the default, so it is built and tested from M1; a test proves cash and accrual agree on cumulative cash movement |
