# DCI Books — Product Specification

| | |
|---|---|
| **Status** | Draft v0.2 (open questions answered 2026-10-31) |
| **Date** | 2026-10-30 |
| **Owner** | Jason DeCorte |
| **Companion** | [DESIGN.md](DESIGN.md) |
| **Working name** | DCI Books (the existing DCI Payroll app grows into this) |

## 1. Purpose

Replace QuickBooks Online (QBO) for DeCorte Industries, a single S-Corp, so the
subscription can be cancelled. The app must keep a correct double-entry set of
books, produce the reports the owner and a CPA need, and run payroll and the
payroll-tax filings that are currently split across QBO and the existing
DCI Payroll calculator.

**Success means:** the QBO subscription is cancelled, and the CPA can prepare
the 1120-S and the owner can file every payroll return from this app's output
alone.

## 2. Users, scale and environment

- **Users:** one owner (full access). Optional second login for a CPA/bookkeeper
  (read-only reports or full access, owner's choice). Not multi-tenant, not a product.
- **Entity:** one company (DeCorte Industries). The schema keeps a single
  `company` row; multi-company is explicitly out of scope.
- **Volume:** hundreds of transactions per year, one payroll employee (the owner),
  monthly payroll. Performance is not a constraint; correctness and auditability are.
- **Deployment:** Docker on the Synology NAS, reachable on the LAN / VPN only
  (confirmed 2026-10-30). Never exposed to the public internet.
- **Jurisdiction:** US federal + Georgia. Tax year constants are reviewed each January.

## 3. Scope

### 3.1 Must have (v1)

| ID | Area | Requirement |
|---|---|---|
| F1 | Ledger | Double-entry general ledger. Every posted journal entry balances (debits = credits) to the cent. Posted entries are immutable; fixes are made by reversal or void, never by edit. |
| F2 | Chart of accounts | Hierarchical chart with account number, name, type (asset, liability, equity, income, cost of goods sold, expense, other income, other expense), detail type, active flag. Seeded from a QBO import; editable. Accounts with activity can be deactivated but not deleted. |
| F3 | Manual journals | Create, reverse, and void general journal entries with memo and attachments. |
| F4 | Reports | Balance Sheet and Profit & Loss with preset periods (this/last month, quarter, year, YTD, trailing 12 months) and custom date ranges; **cash basis by default** with an accrual toggle; comparison to prior period or prior year. Also: Trial Balance, General Ledger, Journal, Account Register. All exportable to CSV and printable/PDF. Every report figure drills down to its transactions. |
| F5 | Customers | Customer list with contact, billing address, default terms. |
| F6 | Invoices | Create, edit (until paid), send-ready PDF, void. Line items from an item/service list mapped to income accounts. Terms and due date, sequential numbering, status (draft, open, partial, paid, void, overdue). Posts Dr Accounts Receivable / Cr Income. |
| F7 | Customer payments | Record a payment received against one or more invoices (partial and over-payment supported; unapplied credit kept on the customer). Record deposits: either straight to a bank account or into Undeposited Funds and later grouped into a bank deposit. |
| F8 | Expenses | Enter expenses with date, vendor/payee, category account(s), amount, receipt attachment. Payment source is a bank/credit-card account, or **"paid personally by owner"**, which credits a Due-to-Owner liability. A reimbursement payment to the owner clears that liability. |
| F9 | Payroll | The existing calculator (federal withholding, FICA, Georgia withholding, S-Corp owner health premiums and HSA) is integrated. Approving a payroll run posts its journal entry to the ledger automatically; no re-keying. Payroll history is kept and re-viewable. |
| F10 | Payroll taxes | Track payroll liabilities by agency. Record deposits/payments to IRS (EFTPS) and Georgia DOR; liabilities reconcile to zero when paid. Show what is owed and when it is due. |
| F11 | Payroll returns | Produce filing-ready worksheets, with the figures for each box, for: **Form 941** (federal, quarterly), **Georgia GA-7M** (state withholding), **Georgia DOL-4N** (state unemployment, quarterly) and **Form 940** (federal unemployment, annual). Also W-2 / W-3 data at year end. See §6 for the verification rule. |
| F12 | Authentication | Secure login for all access (§5). |
| F13 | Import | Import the QBO chart of accounts, customers, items, and opening balances at the cutover date (§7). |
| F14 | Backup and restore | Automated encrypted database backups with a documented, tested restore. |

### 3.2 Should have (strongly recommended additions)

These weren't in the original list, but a QBO replacement is missing something
important without them. Proposed for v1 unless the owner objects.

| ID | Area | Why |
|---|---|---|
| S1 | **Audit log** | Who changed what and when. Required for trust in a ledger. Cheap if built in from the start, hard to retrofit. |
| S2 | **Period close / lock date** | Prevents edits to closed periods (e.g. after tax filing). Password-gated override. |
| S3 | **Bank register + reconciliation** (**in v1**, confirmed) | Mark transactions cleared against a statement (CSV import of the statement). Without it there is no way to prove the books match the bank. |
| S4 | **Attachments** | Receipts and PDFs attached to expenses, invoices, and journals. |
| S5 | **Recurring templates** | Recurring invoices and recurring expenses (e.g. a monthly retainer). |
| S6 | **Additional reports** | A/R Aging, Customer Statement, Open Invoices, Payroll Summary, Payroll Liability report, Owner-reimbursement report. |
| S7 | **Year-end close** | Roll P&L into retained earnings, preserving opening balances for the new year. |

### 3.3 Could have (later)

Vendor bills / A/P and bill payments, 1099-NEC data, email delivery of invoices
via SMTP, online invoice payment links, multi-employee payroll, passkey login,
estimated-tax and shareholder distribution tracking (basis schedule), dashboard.

### 3.4 Out of scope

Bank feeds, inventory, sales tax, multi-company or multi-currency, time
tracking, direct e-filing with the IRS or Georgia, direct deposit/ACH
origination, a mobile app (the web UI must work on a phone browser on the LAN).

## 4. Functional detail

### 4.1 Ledger rules (apply everywhere)

1. Amounts are exact decimals, with two places for money. Floats are never used.
2. A journal entry has ≥ 2 lines, each with exactly one of debit or credit > 0,
   and total debits = total credits. The system refuses to post otherwise.
3. Every operational document (invoice, payment, expense, deposit, payroll run)
   produces exactly one linked journal entry. Editing the document replaces its
   entry within the same database transaction; the old version is kept in the
   audit log. Voiding creates a reversing entry.
4. Dates are transaction dates (not entry timestamps). Entries dated on or before
   the lock date are rejected.
5. Entry numbers are sequential, gap-free per type, and assigned at posting.
6. Account balance = sum of its lines. Balances are always computed from lines,
   never stored.

### 4.2 Chart of accounts

- Number + name unique; a parent/child tree for sub-accounts (e.g.
  `Payroll Liabilities:Federal Income Tax Withheld`).
- A few **system roles** are bound to accounts so automated postings work:
  Accounts Receivable, Undeposited Funds, Due to Owner, Retained Earnings,
  Opening Balance Equity, and the payroll accounts (wages, employer tax
  expense, each liability, health insurance, HSA). Roles are configurable in
  Settings; replaces the `ACCT_*` env vars used today.
- A default chart suitable for a small S-Corp is provided for new installs; for
  the real company the QBO export is the source (§7).

### 4.3 Invoices and receivables

- Statuses derive from payments and due date; `overdue` is computed, not stored.
- Invoice numbers sequential, user-settable starting number (to continue the QBO sequence).
- PDF layout includes company logo/address, remit-to, terms, line items, total,
  balance due. Sent manually in v1 (download and email yourself).
- A payment may be split across invoices; an over-payment leaves an unapplied
  credit that can be applied to a later invoice or refunded.
- Deleting a payment is not allowed; it is voided with a reversal.

**Rulings made for M2 (v2.2.0), where this spec was silent:**

1. **Cash-basis method.** An invoice entry never appears on cash basis. A payment's own cash
   entry is untouched (the money sits in A/R as customer credit). Each *application* of a
   payment to an invoice adds, on its recognition date (the later of the payment and invoice
   dates), a debit to A/R and credits to the invoice's income accounts, allocated cumulatively
   per account with caps so a fully paid invoice yields exactly its per-account income. A void
   payment adds the mirror pair dated the later of the void date and the application's
   recognition date (so an application voided before it was recognized never reaches income).
   Unapplied money stays a credit in A/R (not income).
2. **Applications are append-only and refused on or before the lock date** (they would change
   a closed period on cash basis). Misapplied money is fixed by voiding the payment and
   re-recording it. Credit can never go negative on any past date.
3. **No JSON API for receivables documents** (pages only). The A/R Aging CSV/PDF also appear
   under the M1 report API as read-only exports. Importers (M6) call the services directly.
4. **Void dates.** A void (invoice, payment, deposit or refund) must be dated on or after the
   document itself, so a reversal never precedes the original. It must also be dated after the
   period lock, and the M1 ledger additionally refuses to void a document whose own date is on
   or before the lock (a void erases it from its own period). To correct a document in a locked
   period, move the lock back (password-gated), void or replace it, and lock again (see the
   RUNBOOK). A reversal posted through a manual journal is not a correction: the document stays
   live in its subledger, so the A/R aging and customer balances would not change.
5. **Invoice rules.** Integer numbers assigned on issue from a settable next number (above
   every number used; voided numbers are never reused). Draft, Open, Void are stored; Partial,
   Paid and Overdue are derived. An open invoice can be edited only while nothing is applied
   (edit = reverse and re-post on the same date and number). Void needs no applied payments and
   takes a date on or after the invoice date and after the lock.
6. **Terms are net days only.** No discounts, late fees, credit memos or sales tax (§3.4).
7. **Refunds of unapplied credit** use the journal source type `refund`.
8. **The A/R role is frozen** once any invoice, payment or refund has been recorded (voided
   ones included); Undeposited Funds can still move.
9. **Reports in M2:** A/R Aging with an open-invoice detail. Customer Statement and
   attachments stay for later.
10. **Owner-only mutations;** accountants and viewers read everything (as in M1). Company
   details (name, address, remit-to, footer) and the next invoice number live in
   Settings > Receivables.

### 4.4 Owner-paid expenses

- Expense lines carry an expense account, optional memo and receipt.
- Choosing payment source "Owner (personal card)" credits `Due to Owner`.
- A **Reimburse Owner** action selects open owner-paid expenses and posts
  Dr Due to Owner / Cr Bank for the total, marking them reimbursed.
- The owner can see an "owed to me" balance at any time.

**Rulings made for M3 (v2.3.0), where this spec was silent:**

1. **Whole-expense reimbursement only.** A reimbursement repays whole open owner-paid expenses;
   there is no partial reimbursement (an expense cannot be repaid in instalments).
2. **Receipts are append-only.** An uploaded receipt cannot be removed or replaced; a wrong
   upload is explained with a memo. Uploads are allowed on posted and void expenses. Types
   (PDF, PNG, JPEG, HEIC) are decided from the file contents.
3. **No vendor credits or negative lines.** Expense lines and totals must be positive; a vendor
   refund is recorded with a manual journal.
4. **Vendor or free-text payee.** Each expense has exactly one of a vendor or a payee text.
5. **The Due to Owner role is frozen** once any owner-source expense or reimbursement exists
   (voided ones included), like the A/R rule. Posting an owner-source expense with the role
   unbound is refused.
6. **A reimbursement is paid from an asset account only** (not a credit card or other liability).
7. **Transfers are in M3.** A transfer moves an amount between two different asset or liability
   accounts (for example a credit card payment); the bound roles (A/R, Undeposited Funds, Due to
   Owner) are not allowed on either side.
8. **Bank reconciliation is NOT in M3.** It stays in v1 scope (§9, decision 8) for a later
   milestone.
9. **Error codes.** A state conflict (an expense already reimbursed, a document already void, a
   locked period) answers HTTP 409; bad input answers 422. When two reimbursements race for one
   expense the loser gets a 409. This amends the "422" wording in the M3 design (section 6).
10. **Owner-only mutations;** accountants and viewers read every page, report and receipt
    download. Every mutating route requires the owner role and CSRF.

### 4.5 Payroll

- Preserve the existing tax engine behavior (2026 constants, 73 existing tests).
  Calculation results must be bit-for-bit unchanged after the float→Decimal
  conversion except for rounding-to-cent differences, which are reviewed.
- A payroll run has states `draft → approved (posted) → voided`. Only approved
  runs affect the ledger and the returns.
- Posting produces one journal entry: wages, employer taxes and health
  expense debited; each withholding/employer-tax liability and net pay credited
  (net pay to a bank account or Due-to-Owner when not yet paid).
- Employee record holds name, address, SSN (encrypted), filing status, W-4
  inputs, pay frequency. v1 has one employee but the table allows more.
- Tax constants are versioned by year so previous years' runs stay reproducible.
- **Open item:** confirm how >2% S-Corp shareholder health premiums and HSA
  contributions are treated for W-2 boxes (included in Box 1, excluded from
  FICA wages). This is an existing open question in the project note and must
  be settled with the CPA before the W-2 and 941 outputs are trusted.

**Rulings made for M4 (v2.4.0), where this spec was silent:**

1. **Run states.** `draft`, `approved`, `void` and `imported`. Saving a run creates a draft; only a
   draft can be deleted. A run saved before M4 is `imported`: it keeps its figures, is never posted,
   cannot be deleted, and still counts in the year-to-date wage chain. The database refuses any change
   to a non-draft run except approved to void.
2. **Approval posts once.** Approving takes a pay date (not before the first day of the pay period),
   and net pay from a bank account or Due to Owner, and posts one entry dated the pay date. The stored
   figures are never recalculated. Voiding takes a date on or after the pay date and after the lock,
   and posts the reversal. Zero lines are dropped.
3. **Year-to-date chain.** One non-void run per pay period (a void run frees its period). A run cannot
   be saved while a later-period non-void run exists in the tax year. It cannot be approved while an
   earlier run of the year is a draft, or when its year-to-date start differs from the previous non-void
   run's end (delete it and save it again). It cannot be voided while any later non-void run of the
   year exists (void later approved runs first and delete a later draft).
4. **FUTA and SUTA** are calculated at save from the run's FICA wages and the year-to-date wages, with
   the rates and wage bases in Settings > Payroll, and stored on the run. Rates are entered as
   percentages. Only the tax engine's year (`TAX_YEAR`) is read; saving a run is refused until that
   year's settings exist.
5. **Payroll roles.** Eight system roles (`payroll_wages`, `payroll_tax_expense`,
   `payroll_federal_payable`, `payroll_ga_wh_payable`, `payroll_futa_payable`, `payroll_suta_payable`,
   `payroll_health_payable`, `payroll_hsa_payable`). One federal payable account holds withholding,
   Social Security and Medicare. The roles are frozen once any run is approved or void or any tax
   payment exists. Approving with a role unbound is refused and names the role.
6. **Paying the insurer and funding the HSA** are M3 transfers (or an expense when the premium is paid
   directly), not part of a run.
7. **Net pay to Due to Owner (known limitation).** The Owner page and the Owner reimbursement report
   show a difference equal to the unpaid net pay until it is settled. No settlement document exists
   yet; the only way today is a manual journal, Dr Due to Owner / Cr bank.
8. **The QuickBooks account names in `PayrollConfig`** and the QuickBooks entries view and CSV stay
   until M7.
9. **Owner-only mutations;** accountants and viewers read every page and report. Approve and void are
   pages only. The JSON API refuses `DELETE` of a non-draft run (409) and run responses gain `status`,
   `payDate`, `entryId`, `futa` and `suta`.

### 4.6 Payroll tax payments and returns

- **Liabilities by agency:** IRS (federal withholding, SS, Medicare, FUTA),
  Georgia DOR (withholding), Georgia DOL (SUTA).
- **Payments:** record a tax payment from a bank account; apply it against
  liabilities; the liability report shows balance owed and next due date.
- **Returns:** each return is a report with the period, the line-by-line
  figures with the formula behind each one, and a drill-down to the
  contributing payroll runs. They are not forms to e-file; the owner keys the
  figures into the agency portals (IRS EFTPS/IRS online, Georgia Tax Center,
  Georgia DOL).

| Return | Cadence | Source data |
|---|---|---|
| 941 | Quarterly | Wages, withholding, SS/Medicare, deposits by quarter |
| GA-7M | As GA DOR requires for the deposit schedule | Georgia withholding |
| DOL-4N | Quarterly | Wages, taxable wages vs. SUTA wage base, SUTA due |
| 940 | Annual | FUTA wages, credit reduction, deposits |
| W-2 / W-3 | Annual | Year totals per employee |

**Rulings made for M4 (v2.4.0), where this spec was silent:**

1. **Four agency targets.** A tax payment is made to `irs_941`, `irs_futa`, `ga_dor` or `ga_dol`
   and posts Dr the agency's payable account (`payroll_federal_payable`, `payroll_futa_payable`,
   `payroll_ga_wh_payable`, `payroll_suta_payable`) / Cr the bank. The bank account is an asset or
   liability account that is not bound to a payroll role or to A/R, Undeposited Funds or Due to Owner.
2. **Payments are not tied to a period.** The liability report applies the payments to the oldest
   period first; nothing is stored. An amount above the balance is an overpayment (a negative
   balance). Penalties and interest are ordinary expenses.
3. **Payments are voided** with a date on or after the payment date and after the lock.
4. **Deposit due dates** are one set of rules (table `payroll_deposit_rules`), not per tax year, with a
   cadence and a due day per agency. Defaults: Form 941 and Georgia DOR monthly on day 15 of the
   following month; FUTA and Georgia DOL quarterly, due day 0 (the last day of the month after the
   quarter). Semiweekly schedules are not supported.
5. **Liability report.** Per agency: the ledger balance of the payable account, the unpaid amount of each
   pay period, the next due date, and an Other line (the balance not explained by the periods) that is
   always shown and never aged. A run or tax payment voided after the as-of date still counts as of
   that date. Periods with no liability for an agency are not listed. CSV and PDF are available.
6. **Drill-down.** A payroll run entry opens `/?run={id}`; a tax payment entry opens
   `/payroll/tax-payments/{id}`.

**Known follow-ups (not in M4):** the Payroll Summary report and the returns (M5); semiweekly deposits;
explicit tax-payment applications; split federal payables; a settlement document for net pay owed to the
owner; removing the QuickBooks account names and CSV export (M7); and how approved runs made during the
parallel run relate to the QBO general-ledger import (the M6 design must decide whether to void them
before the import).

### 4.7 Reports

- Presets: This Month, Last Month, This Quarter, Last Quarter, This Year, Last Year,
  YTD, Trailing 12 Months, plus Custom (from/to).
- Balance Sheet is "as of" a date; P&L is for a period.
- **Cash basis is the default** (owner's decision, 2026-10-31); an accrual toggle is available on P&L and Balance Sheet. The ledger itself still records invoices to Accounts Receivable when issued, so both views derive from the same data (see DESIGN §7).
- Columns: single period, or compared to previous period / previous year, with
  $ and % change.
- Every total clickable down to account → transactions → source document.
- Invariant checks run in tests and are visible in the UI: Assets = Liabilities +
  Equity; Trial Balance debits = credits.

## 5. Security requirements

LAN/VPN-only deployment reduces exposure but the data (SSNs, EIN, bank and tax
data) still demands real protection against a compromised device on the
network.

| ID | Requirement |
|---|---|
| SEC1 | All routes except `/login` and `/healthcheck` require an authenticated session. No anonymous API. |
| SEC2 | Passwords hashed with Argon2id. Minimum length 12, checked against a breached-password list offline. |
| SEC3 | Mandatory TOTP second factor (authenticator app) with one-time recovery codes. Passkeys (WebAuthn) are a later enhancement. |
| SEC4 | Server-side sessions with random IDs; cookies `HttpOnly`, `Secure`, `SameSite=Lax`; idle timeout (30 min) and absolute timeout (12 h); logout invalidates server-side. |
| SEC5 | CSRF protection on every state-changing request. |
| SEC6 | Login rate limiting and progressive lockout; all auth events written to the audit log. |
| SEC7 | Roles: `owner` (all), `accountant` (read + reports, optional post), `viewer` (reports only). Enforced in the service layer, not just the UI. |
| SEC8 | TLS between browser and app, terminated by the Synology reverse proxy (or Tailscale `serve`); HSTS and security headers set by the app. |
| SEC9 | SSN and bank-account numbers encrypted at rest (AES-GCM, key from environment/secret file, never in the DB or repo); masked in the UI except on explicit reveal (logged). |
| SEC10 | Backups encrypted with a key kept separate from the NAS; restore tested before cutover and quarterly after. |
| SEC11 | Secrets only via environment or Docker secrets; `.env.example` kept current; nothing sensitive committed. |
| SEC12 | The database port is not published on the host (today's `compose.yml` publishes 5432); only the app container can reach it. |

## 6. Compliance and accuracy rules

- Tax rates, wage bases, form line definitions and due dates change yearly. The
  forms named here (GA-7M, DOL-4N, 941, 940) are taken from the owner's
  current QBO workflow and must be **verified against the current agency
  instructions** before each year's first filing. A short per-year checklist
  lives in the repo.
- Payroll output is cross-checked against QBO for at least two full cycles
  (§8, parallel run) before QBO is cancelled.
- The app presents figures and worksheets; it does not give tax advice. The
  CPA reviews year-end outputs.
- Records are retained for at least 7 years; nothing is hard-deleted.

## 7. Data migration and cutover

1. **Before anything else:** export from QBO and archive (outside the app): all
   reports for all years, the general ledger / journal, customer and invoice
   lists, payroll reports and forms, and attachments. QBO access ends when the
   subscription does, so this archive is the only copy of history.
2. Import the chart of accounts, customers, and items (CSV).
3. **Mid-year cutover (decided 2026-10-31).** The cutover date is the first day
   of a month, chosen once M6 is complete and the parallel run reconciles.
   Opening balances for every balance-sheet account are loaded as of that date
   in one opening-balance journal entry. Import the current year's activity from
   the QBO general ledger so year-to-date reports are complete in the app.
4. **Payroll history import.** The year's payroll runs before the cutover are
   imported as historical runs (marked `imported`, posted to the ledger through
   the import rather than re-calculated). This feeds Social Security wage-base
   tracking, the quarterly returns, and the annual 940 and W-2.
5. **Split-quarter returns.** A cutover that is not on a quarter boundary means the
   cutover quarter's 941 and DOL-4N, and the year's 940 and W-2, combine
   imported and app-calculated runs. The return builders treat both identically
   and the worksheet shows which runs were imported. The quarter's figures
   must be tied to QBO's report for the same quarter before filing.
6. **Full history is the end goal.** All prior years from QBO are to be loaded into
   the ledger eventually, so the importer is built to take the QBO general ledger
   export in any date range: current year first, then earlier years backwards.
   Earlier years load as additional dated journal entries before the opening
   balance date, with the opening-balance entry replaced by real history
   (validated by matching trial balance at each year-end). Locked periods keep
   them from being edited afterwards.
7. Parallel run: enter the month or two before the cutover in both systems and
   reconcile the Balance Sheet and P&L to the penny.
8. Because the cutover is soon-but-not-yet, QBO is cancelled only after
   cutover plus one clean month-end and the archive in step 1 is stored.

## 8. Milestones and acceptance

| # | Milestone | Done when |
|---|---|---|
| M0 | Foundation | Auth + MFA, single-app structure, Postgres-only, Decimal money, audit log, CI green. |
| M1 | Ledger & reports | **Done (v2.1.0).** Chart of accounts, manual journals, Trial Balance, Balance Sheet, P&L (all presets and custom), GL, CSV/PDF export. Cash (default) and accrual toggle are in place; figures differ once receivables exist (M2, v2.2.0). |
| M2 | Receivables | **Done (v2.2.0).** Customers, items, invoices (draft, issue, edit, void, PDF), payments with applications, deposits, customer refunds and credit, A/R aging, cash-basis reports with drill-down. |
| M3 | Expenses | **Done (v2.3.0).** Vendors, expenses with split lines and receipts, owner-paid expenses and whole-expense reimbursement, transfers, the Owner page, owner reimbursement report with drill-down. |
| M4 | Payroll to ledger | **Done (v2.4.0).** Existing calculator posts to the ledger on approval (draft, approved, void, imported), FUTA and Georgia SUTA calculated and posted, tax payments to four agencies, Payroll Liability report with drill-down, Settings > Payroll. |
| M5 | Payroll returns | 941, GA-7M, DOL-4N, 940, W-2/W-3 worksheets matching QBO's figures for 2026. |
| M6 | Import & parallel run | QBO import of the current year (chart, customers, general ledger, payroll history) and a parallel run reconcile to the penny; earlier years follow as M8. |
| M7 | Cutover | Backup/restore drill passed, QBO archive stored, books live on the first of a month, subscription cancelled after one clean month-end. |
| M8 | Historical backfill | Prior years loaded from QBO in reverse order; each year-end trial balance equals QBO's. |

**Global acceptance criteria**
- Balance Sheet balances and Trial Balance debits = credits on all test and imported data.
- 2026 payroll run totals equal QBO's for every month.
- No endpoint is reachable without login; auth tests cover each router.
- Test suite green; coverage not below the current level (85%).
- A restore from backup to a fresh container reproduces identical report totals.

## 9. Decisions and open questions

**Decided (2026-10-31, owner):**

| # | Question | Decision |
|---|---|---|
| 1 | Cutover | Mid-year, first of a month, once M6 is done (§7) |
| 2 | Accounting basis | Cash primary, accrual toggle |
| 3 | Accountant access | No login; PDF/CSV exports are enough |
| 6 | Vendor bills / A/P | Not needed; expenses paid at purchase |
| 7 | Invoice delivery | Download PDF, email manually |
| 8 | Bank reconciliation | In v1 |
| 9 | History | Full QBO history eventually; start with the current year |

**Still open:**

1. **Shareholder health premiums / HSA**: confirm W-2 and 941 treatment with the CPA (§4.5). Blocks trusting M5 output, not M0–M4.
2. **GA-7M and DOL-4N filing cadence** for DCI (monthly vs quarterly deposit schedule), and any additional Georgia annual filings. Blocks M5.
3. **Target cutover month**: pick the actual month once M6 is scheduled.
