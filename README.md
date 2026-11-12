# DCI Payroll

S-Corp owner payroll calculator with QuickBooks journal entries.

**Georgia · Single Filing · Monthly Payroll · Tax Year 2026**

One FastAPI application serves both the web UI (Jinja2 + HTMX) and the JSON API
from a single container, backed by PostgreSQL. Money is handled as `Decimal`
(stored as `NUMERIC`), secrets at rest are encrypted with AES-256-GCM, and every
state change is written to an append-only audit log.

## Books (M1): ledger and reports

Release 2.1.0 adds a double-entry ledger next to the payroll calculator. Money is
`Decimal`/`NUMERIC` everywhere; balances are always computed from journal lines and
never stored. Only the owner can change the books; accountants and viewers read
everything, including every report.

| Page | Purpose |
|------|---------|
| `/accounts` | Chart of accounts: add, edit, deactivate, import from a QBO CSV export (`/accounts/import`), per-account register |
| `/journal` | Manual journal entries (post, reverse, void) and the entry detail |
| `/reports` | Profit and Loss, Balance Sheet, Trial Balance, General Ledger, Journal, with presets (this/last month, quarter, year, YTD, trailing 12), custom ranges, comparison columns and drill-down |
| `/settings/ledger` | System account roles and the period lock (changing it needs your password) |

Every report has CSV and PDF export (`.csv` / `.pdf` on the report URL). PDFs are
rendered by WeasyPrint, which needs the Pango native libraries (`libpango-1.0-0`,
`libpangoft2-1.0-0`, `libharfbuzz-subset0`; installed in the Docker image and CI). On macOS:
`brew install pango`. Without them the PDF routes fail but everything else works (the PDF
tests skip).

"Today" and period boundaries use `COMPANY_TIMEZONE` (default `America/New_York`).
Reports default to cash basis with an accrual toggle (see Receivables below for what each means).

### Ledger CLI

```bash
uv run python -m payroll.ledger.cli seed-chart --email you@example.com     # default chart, idempotent
uv run python -m payroll.ledger.cli seed-demo --email you@example.com      # fictitious 2026 year
# in Docker:
docker compose exec api python -m payroll.ledger.cli seed-chart --email you@example.com
```

The actor is an existing owner (looked up by email; nothing is prompted). `seed-demo`
posts about forty balanced 2026 entries (with reversals and a void) and a set of receivables
(four customers, three items, invoices that are open, partial, paid, overdue and void,
payments, one overpayment, a deposit and a refund) so every report and the aging page have
something to show. It refuses to run when any journal entry exists unless `--force`, and
even then only adds: never run it against real books. It also adds demo expenses and, from 2.4.0, a
year of payroll saved and approved through the real services (twelve runs, one paid to Due to Owner and
settled by a journal, March voided and approved again, and tax payments). The demo year still carries its
older manual "Owner payroll" journal entries, so the demo Profit and Loss shows wage expense from both.

## Receivables (M2): customers to deposits

Release 2.2.0 adds receivables on top of the ledger. Only the owner can create or change
anything; accountants and viewers read every page and report.

| Page | Purpose |
|------|---------|
| `/customers` | Customers (name, contact, billing address, net-days terms); the detail page shows invoices, payments, credit and refunds |
| `/items` | Products and services with a default price and the income account they post to |
| `/invoices` | Draft, issue (assigns the number and posts to the ledger), edit while unpaid, void; send-ready PDF at `/invoices/{id}.pdf` |
| `/payments` | Record a customer payment (to Undeposited Funds or straight to a bank account), apply it to invoices, void it |
| `/deposits` | Group payments held in Undeposited Funds into one bank deposit |
| `/reports/ar-aging` | A/R Aging with the open-invoice detail and customer credit; CSV and PDF |
| `/settings/receivables` | Company details printed on invoices and the next invoice number |

Workflow: **customers, then items, then invoices, then payments, then deposits.** Create the
customer, define the items (each ties to an income account), draft and issue an invoice,
record the payment when it arrives (apply it to invoices now or later), and group undeposited
payments into a deposit when you take them to the bank. Money received but not applied stays in
A/R as customer credit and can be applied later or refunded (from the customer page).

**Cash vs accrual.** Accrual recognizes income when an invoice is issued. Cash (the default on
every report) recognizes it when the invoice is paid: each application of a payment to an
invoice adds income on the later of the payment and invoice dates, split across the invoice's
income accounts. Unapplied money is not income. Both bases are tested to balance; the Balance
Sheet and Trial Balance always balance. On cash basis an income row drills down to the
applications behind it.

Before the first invoice: bind Accounts receivable and Undeposited Funds in Settings > Ledger
(the A/R binding is frozen once any invoice, payment or refund has been recorded), set the next invoice number in
Settings > Receivables to continue your QBO sequence, and enter the company details. Do not rebind
Undeposited Funds while payments are still undeposited: they would be stranded (hidden from the
deposit page, refused by a deposit), so deposit them first.
There is no JSON API for receivables documents; the A/R Aging CSV/PDF are also exposed
read-only under the report API.

## Expenses (M3): vendors, receipts, transfers, reimbursements

Release 2.3.0 adds expenses on top of the ledger. Only the owner can create or change anything;
accountants and viewers read every page, report and receipt.

| Page | Purpose |
|------|---------|
| `/vendors` | Vendors (name, note); deactivate rather than delete |
| `/expenses` | Expenses with split lines (one expense account per line), paid from an account or personally by the owner; vendor or free-text payee; receipts (PDF, PNG, JPEG, HEIC); void with a date; CSV export |
| `/transfers` | Move money between two asset or liability accounts (for example a credit card payment); void |
| `/owner` | The Owner page: what the company owes you (Due to Owner), your open personal-card expenses, and Reimburse (whole expenses, paid from an asset account) with history and void |
| `/reports/owner-reimbursement` | Owed balance as of a date, open expenses and reimbursements in a range; CSV and PDF |

Receipts are append-only (they cannot be removed; explain a wrong upload in the memo), are
checked by file contents, and are stored under `ATTACHMENT_DIR` (default `/data/attachments`),
which must be a mounted volume in production and part of your backups. Before the first
owner-paid expense, bind **Due to Owner** in Settings > Ledger (frozen after first use). Bank
reconciliation is not part of this release. See [docs/RUNBOOK.md](docs/RUNBOOK.md)
("Upgrading to v2.3.0").

## Payroll to the ledger (M4): runs, tax payments, liabilities

Release 2.4.0 makes the payroll calculator post to the ledger. Only the owner can create or change
anything; accountants and viewers read every page and report.

| Page | Purpose |
|------|---------|
| `/` | The calculator. Save creates a **draft**; the run's panel shows the ledger entry Approve would post, an Approve form (pay date, net pay from a bank account or to Due to Owner) and, once approved, a link to the entry and a Void form. The QuickBooks entries and CSV are unchanged. |
| `/payroll/tax-payments` | Record money paid to IRS (941), IRS (FUTA), Georgia DOR or Georgia DOL from a bank account; void |
| `/reports/payroll-liability` | Per agency: ledger balance, unpaid amount by pay period with due dates (payments settle an opening amount, journaled by hand before the first run, first), and an Other line; CSV (columns Agency, Row, Pay period, Due date, Liability, Paid, Unpaid, Overdue) and PDF |
| `/settings/payroll` | FUTA and SUTA rates (typed as percentages) and wage bases for the tax year, and the deposit due-date rules |

A run is `draft`, `approved`, `void` or `imported` (runs saved before 2.4.0: kept for history, never
posted, cannot be deleted). Only a draft can be deleted. Approving posts one balanced entry and the run's
figures never change afterwards; voiding posts the reversal. FUTA and SUTA are calculated when the run is
saved, so enter the year's rates in Settings > Payroll first. Only the engine's tax year (2026)
can be saved. The year-to-date wages must chain: a run cannot be saved before a later non-void run,
approved after an earlier draft, approved when it starts at a different year-to-date figure than an
earlier approved run ends at (an imported run before it is a manual starting point and imposes no
figure), or voided while any later non-void run (imported included) exists. Eight payroll roles (for example `payroll_ga_wh_payable`) point the postings at your
accounts in Settings > Ledger; they are frozen once any run is approved or void or any tax payment exists. Due to Owner is also
frozen once a run has paid net pay to it.

Known limitation: when net pay is posted to Due to Owner, the Owner page shows a difference equal to the
unpaid net pay until you settle it, and for now the only way is a manual journal (Dr Due to Owner / Cr
bank). The QuickBooks account names in the payroll config stay until M7. See
[docs/RUNBOOK.md](docs/RUNBOOK.md) ("Upgrading to v2.4.0").

## Authentication and roles

Every page and API route needs a signed-in user; only `/login`, the MFA pages,
`/payroll/api/v1/healthcheck` and `/static` are public.

- Sign-in is email + password (Argon2id) followed by TOTP two-factor. The first
  login with the password shows a QR code to enrol an authenticator app and a set
  of one-time recovery codes.
- Sessions are server-side, with an idle timeout and an absolute lifetime, in a
  `Secure`, `HttpOnly`, `SameSite` cookie (so production must be served over HTTPS).
  Repeated failed logins lock the account temporarily.
- Roles: `owner` (can change data and manage users), `accountant` and `viewer`
  (read only).
- The interactive API docs (`/payroll/api/v1/docs`, `/payroll/api/v1/redoc`, `/payroll/api/v1/openapi.json`) are
  disabled unless `DEBUG_MODE=true`, and then only for a signed-in user.
- API money values are JSON strings (e.g. `"12345.67"`), not numbers.

## Quick start (local)

You need Python 3.14, [uv](https://docs.astral.sh/uv/), Docker, and a Postgres database.
PDF export uses WeasyPrint, which needs the native Pango libraries: `brew install pango` on macOS,
`apt-get install libpango-1.0-0 libpangoft2-1.0-0` on Debian/Ubuntu (the Docker image and CI install them;
without them the PDF tests skip and the CSV export is unaffected).

```bash
uv sync
cp .env.example .env     # then set ENCRYPTION_KEY (see Configuration); SESSION_COOKIE_SECURE=true (the default in this file) breaks login over plain http, so set it to false locally
# Throwaway Postgres on host port 5433 (tmpfs, NOT for real data):
docker compose -f compose.test.yml up -d db
export DATABASE_URL=postgresql://payroll:payroll@localhost:5433/payroll_test
uv run uvicorn payroll.app:app --reload --port 9000
```

Migrations are applied automatically on startup. Then create the owner (below) and
sign in at **http://localhost:9000/login**.

### First run: create the owner

There are no default credentials. The password is prompted for twice and is never
taken from argv or the environment:

```bash
uv run python -m payroll.auth.cli create-owner --email you@example.com --name "Your Name"
# In Docker: docker compose exec api python -m payroll.auth.cli create-owner --email ...
uv run python -m payroll.auth.cli create-user --email a@example.com --role accountant
uv run python -m payroll.auth.cli reset-owner --email you@example.com  # lost password/authenticator
```

The CLI needs an interactive terminal (`docker compose exec api ...`, not `-T`) and
applies pending migrations first. `create-owner` refuses if any user exists unless
`--force-additional`. `reset-owner` (works for any user) sets a new password, clears
MFA and recovery codes, unlocks the account and signs out all sessions; MFA is
re-enrolled at the next login. **Sign in and enrol TOTP right after `create-owner` /
`reset-owner`:** until then anyone with the password can enrol their own authenticator.

## Docker

```bash
export ENCRYPTION_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
docker compose up --build        # app on http://localhost:9000 (plain http; cookie not Secure by default)
```

> If you copied `.env.example` to `.env`, it sets `SESSION_COOKIE_SECURE=true`, which overrides the
> dev default and breaks login over plain http. Set `SESSION_COOKIE_SECURE=false` in `.env` for local http.

`compose.yml` is the local stack; `compose.synology.yml` is the NAS deployment
(pulls the GHCR image, published on `127.0.0.1:8070` only, with Watchtower; the DSM
reverse proxy must target `http://localhost:8070`, and plain-http LAN access to `:8070`
is intentionally not possible). Postgres is
never published on the host. Compose refuses to start if `ENCRYPTION_KEY` is unset.
See [docs/RUNBOOK.md](docs/RUNBOOK.md) for deployment, backups and recovery.

## Configuration

All settings come from environment variables or `.env` (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://payroll:payroll@localhost:5432/payroll` | PostgreSQL connection (the only supported database) |
| `ENCRYPTION_KEY` | none (required) | base64 of 32 random bytes; encrypts TOTP secrets and recovery codes |
| `SESSION_COOKIE_NAME` | `dci_session` | Session cookie name |
| `SESSION_COOKIE_SECURE` | `true` | Set `false` only for plain-http local development |
| `SESSION_IDLE_MINUTES` / `SESSION_ABSOLUTE_HOURS` | `30` / `12` | Session timeouts |
| `MAX_FAILED_LOGINS` / `LOCKOUT_MINUTES` | `5` / `15` | Login lockout |
| `TOTP_ISSUER` | `DCI Books` | Name shown in the authenticator app |
| `MAX_REQUEST_BODY_BYTES` | `1048576` | Larger request bodies get HTTP 413 |
| `MAX_UPLOAD_BODY_BYTES` | `67108864` | Largest receipt upload body (expense create / add receipts), for a signed-in owner on those two routes only; larger gets HTTP 422 |
| `ATTACHMENT_DIR` | `/data/attachments` | Where receipts are stored; mount a volume here (compose does) and back it up |
| `MAX_CONCURRENT_PASSWORD_HASHES` | `4` | Argon2 hashes/verifies at once per worker (~64 MiB each); extra logins queue |
| `COMPANY_TIMEZONE` | `America/New_York` | IANA timezone that defines "today" and period boundaries for reports |
| `DEBUG_MODE` | `false` | Enables debug logging, the API docs routes and SQL echo **with bound parameters** (hashes, tokens): never in production |
| `API_LOG_TYPE` | `standard` | `standard` or `json` |
| `WORKERS` | `2` | Gunicorn workers |
| `ACCT_*` | See `.env.example` | QuickBooks account names |

## Project layout

```
payroll/
├── payroll/
│   ├── __init__.py          Settings (Pydantic Settings)
│   ├── app.py               FastAPI app + lifespan (runs migrations on startup)
│   ├── auth/                Users, sessions, MFA, roles, owner CLI
│   ├── audit/               Append-only audit log
│   ├── common/              Shared infrastructure: money (Decimal), crypto, middleware, deps
│   ├── frontend/            Web UI: templates, static files, in-process routes
│   ├── ledger/              Chart of accounts, journal entries, period lock, ledger CLI
│   ├── receivables/         Customers, items, invoices, payments, deposits, refunds, A/R aging
│   ├── expenses/            Vendors, expenses, receipts, transfers, owner reimbursements
│   ├── reports/             Trial balance, P&L, balance sheet, GL, journal; CSV/PDF export
│   ├── tax/                 Pure calculation engine (no I/O)
│   │   ├── constants.py     ← Update tax rates here each January
│   │   ├── engine.py        calc_payroll() — all withholding math
│   │   └── unemployment.py  calc_unemployment() — FUTA and SUTA
│   ├── payroll_run/         Payroll domain (models, schemas, services, API routes, run posting, tax payments, liability report)
│   ├── migrations/          Alembic runner and versions
│   ├── exceptions.py, logger_conf.py, gunicorn_config.py
├── tests/                   pytest suite (needs Postgres)
├── docs/                    SPEC.md, DESIGN.md, RUNBOOK.md
├── compose.yml              Local stack (app + Postgres)
├── compose.synology.yml     NAS deployment (GHCR image + Watchtower)
├── compose.test.yml         Throwaway Postgres for tests (port 5433)
├── Dockerfile
└── .env.example
```

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/payroll/api/v1/payroll/runs/calculate` | Calculate payroll + get journal entries |
| `GET` | `/payroll/api/v1/payroll/runs` | List saved payroll history |
| `GET` | `/payroll/api/v1/payroll/runs/{id}` | Get a saved run with full journal entries |
| `DELETE` | `/payroll/api/v1/payroll/runs/{id}` | Delete a draft run (owner); approved, void and imported runs are refused (409) |
| `GET` | `/payroll/api/v1/payroll/tax-constants` | View current tax year rates |
| `GET` | `/payroll/api/v1/healthcheck` | Health check (public) |
| `GET` `POST` `PATCH` | `/payroll/api/v1/ledger/accounts[...]` | Chart of accounts (writes: owner) |
| `GET` `POST` | `/payroll/api/v1/ledger/journal-entries[...]` | Journal entries; `/reverse` and `/void` (writes: owner) |
| `GET` `PUT` | `/payroll/api/v1/ledger/period-lock` | Period lock (PUT: owner, needs password) |
| `GET` | `/payroll/api/v1/ledger/reports/{trial-balance,profit-and-loss,balance-sheet,general-ledger,journal}` | Reports as JSON; add `.csv` / `.pdf` for export |

## Development and tests

The tests need PostgreSQL. A throwaway one is defined in `compose.test.yml`
(host port 5433, data in tmpfs):

```bash
docker compose -f compose.test.yml up -d db
uv sync
uv run pytest            # full run; coverage floor enforced (--cov-fail-under in pyproject.toml)
uv run pytest tests/test_x.py -k name --no-cov   # partial runs MUST pass --no-cov or the floor fails the exit code
uv run ruff check .
uv run ruff format .
uv run pre-commit install
```

CI (`.github/workflows/test.yml`) runs lint and tests on every push and pull
request; the image build (`build.yml`) re-runs them and only builds and pushes to
GHCR when they pass.

## Schema migrations (Alembic)

Migrations are applied on app startup (startup aborts if one fails). They are written
by hand as files under `payroll/migrations/versions/` (the `sqlalchemy.url` is set only
by the runner, so the plain `alembic` command line is not configured for this repo).
After adding one, update `_HEAD_REVISION` in `payroll/migrations/alembic_runner.py`.
To apply or roll back manually:

```bash
uv run python -m payroll.migrations.alembic_runner upgrade            # to head
uv run python -m payroll.migrations.alembic_runner downgrade <revision>
```

## Updating tax constants

Edit `payroll/tax/constants.py` each January with the new rates from:
- **IRS Pub 15-T** (federal brackets): https://www.irs.gov/pub/irs-pdf/p15t.pdf
- **Social Security wage base**: https://www.ssa.gov/oact/cola/cbb.html
- **Georgia DOR** (GA flat rate): https://dor.georgia.gov

---

> ⚠️ Tax tables are based on IRS Pub 15-T 2025 and Georgia DOR 2026 rates.
> Verify annually. This is not tax advice — consult your CPA.
