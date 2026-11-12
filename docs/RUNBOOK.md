# DCI Payroll runbook (v2.4.0)

Compose service names: `db` (Postgres 17) and `api` (the whole app, UI + API).
Synology paths below assume the stack lives in `/volume1/docker/payroll`.

## BEFORE DEPLOYING THIS RELEASE (v2.0.0)

Pushing to `master` builds the image (after tests pass) and **Watchtower pulls it within
about 5 minutes**. The new container runs the money-to-`NUMERIC` migration on startup.

Order matters (the old `frontend` container still holds host port 8070, and with
Watchtower stopped a plain `up -d` would reuse a stale local `:latest`):

1. **Pause Watchtower** so the upgrade happens when you are ready:
   `docker compose -f compose.synology.yml stop watchtower`
2. **Back up the database:**
   ```bash
   docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll > payroll_pre_v2_$(date +%F).dump
   ```
   Restore is a **full** `pg_restore` into an empty or recreated database; a data-only restore
   into an already migrated database fails, because the trigger functions resolve table names
   through `search_path`. Stop the app first:
   ```bash
   docker compose -f compose.synology.yml stop api
   docker compose -f compose.synology.yml exec -T db pg_restore -U payroll -d payroll --clean --if-exists < payroll_pre_v2_YYYY-MM-DD.dump
   ```
   The migration's downgrade cannot restore sub-cent float noise that existed in the old
   float columns; the dump is the only way back to the exact old data. To roll back to
   pre-v2 data you must also **pin the old image tag** (e.g. `ghcr.io/wjdecorte/payroll:sha-<old>`)
   in the compose file instead of `:latest`, or `:latest` (and Watchtower) will migrate it again.
3. **Check for legacy sub-cent inputs** (run before upgrading):
   ```sql
   SELECT id FROM payroll_run
   WHERE gross_salary::numeric <> round(gross_salary::numeric,2)
      OR health_insurance::numeric <> round(health_insurance::numeric,2)
      OR hsa_contribution::numeric <> round(hsa_contribution::numeric,2)
      OR ytd_fica_before::numeric <> round(ytd_fica_before::numeric,2);
   ```
   Run it with `docker compose -f compose.synology.yml exec db psql -U payroll payroll`.
   It checks only these four `payroll_run` input columns because they are the inputs from which the other
   figures were calculated. Any rows returned are saved runs whose inputs had fractions of a cent; their
   rebuilt journal could differ by a cent from what you posted. Review those runs against
   QBO after the upgrade. No hits means nothing to do.
4. Set `ENCRYPTION_KEY` in `.env` (next section), replace the compose file with the new one
   (the `frontend` service is gone), then pull and start, removing the orphan:
   ```bash
   docker compose -f compose.synology.yml pull
   docker compose -f compose.synology.yml up -d --remove-orphans db api   # watchtower stays stopped
   ```
5. **Verify:** the schema is at the release's migration head:
   `docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c 'select version_num from alembic_version'`
   must print `7c1f3a5e8b92` for v2.0.0 and `a1c5e8d3b7f2` for v2.1.0 (the expected revision
   changes in future releases; see `_HEAD_REVISION` in `payroll/migrations/alembic_runner.py`), and
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200.
6. Create the owner (see First run), then **re-enable Watchtower:**
   `docker compose -f compose.synology.yml start watchtower`

What changes for you:
- Saved runs display their stored values. Calculation now rounds half-up on exact
  decimals, so a new run can differ by $0.01 from the old float rounding on half-cent ties.
- API money values are JSON **strings** now. Scripts using the API must parse them as
  decimals (breaking change).
- Every route requires sign-in with MFA. There is no API-only anonymous access.

## Upgrading to v2.4.0 (payroll to ledger, M4)

The M4 migration (`e5f1b3c9d7a2`) adds columns to `payroll_run` and three tables
(`payroll_settings`, `payroll_deposit_rules`, `tax_payment`). Every payroll run saved before the
upgrade becomes status `imported`: it is kept for history and for the year-to-date wage chain, its
figures do not change, it is never posted to the ledger, and it **cannot be deleted** (the database
refuses). FUTA and SUTA on those runs are 0.00. The migration also binds the eight payroll roles to
the default chart's accounts (Wages, Taxes, Federal Taxes (941/943/944), GA Income Tax, FUTA Payable,
GA SUTA Payable, S-Corp Health Insurance, Co. HSA) when each exists under Payroll Expenses or
Payroll Liabilities, is active and is not already bound; anything else you bind yourself (First use,
step 1).

**The downgrade is refused** while any run is approved or void (or has a journal entry) or any tax
payment exists, because dropping the columns would destroy the links to the ledger. Rolling back after
real use means restoring the `pg_dump` taken below and pinning the previous image tag. A permitted
downgrade (only drafts and imported runs) also unbinds the eight payroll roles, which the earlier
release does not know.

1. **Check for duplicate pay periods first.** The migration adds a unique index that allows one
   non-void run per pay period. The old app refused a second run for a period, but the column was
   never unique in the database, so check:
   ```bash
   docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c \
     "SELECT pay_period, count(*), array_agg(id ORDER BY id) AS run_ids FROM payroll_run GROUP BY pay_period HAVING count(*) > 1"
   ```
   No rows means nothing to do. If a period is listed, **the upgrade fails** (the app does not start,
   because the migration fails) until the duplicate is removed. Delete the extra run in the current
   release (Payroll history, delete), keeping the one that matches what you entered in QBO, and check
   that the later runs' year-to-date wages still follow from it. Run the query again until it returns
   no rows.
2. **List runs for future months or other years, and delete them in the old release.** Only the
   tax year the app calculates (2026) can be saved from now on, and every run saved before the
   upgrade becomes imported, which can never be edited or deleted; an imported run for a month that
   has not happened yet, or for another year, would stay in the history for good and would block
   saving or voiding later months. Run:
   ```bash
   docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c \
     "SELECT id, pay_period FROM payroll_run WHERE pay_period > to_char(now(), 'YYYY-MM') OR pay_period NOT LIKE '2026-%' ORDER BY pay_period"
   ```
   No rows means nothing to do. Delete each listed run in the current release (Payroll history,
   delete) before upgrading.
3. **A payment for a pre-upgrade month is only reported correctly if that month's liability was
   journaled into the ledger first.** The liability report counts, per agency, an opening amount: what
   the payable account holds from entries that are not payroll runs or tax payments (a manual journal
   or an opening balance). Tax payments settle that opening amount first and the approved periods
   after. If the month's liability was never credited to the payable account (the runs saved before
   the upgrade are imported and post nothing), a payment you record for it is applied to the oldest
   approved period instead. Journal it first (Dr the expense, Cr the payable, dated in that month)
   when you want the report to be right.
4. **Before merging or pushing to `master`, stop Watchtower** (it would pull the image as soon
   as the build finishes): `docker compose -f compose.synology.yml stop watchtower`
5. **Back up the database:**
   ```bash
   docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll > payroll_pre_v2_4_$(date +%F).dump
   ```
6. Merge/push, and let the image build (tests must pass first).
7. Pull and start:
   ```bash
   docker compose -f compose.synology.yml pull
   docker compose -f compose.synology.yml up -d --remove-orphans db api   # watchtower stays stopped
   ```
8. **Verify:** `docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c 'select version_num from alembic_version'`
   prints `e5f1b3c9d7a2`, and
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200.
9. **Start Watchtower again:** `docker compose -f compose.synology.yml start watchtower`

First use, as the owner, before saving the next run:

1. **Map the eight payroll roles** in Settings > Ledger: `payroll_wages`, `payroll_tax_expense`,
   `payroll_federal_payable` (federal withholding plus Social Security and Medicare, employee and
   employer), `payroll_ga_wh_payable` (Georgia withholding), `payroll_futa_payable`,
   `payroll_suta_payable`, `payroll_health_payable` and `payroll_hsa_payable`. Approving a run with a
   role unbound is refused and names the role. The roles are **frozen** once any run is approved or
   void or any tax payment exists (drafts and imported runs do not count), so confirm the accounts
   first. **Due to Owner** is also frozen once an approved or void run has paid net pay to it, because
   that run finds its account through the role.
2. **Enter the FUTA and SUTA rates and wage bases** in Settings > Payroll. The page opens on the tax
   engine's year (2026), which is the only year the run service reads, and starts empty. Rates are
   typed as percentages (0.6 for 0.6%). **Saving a run is refused until that year's settings exist.**
   FUTA and SUTA are calculated when the run is saved and stored on it; approving never recalculates,
   so a draft saved with a wrong rate is deleted and saved again.
3. **Confirm the deposit due dates** in Settings > Payroll. They are one set of rules for all years
   (table `payroll_deposit_rules`), not part of the per-year settings. The defaults: Form 941 and
   Georgia DOR monthly, due on the 15th of the month after the pay period; FUTA and Georgia DOL
   quarterly, due on the last day of the month after the quarter (due day 0 means the last day of the
   month). FUTA is legally due quarterly only once the accumulated liability passes $500; the report
   shows the quarterly date regardless. The dates only drive "next due" and "overdue" on the
   liability report.
4. **Run lifecycle.** Saving a run creates a **draft**. On the run's panel the owner approves it (a pay
   date, which cannot be before the first day of the pay period, and net pay from a bank account or
   to Due to Owner), which posts one journal entry dated the
   pay date, or voids an approved run, which posts the reversal. Only a draft can be deleted; approved,
   void and imported runs are kept. These rules keep the year-to-date wages consistent:
   - Only a pay period in the tax year the app calculates (2026) can be saved; another year is
     refused. A calculation that is not saved works for any year.
   - A run cannot be saved while a later-period run that is not void exists in the same tax year.
   - A run cannot be approved while an earlier run of the year is still a draft, or when its
     year-to-date start differs from the end of the previous run if that run is **approved** (delete
     it and save it again). When the previous run is **imported**, any year-to-date start is accepted:
     an imported run cannot be edited or deleted, so its end figure is only a starting point.
   - A run cannot be voided while **any** later non-void run of the year exists: void later approved
     runs first, and delete a later draft (a later draft blocks the void too). A later **imported**
     run blocks it as well, and that cannot be undone, because an imported run can never be deleted.
   - A void run frees its pay period; the same month can be saved and approved again.
5. **Tax payments** (`/payroll/tax-payments`) record money paid to IRS (941), IRS (FUTA), Georgia DOR
   or Georgia DOL: Dr the agency's payable account, Cr the bank. The bank account cannot be one bound
   to a payroll role or to A/R, Undeposited Funds or Due to Owner. A payment may exceed what is owed;
   the difference shows as an overpayment. Penalties and interest are ordinary expenses.
6. **Payroll liability report** (`/reports/payroll-liability`, with CSV and PDF): per agency, the
   ledger balance of the payable account, each pay period's liability, paid and unpaid amounts with
   its due date, and an **Other** line for whatever the periods do not explain (an opening balance, a
   manual journal, an overpayment). Before the first period sits an **opening amount**: the net credit
   on the payable account from entries that are neither payroll runs nor tax payments, as of the
   date. Payments settle the opening amount first, then the periods oldest first; what is left of the
   opening amount stays in the Other line. A run or tax payment voided after the as-of date still
   counts as of that date; periods with no liability for an agency are not listed; the Other line is
   always shown. The CSV has one row per line with the columns Agency, Row (Balance, Period, Other or
   Next due), Pay period, Due date, Liability, Paid, Unpaid and Overdue: the balance is in Liability,
   Other in Unpaid (so the Unpaid column adds up to the balance) and the next due date in Due date.
7. Paying the health insurer and funding the HSA are not part of a run: record them with a Transfer
   from the bank to the health payable or HSA payable account, or as an expense when the premium is
   paid directly.

Known limitation: net pay to Due to Owner. When a run's net pay is posted to **Due to Owner** instead of
a bank account, the Owner page and the Owner reimbursement report show a difference equal to the unpaid
net pay (the ledger balance includes it, the open owner-paid expenses do not) until it is settled.
There is no settlement document yet. The only way to settle it today is a manual journal entry,
Dr Due to Owner / Cr the bank account, dated when you pay yourself. (`seed-demo` does this for its June
run.)

Other things to know:

- The QuickBooks entries panel and CSV export on the payroll page are unchanged. The QuickBooks account
  names (`ACCT_*`, edited on the payroll admin page) are a separate setting from the ledger roles and
  stay until M7; keep the two in step by hand.
- Approved runs made during the parallel run are not yet reconciled with the QBO general-ledger import;
  M6 must decide whether to void them before the import.
- The JSON API is unchanged except that `DELETE /payroll/api/v1/payroll/runs/{id}` answers 409 for a
  run that is not a draft, and run responses gain `status`, `payDate`, `entryId`, `futa`, `suta` and
  the FUTA and SUTA wage fields. Approve, void and tax payments are pages only.
- `seed-demo` now also adds a year of payroll through the real services (twelve runs, one paid to Due
  to Owner and settled by a journal, March voided and approved again, and tax payments). The demo year
  still carries its older manual "Owner payroll" journal entries as well, so the demo Profit and Loss
  shows wage expense from both. Never run it against real books.

## Upgrading to v2.3.0 (expenses, M3)

The M3 migration (`d4e0a2b8c6f1`) is **additive**: it creates only new tables (vendors,
expenses and lines, transfers, reimbursements, attachments) and their integrity triggers, and
changes no existing data. Its **downgrade drops all expenses data** (the tables go; the receipt
files on disk are not touched), so rolling back means restoring the `pg_dump` taken below and
pinning the previous image tag. Receipts are files outside the database: back them up too.

1. **Before merging or pushing to `master`, stop Watchtower** (it would pull the image as soon
   as the build finishes): `docker compose -f compose.synology.yml stop watchtower`
2. **Back up the database** (see the Backups section for the nightly job):
   ```bash
   docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll > payroll_pre_v2_3_$(date +%F).dump
   ```
3. **Create the attachments folder and make it writable by the app.** The new
   `compose.synology.yml` bind-mounts `/volume1/docker/payroll/attachments` at
   `/data/attachments`. The container runs as the non-root user `appuser` (uid **10001**, the
   Dockerfile's `UID` argument), which must be able to write there:
   ```bash
   mkdir -p /volume1/docker/payroll/attachments
   chown 10001:0 /volume1/docker/payroll/attachments   # run as root / with sudo over SSH
   chmod 700 /volume1/docker/payroll/attachments
   ```
   If uploads fail with a server error after the upgrade, this ownership is the first thing to
   check (`ls -ln /volume1/docker/payroll/attachments` should show owner 10001). Replace the
   compose file with the new one before starting so the mount exists; otherwise receipts would
   land in the container's own filesystem and vanish when Watchtower recreates it. The
   development `compose.yml` uses a named volume `attachments` instead (the image creates the
   directory owned by `appuser`, so no chown is needed there).
4. Merge/push, and let the image build (tests must pass first).
5. Pull and start:
   ```bash
   docker compose -f compose.synology.yml pull
   docker compose -f compose.synology.yml up -d --remove-orphans db api   # watchtower stays stopped
   ```
6. **Verify:** `docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c 'select version_num from alembic_version'`
   prints `d4e0a2b8c6f1`,
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200,
   and `docker compose -f compose.synology.yml exec api ls -ld /data/attachments` shows the
   directory owned by `appuser` (or uid 10001).
7. **Start Watchtower again:** `docker compose -f compose.synology.yml start watchtower`

First use, as the owner:

1. **Bind Due to Owner.** Open Settings > Ledger and check that **Due to Owner** points at your
   liability account. The default chart (`seed-chart`) already binds it; a chart imported from QBO
   does not, so bind it yourself. Posting an owner-paid expense with the role unbound is refused
   with a message telling you to bind it. The role is **frozen** after the first owner-paid
   expense or reimbursement is recorded (voided ones count) or a payroll run pays net pay to it
   (from v2.4.0), so choose the account first.
2. Add vendors (or type a free-text payee on each expense), then expenses. Pick **Owner
   (personal card)** as the source for something you paid personally; it credits Due to Owner
   until you reimburse it from the Owner page (whole expenses only, paid from an asset account).
3. Transfers (credit card payments, moves between accounts) are on the Transfers page. Bank
   reconciliation is not part of this release.
4. `seed-demo` now also creates demo vendors, expenses, a reimbursement and a transfer; never run
   it against real books.

Receipts:

- Allowed types are PDF, PNG, JPEG and HEIC (decided from the file contents), 10 MB per file,
  10 files and 50 MB per expense. Receipts are append-only: there is no way to remove one (add a
  memo to explain a wrong upload). They are stored under `ATTACHMENT_DIR` (default
  `/data/attachments`, set in `.env.example`) as `yyyy/mm/<random name>`; the database holds the
  original filename, type, size and SHA-256.
- **Backup and restore drill:** `/data/attachments` (on Synology
  `/volume1/docker/payroll/attachments`) is **not** inside the database dump. Add it to the
  nightly job's Hyper Backup selection beside the dumps, and when you test a restore, restore the
  dump **and** the attachments folder together, then fix ownership (files restored by root with
  mode 0600 are unreadable to the app otherwise):
  `chown -R 10001:0 /volume1/docker/payroll/attachments && chmod -R u+rwX,go-rwx /volume1/docker/payroll/attachments`.
  Then open an expense with a receipt and download it. A database restored without the files shows receipts that
  fail to download; files without the database are orphans.
- `MAX_UPLOAD_BODY_BYTES` (default 64 MiB) is the largest request body on the two upload routes
  only (`POST /expenses/new` and `POST /expenses/{id}/receipts`), and the larger ceiling applies
  only to a signed-in owner; everyone else on those routes, and every other route for every
  user, is held to `MAX_REQUEST_BODY_BYTES` (1 MiB). A body beyond the limit is answered with
  422 on the upload routes and 413 elsewhere. Behind the DSM reverse proxy also check its own
  body-size limit if large uploads fail.
- Downloads need sign-in (any role) and are sent as attachments with `nosniff` and
  `Cache-Control: private, no-store`.

Correcting a document in a locked period (invoice, payment, expense, transfer, reimbursement,
payroll run, tax payment):
move the lock back (Settings > Ledger, password required), void or replace the document, then
lock again. A reversing manual journal is not a correction, because the document stays live.

## Upgrading to v2.2.0 (receivables, M2)

The M2 migration is **additive**: it adds the receivables tables (customers, items, invoices
and lines, payments and applications, deposits, customer refunds), their integrity triggers
and one settings row (`receivables_settings`), and changes no existing data. The ledger,
payroll runs, users and the audit log are untouched. A `pg_dump` first is still the rule before
any upgrade. Rolling back means restoring that dump and pinning the previous image tag.

1. **Before merging or pushing to `master`, stop Watchtower** (it would pull the image as soon
   as the build finishes): `docker compose -f compose.synology.yml stop watchtower`
2. **Back up the database:**
   ```bash
   docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll > payroll_pre_v2_2_$(date +%F).dump
   ```
3. Merge/push, and let the image build (tests must pass first).
4. Pull and start:
   ```bash
   docker compose -f compose.synology.yml pull
   docker compose -f compose.synology.yml up -d --remove-orphans db api   # watchtower stays stopped
   ```
5. **Verify:** `docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c 'select version_num from alembic_version'`
   prints `c3d9f1a7e2b4`, and
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200.
6. **Start Watchtower again:** `docker compose -f compose.synology.yml start watchtower`

First use, as the owner:

1. In Settings > Ledger bind **Accounts receivable** and **Undeposited Funds** to the right
   accounts. Do this before the first invoice or payment: once an invoice, payment or refund has
   been recorded (voided ones count) the A/R binding is frozen (Undeposited Funds can still move).
   Warning: rebinding Undeposited Funds while payments are still undeposited strands them (they
   vanish from the deposit page and a deposit refuses them), so deposit them first.
2. In Settings > Receivables set the **next invoice number** to continue QBO's sequence (it must
   be above every number already used; numbers of voided invoices are never reused, so a void
   leaves a gap), and enter the **company details** (name, address, remit-to, footer) printed on
   invoice PDFs.
3. Add customers and items, then invoices, payments and deposits. `seed-demo` now also creates
   demo receivables; never run it against real books.

Voiding an invoice, payment, deposit or refund needs a void date on or after the document's own date (an earlier date is refused).

Cash-basis note: reports default to cash basis, where income appears when an invoice is paid
(on the later of the payment and invoice dates). Applications cannot be created on or before the
period-lock date, and a payment cannot be voided into a locked period; fix a misapplied payment
by voiding it and recording it again. The ledger also refuses to void a document whose own
date is on or before the lock: to correct it, move the lock back (password-gated), void or
replace the document, and lock again (see Locking a closed year). Do not use a manual journal
for this: the receivables document stays live in the subledger, so the A/R aging and the
customer balances would not change.

## Upgrading to v2.1.0 (ledger and reports, M1)

The M1 migration is **additive**: it creates the ledger tables (accounts, journal entries and
lines, period lock, system roles) and their integrity triggers, and converts or rewrites no
existing data. Payroll runs, users and the audit log are untouched. A `pg_dump` first
is still the rule before any upgrade. Rolling back means restoring that dump and pinning
the previous image tag.

Order matters (Watchtower would otherwise pull the new image as soon as the build finishes):

1. **Before merging or pushing to `master`, pause Watchtower:**
   `docker compose -f compose.synology.yml stop watchtower`
2. **Back up the database:**
   ```bash
   docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll > payroll_pre_v2_1_$(date +%F).dump
   ```
3. Merge/push, and let the image build (tests must pass first).
4. Pull and start:
   ```bash
   docker compose -f compose.synology.yml pull
   docker compose -f compose.synology.yml up -d --remove-orphans db api   # watchtower stays stopped
   ```
5. **Verify:** the schema is at the release head,
   `docker compose -f compose.synology.yml exec db psql -U payroll -d payroll -c 'select version_num from alembic_version'`
   prints `a1c5e8d3b7f2`, and
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200.
6. **Re-enable Watchtower:** `docker compose -f compose.synology.yml start watchtower`

After the upgrade, as the owner:

1. **Seed the chart of accounts** (idempotent, safe to repeat; creates only what is missing):
   ```bash
   docker compose -f compose.synology.yml exec api python -m payroll.ledger.cli seed-chart --email you@example.com
   ```
   (Outside Docker: `uv run python -m payroll.ledger.cli seed-chart --email you@example.com`.)
   Rename or add accounts afterwards on the Accounts page; check Settings > Ledger that the
   system roles (A/R, Undeposited Funds, Due to Owner, Retained Earnings, Opening Balance
   Equity) point at the right accounts.
   **Or import your QBO chart instead of the default one.** In QBO export the chart (Settings >
   Chart of accounts > Export) or the Account List report as CSV, then use Accounts > Import
   from QBO. You get a preview first and tick the accounts you want (unticking a parent
   unticks its sub-accounts); accounts already in the app (same number, or same name under the
   same parent) are skipped and never changed, rows with errors are listed but never imported,
   and balances are ignored. Nothing is created until you confirm. Sub-accounts come from `Parent:Child` names. Account
   numbers QBO decorated (for example `2100*OE`) are kept as exported and flagged; fix them on
   the Accounts page. Import before binding the system roles, then bind them in Settings > Ledger.
2. **Do not run `seed-demo` against real books.** It posts a fictitious 2026 year; it refuses
   when any journal entry exists, and exists only to explore the reports on a scratch database.
3. Set `COMPANY_TIMEZONE` in `.env` if the company is not on Eastern time (default
   `America/New_York`); it decides what "today" and "this month" mean in reports.

### Locking a closed year

The period lock (Settings > Ledger, or `PUT /payroll/api/v1/ledger/period-lock`) blocks
posting into it and voiding entries dated in it. A manual journal entry in a locked period can be
corrected with a reversal dated after the lock; an invoice, payment, deposit, refund, expense,
transfer, reimbursement, payroll run or tax payment in a locked period cannot (a reversing journal would leave the document live in its subledger), so
move the lock back instead. Lock a year once it
is final: after the CPA has the return figures, the year-end trial balance is saved
(CSV/PDF from Reports), and any year-end close or adjusting entries are posted. Set the lock to
the last day of that year (for example `2026-12-31`). Changing or clearing the lock needs the
owner's password again and is audited. Dates must lie between 1900-01-01 and 2200-12-31. To
correct a locked year, clear or move the lock back, post the correction, then lock again.

## ENCRYPTION_KEY

Required; the app and compose refuse to start without it. Generate and store it:

```bash
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

Put it in `/volume1/docker/payroll/.env` as `ENCRYPTION_KEY=...` (compose reads it
automatically), `chmod 600` the file, and keep a copy in your password manager. Back it
up separately from the database dumps.

Losing or changing the key makes every stored TOTP secret **and recovery code**
unreadable: every user must then re-enrol MFA, done per user with `reset-owner`.
Treat rotation as a deliberate reset of all MFA.

## Ports, TLS and the reverse proxy

- The app listens on 9000 in the container. `compose.synology.yml` publishes it as host
  port **8070** (the old UI port) **on 127.0.0.1 only** (`127.0.0.1:8070:9000`);
  `compose.yml` (dev) publishes 9000.
- The DSM reverse proxy must target `http://localhost:8070`. Plain-http access from the
  LAN to `http://<nas>:8070` is intentionally not possible (it would bypass HTTPS and
  the `Secure` cookie); always use the HTTPS hostname.
- The database is not published on the host in either file; only `api` can reach it.
- The session cookie is `Secure`, so the site must be served over HTTPS. In DSM:
  Control Panel > Login Portal > Advanced > Reverse Proxy: source HTTPS on your hostname
  (with a certificate), destination `http://localhost:8070`. WebSockets are not needed.
- `SESSION_COOKIE_SECURE=false` is only for plain-http local development.

## First run and account recovery

Run these from the stack directory; they need an interactive terminal (no `-T`).

```bash
docker compose -f compose.synology.yml exec api python -m payroll.auth.cli create-owner --email you@example.com --name "Your Name"
docker compose -f compose.synology.yml exec api python -m payroll.auth.cli create-user --email acct@example.com --role accountant
docker compose -f compose.synology.yml exec api python -m payroll.auth.cli reset-owner --email you@example.com
```

`reset-owner` (any user) is the recovery path for a lost password or authenticator: it sets
a new password, clears MFA and recovery codes, unlocks the account and signs out all sessions.

**Log in and enrol TOTP immediately after `create-owner` / `create-user` / `reset-owner`.**
Until the first login completes enrolment, the account has only a password: anyone who
knows (or guesses) it can enrol *their own* authenticator and lock you out of MFA.

MFA enrolment: the first login with the password shows a QR code for the authenticator
app. Reloading the enrolment page issues a **new** QR code (the old one stops working),
so scan the one currently displayed and save the recovery codes.

## Security notes

- `audit_log` is append-only, enforced by database triggers. In a hardened setup the
  database role the app connects as should **not own** the table (owners can drop triggers).
- `/payroll/api/v1/docs`, `/payroll/api/v1/redoc` and `/payroll/api/v1/openapi.json` are disabled unless `DEBUG_MODE=true`.
- Request bodies larger than `MAX_REQUEST_BODY_BYTES` (default 1 MiB) get HTTP 413.
  The two receipt upload routes (`POST /expenses/new`, `POST /expenses/{id}/receipts`) allow a
  signed-in owner up to `MAX_UPLOAD_BODY_BYTES` (default 64 MiB) and answer HTTP 422 beyond the
  limit that applies (see Upgrading to v2.3.0).
- **Never enable `DEBUG_MODE=true` in production.** Besides the API docs, it turns on
  SQLAlchemy SQL echo, which logs every statement **with its bound parameters**: password
  hashes, encrypted TOTP secrets, session token hashes and audit data end up in the logs.
- At most `MAX_CONCURRENT_PASSWORD_HASHES` (default 4) Argon2 hashes/verifies run at once
  per worker process (~64 MiB each); further login attempts wait for a slot. No database
  transaction is held open while a password is hashed.
- Failed logins are audited as `auth.login.failure`. The attempted email is recorded only
  when it matches a user; for an unknown email only `reason: unknown_email` and `email_fp`
  (a 16-hex-char keyed HMAC fingerprint, the same for repeated probes of one address) are
  stored, so a password typed into the email field never lands in the append-only log.
- Postgres credentials in the compose files are `payroll`/`payroll` and the DB is internal
  only. On Synology, still change them: edit `POSTGRES_PASSWORD` and the password in
  `DATABASE_URL`, and set the new password in the existing database
  (`ALTER USER payroll PASSWORD '...';`) before restarting, since the data directory
  keeps the old password.

## Known gaps

- **SEC6 (login rate limiting) is partial.** Per-account lockout exists
  (`MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES`, doubling on repeat lockouts), but there is
  **no per-IP rate limit**. Behind the DSM reverse proxy the client address the app sees
  is the proxy / Docker gateway, so the IPs in `audit_log` and `auth_session` rows are the
  proxy's, not the real client's.
- Anyone who knows an account's email can keep that account locked by repeatedly
  failing its password. Recovery: wait out the lockout, or run `reset-owner` for the
  account (it unlocks it and signs out every session).
- Unknown-email probes are audited (`auth.login.failure`, `reason: unknown_email`, with a
  keyed fingerprint instead of the text); they cannot lock anything, but each still costs
  one Argon2 verify (capped per process, see Security notes).

## Backups

Automated backups are **not** part of M0 (planned for M7). Until then, schedule a nightly
dump in DSM Task Scheduler (User-defined script, run as root, nightly):

```bash
cd /volume1/docker/payroll && \
docker compose -f compose.synology.yml exec -T db pg_dump -U payroll -Fc payroll \
  > /volume1/backups/payroll_$(date +%F).dump && \
find /volume1/backups -name 'payroll_*.dump' -mtime +30 -delete
```

Include `/volume1/backups` in Hyper Backup, and test a restore occasionally.

From v2.3.0 receipts live in `/volume1/docker/payroll/attachments` (mounted at
`/data/attachments`); include that folder in Hyper Backup too, and restore it together with the
database dump (see Upgrading to v2.3.0). After restoring the folder, run
`chown -R 10001:0 /volume1/docker/payroll/attachments && chmod -R u+rwX,go-rwx /volume1/docker/payroll/attachments`
so the app can read the files.

## System of record

QuickBooks Online remains the system of record until cutover. Do not delete anything
there. From v2.4.0 an approved payroll run also posts to this app's ledger, and the app still
produces the QuickBooks journal entries (the QuickBooks entries panel and CSV on the payroll page)
to enter into QBO.
