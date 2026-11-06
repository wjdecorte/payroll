# M3 Expenses: design

Status: draft for owner review, 2026-11-05. Implements SPEC §8 milestone M3 (F8, S4, part of S6).
Builds on M1 (ledger) and M2 (receivables, v2.2.0). Target release: 2.3.0.

## 1. Goal and success criteria

Record what the company spends, keep the receipt, track what the company owes the owner for
expenses he paid personally, repay him, and move money between the company's own accounts.
Everything posts to the ledger; nothing is stored that the ledger cannot reproduce.

Done when:

1. An expense with several category lines, a payment source and receipts can be entered, shown,
   voided and found again; it posts exactly one balanced journal entry.
2. An expense paid by "Owner (personal card)" credits Due to Owner; Reimburse Owner repays any
   set of open owner-paid expenses with one bank payment and clears exactly their total.
3. A transfer between two company accounts (including a credit-card payment) posts and voids.
4. "Owed to me" always equals the Due to Owner ledger balance; any mismatch is shown, not hidden.
5. Every figure drills down to its expense, transfer or reimbursement; the cash-basis P&L needs
   no change (expenses are cash events).
6. Receipts are stored safely, are only served to signed-in users, and are in the backup.
7. All the M1/M2 integrity rules hold: lock date, void rules, owner-only mutations, CSRF,
   422-not-500, strict 2-decimal amounts, CSV formula neutralization.

## 2. Scope

In: vendors, expenses with split lines, transfers, owner reimbursement, generic attachments
(first used by expenses), Owner page and Owner-reimbursement report, housekeeping (consolidate
the duplicated `_commit` / `_locked_*` helpers from `payroll/receivables/_common.py` and the
other services into one shared module before adding code).

Out (and stays out): bank reconciliation (needs its own milestone slot, decide before M4),
recurring expenses (S5), vendor bills / A/P (SPEC §9 decision 6), 1099 data, OCR, vendor
refunds or credits (use a manual journal), partial reimbursement of one expense, attachments on
invoices and journals (the store is generic; wiring them is a later, small change).

## 3. Data model

New tables, new migration `d4e0a2b8c6f1_expenses.py` (head after `c3d9f1a7e2b4`).

```
vendor         id, name, note, active, created/updated
               unique (lower(name))
expense        id, date, vendor_id (nullable), payee_text (nullable), memo,
               source (account | owner), paid_from_account_id (null when source = owner),
               total, status (posted | void), entry_id, void_entry_id, void_date,
               reimbursement_id (nullable), created/updated
expense_line   id, expense_id, line_no, account_id, memo, amount
transfer       id, date, from_account_id, to_account_id, amount, memo,
               status (posted | void), entry_id, void_entry_id, void_date
reimbursement  id, date, paid_from_account_id, total, memo,
               status (posted | void), entry_id, void_entry_id, void_date
attachment     id, owner_type, owner_id, filename, content_type, sha256, stored_name,
               size, uploaded_at, uploaded_by
```

Database CHECKs and triggers (the DB is the last line of defence, as in M2):

- expense: exactly one of `vendor_id` / `payee_text` is set; `source = 'owner'` iff
  `paid_from_account_id IS NULL`; `total > 0`; `total` equals the sum of its lines (deferred
  constraint trigger); status/entry/void columns consistent (posted has no void fields, void has
  all of them); `void_date >= date`.
- `expense.reimbursement_id` may only be set while `source = 'owner'` and the status is posted,
  and is set-once (a trigger refuses changing it from non-null to another non-null value; it may
  be cleared only when the owning reimbursement is void).
- expense_line: `amount > 0`, `line_no` unique per expense. Lines are append-only.
- transfer: `from_account_id <> to_account_id`, `amount > 0`.
- reimbursement: `total > 0`; the sum of its expenses' totals equals `total` (deferred
  constraint trigger, FOR NO KEY UPDATE like the M2 application cap).
- attachment: append-only (UPDATE and DELETE refused by trigger), `size` between 1 and
  10 MB, `owner_type` in (`expense`), `sha256` is 64 hex characters.

`SourceType.EXPENSE` and `SourceType.REIMBURSEMENT` already exist; add `SourceType.TRANSFER`
(the column is a plain string, so no schema change for it beyond the CHECK list if one exists;
the implementer verifies and extends any CHECK). Reversal entries copy source type and source
id, as in M2.

## 4. Posting rules

Each document posts exactly one entry through `LedgerPostingService` and commits through the
shared rollback-on-any-error wrapper.

| Document | Entry |
|---|---|
| Expense, bank/card source | Dr each line's account for its amount; Cr payment account for the total |
| Expense, owner source | Dr each line's account; Cr Due to Owner (role account) for the total |
| Transfer | Dr `to` account; Cr `from` account |
| Reimbursement | Dr Due to Owner for the total; Cr the bank account |
| Void of any of these | Reversal entry dated the void date; documents released as below |

Account rules, checked in the service and again in tests:

- Expense lines: account type expense, cost of goods sold, or other expense; active.
- Payment account (expense, reimbursement paid-from, transfer ends): type asset or liability,
  active, and not bound to the A/R, Undeposited Funds or Due to Owner roles. Reimbursement
  paid-from must be an asset. Transfer accepts asset or liability on either end.
- Dr to a liability account (paying a card down) is allowed; so is a card going to a debit
  balance. No sign policing beyond the ledger's own rules.
- Amounts are strict 2-decimal positive values (the M2 parser), at most 100,000,000.00.
- Memo and payee text are capped at 500 characters and 200 characters; payee text and vendor
  names are trimmed, and control characters are refused.

## 5. Owner flow

- **Open owner-paid expenses** are posted expenses with `source = 'owner'` and no
  `reimbursement_id`.
- **Reimburse Owner:** the owner picks one or more open expenses, a date and a bank account.
  The service locks the Due to Owner role row FOR SHARE, then the chosen expenses FOR UPDATE in
  id order, rejects any that are not open (already reimbursed, void, or not owner-paid), then
  the period lock FOR SHARE, then the per-source entry counter. It creates the reimbursement
  with `total` = sum of the expenses, posts Dr Due to Owner / Cr Bank, and sets
  `reimbursement_id` on every expense, all in one transaction. The date must be on or after the
  latest chosen expense date and after the lock date.
- **Void a reimbursement:** needs no later dependency (nothing depends on it); posts the
  reversal dated on or after the reimbursement date and after the lock, clears
  `reimbursement_id` on its expenses, which become open again.
- **Void an expense:** refused while `reimbursement_id` is set (void the reimbursement first);
  otherwise posts the reversal dated on or after the expense date and after the lock.
- **Owed to me** is the Due to Owner ledger balance (credit minus debit). The Owner page also
  shows the sum of open owner-paid expenses. When they differ (a manual journal touched the
  account, or an opening balance was entered) the page shows both figures and a plain note,
  never silently picking one.
- **Role freeze:** the Due to Owner role is frozen once any owner-source expense or
  reimbursement exists (voided ones included), like the A/R rule. It is bound in Settings >
  Ledger beside the existing roles. Posting an owner-source expense with the role unbound is a
  422 telling the owner to bind it.

## 6. Locking order

Same family as M2, with one new sentence: payment/role rows first, then document rows by id,
then the period lock FOR SHARE, then the per-source entry counter. Reimbursement and the
owner-source expense both take the Due to Owner role row FOR SHARE (`bind_role` takes it FOR
UPDATE), so a rebind cannot interleave with a posting. Two reimbursements racing for the same
expense serialize on the expense row lock; the loser sees it already reimbursed and gets a 422.

## 7. Attachments

- Store: `{ATTACHMENT_DIR}/{yyyy}/{mm}/{random 32 hex}` where `ATTACHMENT_DIR` defaults to
  `/data/attachments` (already a volume in DESIGN §9). The directory is created with mode 0700;
  files are written to a temporary name, fsynced and renamed. If the database insert fails the
  file is removed; if the file write fails nothing is inserted.
- Allowed types, decided from the bytes (magic numbers), not the filename or the sent
  content type: PDF, PNG, JPEG, HEIC. 10 MB per file, 10 files per expense, 50 MB per expense.
  Anything else is a 422 with a message naming the problem.
- Stored name is random; the original filename is kept only as display text, stripped to a
  safe basename, 150 characters, control characters removed.
- Download route requires login, any role (viewers read). Response headers:
  `Content-Type` from the verified type, `Content-Disposition: attachment` with an encoded
  filename, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`. Images may
  also be shown inline by an `?inline=1` that is honored only for PNG and JPEG.
- Upload requires owner role and CSRF. Uploading is allowed on a posted or void expense;
  removal is not offered anywhere (append-only, auditable). A wrongly uploaded file is
  explained by a memo, not deleted. This is a deliberate trade for auditability.
- The ledger-level lock date does not govern attachments (they carry no money).
- Backups: the existing backup job already includes `/data/attachments`; the RUNBOOK gains a
  line for the restore drill.
- Duplicate content on the same expense (same sha256) is rejected with a 422.

## 8. Screens and routes

All under `payroll/frontend/expense_routers.py`, templates in `templates/expenses/`.
Every mutating route is owner-only and CSRF-protected; every GET is any signed-in role.

- `/vendors` list, add, edit name/note, deactivate (no delete; inactive vendors stay on old
  expenses and are hidden from new-expense pickers).
- `/expenses` list: filters for date range, vendor, account, status, owner-paid-open; paging of
  50; CSV export. `/expenses/new` form: date, vendor or payee text, payment source, memo, lines
  (add/remove rows, running total), receipt upload (multiple). `/expenses/{id}` detail with
  receipts, ledger entry link, Void with date.
- `/transfers` list, new, detail, void.
- `/owner` page: owed-to-me, the reconciliation note when needed, open owner-paid expenses
  with checkboxes, Reimburse form (date, bank account), reimbursement history with void.
- `/reports/owner-reimbursement` (S6): as-of date, owed balance, open expenses, reimbursements
  in range; CSV and PDF export through the existing report export layer (formula-neutralized).
- Navigation: Expenses, Vendors, Transfers, Owner added to the existing menu (green theme).
- Drill-down: expense/transfer/reimbursement entries in the GL, P&L and Balance Sheet detail
  link to their document, as invoices do today.
- The route-protection sweep gets every new mutating endpoint added in the same task that
  adds it (the M2 ruling).

## 9. Seed data and docs

`seed-demo` gains a few vendors, a split expense with a receipt placeholder, an owner-paid
expense, a reimbursement, and a card payment transfer. README, RUNBOOK ("Upgrading to
v2.3.0": pg_dump first, additive migration, bind Due to Owner, create the attachments volume),
SPEC (§4.4 rulings, M3 marked done at the end) and DESIGN (§4.3, §7) are updated; version
2.3.0 in `pyproject.toml`, `payroll/__init__.py`, `.env.example`, `uv.lock`.

## 10. Decisions to confirm (rulings made in this spec)

1. Reimbursement is whole expenses only; no partial reimbursement. If wrong: an expense of
   $500 can't be repaid in two $250 instalments. Workaround is a manual journal.
2. Attachments can't be removed. If wrong: a mistaken upload stays forever. Easy to add a
   guarded owner-only delete later.
3. No vendor refunds/credits; negative lines refused. If wrong: use a manual journal.
4. Vendor optional, payee text allowed. If wrong: some expenses have no vendor for reports.
5. Due to Owner role is frozen after first use. If wrong: rebinding needs a journal.
6. Reimbursement paid from an asset account only (not a credit card).
7. Bank reconciliation is not in M3.

## 11. Testing and review focus

Layers as in M2: service unit tests, route and permission sweeps (viewer, accountant, anonymous,
no-CSRF), migration/trigger tests, Hypothesis properties, browser verification on a seeded
database, PDF export in Docker.

Properties and races that must be tested:

- Every expense, transfer, reimbursement and void leaves the ledger balanced; the sum of lines
  equals the expense total for any split (Hypothesis).
- Reimbursement total equals the sum of its expenses for any selection; void then re-reimburse
  gives the same figures.
- Two reimbursements racing for one expense: exactly one wins (real two-session test, mutation
  checked). Reimburse vs void-expense race. Reimburse vs `bind_role` race. Expense post vs lock
  date change race.
- Due to Owner ledger balance equals the sum of open owner-paid expenses on seeded data with no
  manual journals, and the mismatch note appears when one is posted.
- Locked-period behavior: cannot post, void or reimburse on or before the lock date; void date
  rules.
- Attachments: a renamed `.exe` with a `.pdf` name is refused; a PDF with an `.exe` name is
  accepted as PDF; zero-byte, 10 MB + 1, 11th file, duplicate sha256, path traversal in the
  filename, unicode/RTL filename, concurrent uploads, failed insert removes the file, download
  without login is 302/401, viewer can download, headers are exactly as specified.
- Inputs: a $0.00 line, a line over two decimals, 101 lines, an inactive account, an account of
  the wrong type, a payment account that is a bound role, vendor and payee text both set or
  both missing, a 501-character memo, a formula-looking vendor name in CSV export.
- Credit card flow: card expense then transfer to pay the card brings the card balance to zero;
  voiding the transfer restores it.
- Consolidated helpers keep every M2 test green (the refactor lands first, alone).

## 12. Known follow-ups (not in M3)

Bank reconciliation, recurring expenses, attachments on invoices and journals, optional
attachment delete, vendor credits, QBO import of vendors and expense history (M6).
