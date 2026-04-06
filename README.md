# Payroll

S-Corp owner payroll calculator with QuickBooks journal entries.

**Georgia · Single Filing · Monthly Payroll · Tax Year 2026**

## Quick start

```bash
# Install dependencies and create virtualenv
uv sync

# Start the development server
uv run uvicorn payroll.app:app --reload --port 9000
```

Open **http://localhost:9000/payroll/api/v1/docs** for the interactive API docs.

## Project layout

```
payroll/
├── payroll/
│   ├── __init__.py          Settings (Pydantic Settings — all config via .env)
│   ├── app.py               FastAPI app factory + lifespan
│   ├── exceptions.py        Base AppBaseError with auto-registration
│   ├── logger_conf.py       Structured logging (standard or JSON)
│   ├── gunicorn_config.py   Production server config
│   ├── common/              Shared infrastructure (models, middleware, deps)
│   ├── tax/                 Pure calculation engine (no I/O, easy to test)
│   │   ├── constants.py     ← Update tax rates here each January
│   │   └── engine.py        calc_payroll() — all withholding math
│   └── payroll_run/         Core domain (models, schemas, service, routes)
├── tests/                   73 tests, 85% coverage
├── pyproject.toml
├── uv.lock                  Locked dependency versions (commit this)
├── compose.yml              Docker Compose (single container + SQLite volume)
├── Dockerfile
└── .env.example             → copy to .env and fill in your values
```

## Configuration

Copy `.env.example` to `.env` and set your values:

```bash
cp .env.example .env
```

Key settings:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./payroll.db` | SQLite (local) or PostgreSQL (prod) |
| `ACCT_*` | See `.env.example` | QuickBooks account names |
| `API_LOG_TYPE` | `standard` | `standard` or `json` |
| `DEBUG_MODE` | `false` | Enables debug logging |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/payroll/api/v1/payroll/runs/calculate` | Calculate payroll + get journal entries |
| `GET` | `/payroll/api/v1/payroll/runs` | List saved payroll history |
| `GET` | `/payroll/api/v1/payroll/runs/{id}` | Get a saved run with full journal entries |
| `DELETE` | `/payroll/api/v1/payroll/runs/{id}` | Delete a saved run |
| `GET` | `/payroll/api/v1/payroll/tax-constants` | View current tax year rates |
| `GET` | `/payroll/api/v1/healthcheck` | Health check |

## Development

```bash
# Install all deps including dev group
uv sync

# Run tests
uv run pytest

# Run linter
uv run ruff check .

# Run formatter
uv run ruff format .

# Install pre-commit hooks
uv run pre-commit install
```

## Updating tax constants

Edit `payroll/tax/constants.py` each January with the new rates from:
- **IRS Pub 15-T** (federal brackets): https://www.irs.gov/pub/irs-pdf/p15t.pdf
- **Social Security wage base**: https://www.ssa.gov/oact/cola/cbb.html
- **Georgia DOR** (GA flat rate): https://dor.georgia.gov

## Docker

```bash
# Build and run
docker compose up --build

# App runs on http://localhost:9000
```

## Schema migrations (Alembic)

The app uses `SQLModel.metadata.create_all` on startup (safe for SQLite).
For production schema changes, use Alembic:

```bash
# Generate a new migration after changing models
uv run python -m payroll.migrations.alembic_runner generate "add column foo"

# Apply migrations
uv run python -m payroll.migrations.alembic_runner upgrade
```

---

> ⚠️ Tax tables are based on IRS Pub 15-T 2025 and Georgia DOR 2026 rates.
> Verify annually. This is not tax advice — consult your CPA.
