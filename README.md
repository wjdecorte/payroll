# DCI Payroll

S-Corp owner payroll calculator with QuickBooks journal entries.

**Georgia · Single Filing · Monthly Payroll · Tax Year 2026**

One FastAPI application serves both the web UI (Jinja2 + HTMX) and the JSON API
from a single container, backed by PostgreSQL. Money is handled as `Decimal`
(stored as `NUMERIC`), secrets at rest are encrypted with AES-256-GCM, and every
state change is written to an append-only audit log.

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
| `MAX_CONCURRENT_PASSWORD_HASHES` | `4` | Argon2 hashes/verifies at once per worker (~64 MiB each); extra logins queue |
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
│   ├── tax/                 Pure calculation engine (no I/O)
│   │   ├── constants.py     ← Update tax rates here each January
│   │   └── engine.py        calc_payroll() — all withholding math
│   ├── payroll_run/         Payroll domain (models, schemas, service, API routes)
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
| `DELETE` | `/payroll/api/v1/payroll/runs/{id}` | Delete a saved run (owner) |
| `GET` | `/payroll/api/v1/payroll/tax-constants` | View current tax year rates |
| `GET` | `/payroll/api/v1/healthcheck` | Health check (public) |

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
