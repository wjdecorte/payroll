# DCI Books M0 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the DCI Payroll app into a secure, single-container, Postgres-only app with exact-decimal money, an audit log, and mandatory-MFA login, ready for the ledger (M1).

**Architecture:** Keep the FastAPI + SQLModel + Alembic layout. Merge the HTMX frontend into the API app so pages call services in-process. Add `auth/` (users, sessions, TOTP, CSRF, roles) and `audit/` modules. Convert tax math and persisted money from `float` to `Decimal`/`NUMERIC(14,2)`.

**Tech Stack:** Python 3.14, uv, FastAPI, SQLModel, Alembic, Postgres 17, Jinja2 + HTMX, `argon2-cffi`, `pyotp`, `cryptography`, `segno` (QR as SVG; chosen over `qrcode` to avoid Pillow), pytest, ruff.

**Spec:** [docs/SPEC.md](../../SPEC.md) (§5 security, §8 M0) and [docs/DESIGN.md](../../DESIGN.md) (§2.1, §3, §4.6, §8). Executors read both.

## Global Constraints

- Python `>=3.14`; dependencies only via `uv add`; commit `uv.lock`; `ruff check` and `ruff format` clean; `pytest` coverage not below the current 85%.
- Money is `Decimal`; DB type `Numeric(14, 2)`; never `float`. Rounding is half-up to cents via one helper, `round_money()`.
- Postgres only (tests and production). No SQLite fallback anywhere.
- Passwords: Argon2id, minimum length 12, checked against an offline common-password list (SEC2); maximum length 128.
- TOTP mandatory for every user, 30 s step, ±1 window, 10 one-time recovery codes stored hashed (SEC3).
- Sessions: server-side, random 256-bit ID with only its hash stored; cookie `HttpOnly`, `Secure`, `SameSite=Lax`; idle timeout 30 min, absolute timeout 12 h; ID rotated on login (SEC4).
- CSRF token required on every POST/PUT/PATCH/DELETE (SEC5). Lockout after 5 failed logins for 15 min, doubling on repeat (SEC6).
- Roles exactly: `owner`, `accountant`, `viewer` (SEC7). Every route except `/login*`, `/healthcheck`, `/static/*` requires auth (SEC1).
- Security headers: CSP self-only, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` on authenticated pages.
- No default credentials and no open sign-up; the first owner is created only by CLI.
- DB port is not published to the host (SEC12). `.env.example` stays current; secrets never committed.
- Keep the API prefix `/payroll/api/v1` (`base_url_prefix`) and the module name `payroll`.
- Version becomes `2.0.0` (breaking restructure).
- Commit after each task. Follow `AGENTS.md`; do not touch the pre-existing uncommitted `AGENTS.md` change.

## Review Focus

Failure modes the spec implies but no feature task would test on its own:

1. **User enumeration / timing:** unknown email and wrong password return the identical response and both run an Argon2 verify.
2. **TOTP replay:** the same valid code is rejected a second time within its window.
3. **Giant or odd passwords:** a 10 MB password is rejected before hashing (DoS); Unicode passwords round-trip.
4. **Open redirect:** `?next=` on login accepts only same-site relative paths.
5. **Production data conversion:** existing float rows (e.g. `8000.1`, `0.0`, long fractions) convert to exact cents; the migration upgrades a DB that predates Alembic and downgrades cleanly.

Each is pinned by a test in the task named in brackets: 1 → Task 9, 2 → Task 7, 3 → Task 7, 4 → Task 9, 5 → Task 4.

## File Structure

```
payroll/
├── common/money.py            NEW  round_money(), Money type alias
├── common/crypto.py           NEW  AES-GCM encrypt/decrypt for secrets
├── audit/{models,services}.py NEW  AuditLog table + record()
├── auth/
│   ├── models.py              NEW  User, RecoveryCode, AuthSession
│   ├── security.py            NEW  password + TOTP + recovery-code primitives
│   ├── sessions.py            NEW  session create/load/revoke, lockout
│   ├── dependencies.py        NEW  current_user, require_role, CSRF check
│   ├── middleware.py          NEW  security headers, session loader
│   ├── routers.py             NEW  /login, /login/mfa, /enroll, /logout, password change
│   ├── cli.py                 NEW  create_owner, reset_owner
│   ├── common_passwords.txt   NEW  offline list
│   └── templates/             NEW  login, mfa, enroll pages (extend frontend base.html)
├── frontend/                  MODIFY  routers call services in-process; client.py + second settings removed
├── tax/{constants,engine,schemas}.py  MODIFY  Decimal
├── payroll_run/{models,config_models,schemas,services}.py  MODIFY  Decimal
├── migrations/versions/       NEW  three revisions (decimal, audit, auth)
tests/                         conftest.py rewritten for Postgres; new tests/auth, tests/audit
.github/workflows/test.yml     NEW
compose*.yml, Dockerfile, .env.example, README.md   MODIFY
```

---

### Task 1: Postgres-only test infrastructure and CI

**Files:**
- Create: `compose.test.yml`, `.github/workflows/test.yml`
- Modify: `tests/conftest.py`, `payroll/__init__.py` (default `database_url`), `pyproject.toml` (no new deps)
- Test: `tests/test_db_infra.py`

**Interfaces:**
- Produces: pytest fixtures `engine` (session scope, migrated schema), `session` (per-test, rolled back; nested savepoints so code that commits still rolls back), `client` (TestClient with `get_session` overridden). Env var `TEST_DATABASE_URL` (default `postgresql://payroll:payroll@localhost:5433/payroll_test`).

- [ ] **Step 1: Write `tests/test_db_infra.py::test_session_is_isolated_between_tests`** — two tests; the first inserts a `PayrollConfig` row and commits via `session.commit()`; the second asserts `select(PayrollConfig)` returns no rows. Add `test_engine_is_postgres` asserting `engine.dialect.name == "postgresql"`.
- [ ] **Step 2: Run `uv run pytest tests/test_db_infra.py`** — Expected: FAIL (engine is SQLite).
- [ ] **Step 3: Rewrite `tests/conftest.py`**: `engine` creates the DB schema by running `alembic_runner.upgrade()` against `TEST_DATABASE_URL`; `session` uses `Session(bind=connection, join_transaction_mode="create_savepoint")`. Remove SQLite imports and `create_all`.
- [ ] **Step 4: Add `compose.test.yml`** (service `db`: postgres:17, port `5433:5432`, `POSTGRES_DB=payroll_test`, tmpfs data dir). Change the `Settings.database_url` default to the Postgres dev URL `postgresql://payroll:payroll@localhost:5432/payroll`; update `.env.example` accordingly and delete the SQLite note.
- [ ] **Step 5: Add `.github/workflows/test.yml`** (push + pull_request): Postgres 17 service container, `astral-sh/setup-uv`, `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`, with `TEST_DATABASE_URL` pointed at the service.
- [ ] **Step 6: Run `docker compose -f compose.test.yml up -d db && uv run pytest`** — Expected: the new tests PASS; existing tests that relied on SQLite semantics are fixed in place (list them in the commit message). Full suite green.
- [ ] **Step 7: Commit** `test: run the suite against Postgres and add CI workflow`.

---

### Task 2: Golden snapshot of current engine behavior

**Files:**
- Create: `tests/tax/golden_cases.json`, `tests/tax/test_golden.py`, `tests/tax/make_golden.py`

**Interfaces:**
- Produces: `golden_cases.json`, a list of `{inputs: {...}, expected: {...all PayrollCalculation fields...}}` generated from the **current float** `calc_payroll`, used by Task 3.

- [ ] **Step 1:** Write `make_golden.py` that runs `calc_payroll` over a grid: gross ∈ {0, 1, 1234.56, 5000, 8000, 16666.67, 30000, 55000}, health ∈ {0, 500}, hsa ∈ {0, 300}, both toggle combinations, `ytd_fica_wages` ∈ {0, 100_000, 184_000, 184_500, 250_000}. Write the JSON (floats as given).
- [ ] **Step 2:** Run it once on the unchanged engine; commit the JSON.
- [ ] **Step 3:** Write `test_golden.py::test_engine_matches_golden` asserting each field equals expected within `0.005` (float engine passes trivially now).
- [ ] **Step 4: Run `uv run pytest tests/tax`** — Expected: PASS.
- [ ] **Step 5: Commit** `test: add golden snapshot of payroll engine output`.

---

### Task 3: Decimal tax engine

**Files:**
- Create: `payroll/common/money.py`, `tests/common/test_money.py`
- Modify: `payroll/tax/constants.py`, `payroll/tax/engine.py`, `payroll/tax/schemas.py`, `tests/tax/test_engine.py`, `tests/tax/test_golden.py`

**Interfaces:**
- Produces: `round_money(value: Decimal | int | str) -> Decimal` (half-up to 2 places, raises `TypeError` on `float`); `Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]`. `calc_payroll(gross_salary: Decimal, health_insurance: Decimal, hsa_contribution: Decimal, health_in_income_tax: bool, hsa_in_income_tax: bool, ytd_fica_wages: Decimal, pay_periods: int = ...) -> PayrollCalculation` where every amount field of `PayrollCalculation` is `Decimal`.

- [ ] **Step 1: Write `test_money.py`**: `round_money(Decimal("2.345")) == Decimal("2.35")`; `round_money(Decimal("2.344")) == Decimal("2.34")`; `round_money(Decimal("-0.005")) == Decimal("-0.01")`; `round_money(1.5)` raises `TypeError`.
- [ ] **Step 2: Run** — FAIL (module missing).
- [ ] **Step 3: Implement `common/money.py`** with `decimal.ROUND_HALF_UP`.
- [ ] **Step 4: Update `test_golden.py`** to load expected values via `Decimal(str(x))` and require `abs(actual - expected) <= Decimal("0.01")`; add a test listing every case where the difference exceeds `0` so a reviewer can see them (print, not fail).
- [ ] **Step 5: Convert** constants (rates and brackets as `Decimal("0.062")` etc., `inf` bracket via a `None` upper bound handled in the lookup), engine, and `PayrollCalculation` to `Decimal`, applying `round_money` at the same points the float engine called `round(..., 2)`. Update existing `test_engine.py` literals to `Decimal`.
- [ ] **Step 6: Add boundary tests** in `test_engine.py`: gross `0` yields all-zero result; YTD exactly at `184_500` yields zero SS; one cent under yields the remaining cent-bound SS; negative gross raises `ValueError`.
- [ ] **Step 7: Run `uv run pytest tests/tax tests/common/test_money.py`** — Expected: PASS; no golden diff over $0.01. If one exists, stop and report it, since it is a tax-correctness question.
- [ ] **Step 8: Commit** `refactor: exact Decimal arithmetic in the tax engine`.

---

### Task 4: Decimal persistence and Alembic migration

**Files:**
- Create: `payroll/migrations/versions/<rev>_money_to_numeric.py`, `tests/migrations/test_money_migration.py`
- Modify: `payroll/payroll_run/{models,config_models,schemas,services,config_schemas,config_services}.py`, `payroll/migrations/alembic_runner.py` (`_HEAD_REVISION`), `payroll/app.py` (remove SQLModel fallback), `tests/payroll_run/*`, `tests/conftest.py` fixtures

**Interfaces:**
- Consumes: `Money`, `round_money`, Decimal `calc_payroll` (Task 3).
- Produces: all money columns on `payroll_run` and `payroll_config` are `NUMERIC(14,2)`; Pydantic schemas use `Money`; JSON money serializes as strings (e.g. `"netPay": "5123.45"`); `ytd_fica_*` and `default_*` money columns included.

- [ ] **Step 1: Write `test_money_migration.py`** using a scratch database: upgrade to the pre-change head, insert a `payroll_run` row with floats `8000.1`, `0.0`, `1234.5678`, and a `payroll_config` row; upgrade to the new head; assert values are `Decimal("8000.10")`, `Decimal("0.00")`, `Decimal("1234.57")`. Add `test_downgrade_restores_float` and `test_legacy_db_without_alembic_version_is_stamped_and_upgraded` (create tables via the old schema, no `alembic_version`, run `upgrade()`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Write the migration** with `op.alter_column(..., type_=sa.Numeric(14,2), postgresql_using="round(col::numeric, 2)")` for every float column; downgrade casts back to `Float`. Update `_HEAD_REVISION`.
- [ ] **Step 4: Convert models, schemas and `PayrollRunService`** (journal lines are `Decimal`; `dr_total`/`cr_total` sum Decimals). Remove the `create_all` fallback block from `lifespan`: failure to migrate must stop startup.
- [ ] **Step 5: Update existing tests/fixtures** to Decimal-string payloads and assertions (`response.json()["netPay"] == "…"`). Add a test that journal debits equal credits exactly for every golden-grid input via the service.
- [ ] **Step 6: Run the full suite** — Expected: PASS.
- [ ] **Step 7: Commit** `feat: store money as NUMERIC(14,2) and convert existing payroll data`. In the commit body note that a `pg_dump` must be taken before deploying (AGENTS.md: production data).

---

### Task 5: Merge the frontend into the app

**Files:**
- Modify: `payroll/app.py`, `payroll/frontend/routers.py`, `payroll/frontend/__init__.py`, `tests/common/test_common_routers.py`
- Delete: `payroll/frontend/client.py`, `payroll/frontend/app.py`
- Create: `tests/frontend/test_pages.py`

**Interfaces:**
- Consumes: `PayrollRunService(session)`, `PayrollConfigService(session)`, `get_session`.
- Produces: the main app serves `GET /`, `GET /admin`, `/htmx/*` and `/static/*` alongside the API; page routes use `Depends(get_session)` and call services directly. `frontend_settings` is removed; templates read `app_settings`.

- [ ] **Step 1: Write `tests/frontend/test_pages.py`**: `test_index_renders` (GET `/` → 200, contains "DCI Payroll"); `test_calculate_returns_results_partial` (POST `/htmx/calculate` with form data → 200, contains the formatted net pay); `test_history_lists_saved_runs`; `test_delete_run_refreshes_history`. They run through the single `client` fixture.
- [ ] **Step 2: Run** — FAIL (routes not on the main app).
- [ ] **Step 3: Rewrite `frontend/routers.py`** replacing each `PayrollAPIClient` call with the equivalent service call; keep templates and HTMX contract unchanged; register the router and static mount in `create_app()`. Mark page routes `include_in_schema=False`.
- [ ] **Step 4: Delete `client.py`, `frontend/app.py` and `FrontendSettings`**; point template globals at `app_settings`.
- [ ] **Step 5: Run full suite and manually run `uv run uvicorn payroll.app:app --port 9000`**; load `/` and run one calculation. Expected: page loads, calculation shows results.
- [ ] **Step 6: Commit** `refactor: serve the UI from the API app, remove the separate frontend service`.

---

### Task 6: Settings and crypto helper

**Files:**
- Create: `payroll/common/crypto.py`, `tests/common/test_crypto.py`
- Modify: `payroll/__init__.py`, `.env.example`

**Interfaces:**
- Produces: `encrypt_secret(plaintext: str) -> str` and `decrypt_secret(token: str) -> str` (AES-256-GCM, random 12-byte nonce, urlsafe-base64 `nonce+ciphertext`); raises `ValueError` on tampering. New `Settings` fields: `encryption_key: str` (base64 32 bytes; required, no default), `session_cookie_name: str = "dci_session"`, `session_cookie_secure: bool = True`, `session_idle_minutes: int = 30`, `session_absolute_hours: int = 12`, `max_failed_logins: int = 5`, `lockout_minutes: int = 15`, `totp_issuer: str = "DCI Books"`.

- [ ] **Step 1: Write tests**: round-trip; two encryptions of the same plaintext differ; a flipped byte raises `ValueError`; decrypt with another key raises `ValueError`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** with `cryptography.hazmat.primitives.ciphers.aead.AESGCM`; add the dependency with `uv add cryptography`.
- [ ] **Step 4: Add the settings** and a test key in `tests/conftest.py` via `os.environ.setdefault` before importing `payroll`; document each variable in `.env.example`.
- [ ] **Step 5: Run suite; commit** `feat: add secret encryption helper and auth settings`.

---

### Task 7: Auth data model and security primitives

**Files:**
- Create: `payroll/auth/{__init__,models,security}.py`, `payroll/auth/common_passwords.txt`, `payroll/migrations/versions/<rev>_auth_tables.py`, `tests/auth/test_security.py`
- Modify: `payroll/migrations/alembic_runner.py` (`_HEAD_REVISION`), test conftest model imports

**Interfaces:**
- Produces models: `User(id, email unique lower-cased, display_name, password_hash, role, totp_secret_enc nullable, totp_enabled bool, totp_last_step int nullable, failed_logins int, locked_until datetime nullable, lock_count int, is_active bool)`, `RecoveryCode(id, user_id, code_hash, used_at nullable)`, `AuthSession(id str pk = sha256 hex of token, user_id, created_at, last_seen_at, expires_at, ip, user_agent, mfa_verified bool, csrf_token str)`.
- Produces functions in `security.py`:
  - `hash_password(password: str) -> str`; `verify_password(password: str, password_hash: str | None) -> bool` (when hash is `None` runs a verify against a module-level dummy hash and returns `False`).
  - `validate_password_policy(password: str) -> None` raising `PasswordPolicyError(message)`: length < 12, > 128, or present in `common_passwords.txt` (case-insensitive).
  - `new_totp_secret() -> str`; `totp_uri(secret: str, email: str) -> str`; `verify_totp(user: User, code: str, now: datetime | None = None) -> bool` — accepts ±1 step, rejects any step ≤ `user.totp_last_step`, and on success sets `user.totp_last_step`.
  - `generate_recovery_codes(n: int = 10) -> list[str]` (format `xxxxx-xxxxx`); `hash_recovery_code(code: str) -> str`; `consume_recovery_code(session, user_id, code) -> bool`.

- [ ] **Step 1: Write tests**: `test_hash_is_argon2id` (hash starts `$argon2id$`); `test_verify_unknown_user_hash_none_returns_false_and_takes_similar_time` (within an order of magnitude of a real failed verify) [Review Focus 1]; `test_policy_rejects_short_long_and_common` (11 chars, 129 chars, 10 MB string rejected without calling the hasher [Review Focus 3], `"Password12345"` if present in list); `test_unicode_password_roundtrip`; `test_totp_accepts_current_and_adjacent_step`, `test_totp_rejects_replay` [Review Focus 2], `test_totp_rejects_old_step`; `test_recovery_code_single_use`; migration `test_auth_tables_exist`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Add dependencies** `uv add argon2-cffi pyotp`; implement models, migration and `security.py`. Vendor the 10,000 most-common-passwords list (SecLists, MIT) into `common_passwords.txt` (one password per line) and load it once into a frozenset; record its source and license in `payroll/auth/README.md`. Store `totp_secret_enc` via `encrypt_secret`.
- [ ] **Step 4: Run** `uv run pytest tests/auth` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(auth): user, session and recovery models with password and TOTP primitives`.

---

### Task 8: Audit log

**Files:**
- Create: `payroll/audit/{__init__,models,services}.py`, `payroll/migrations/versions/<rev>_audit_log.py`, `tests/audit/test_audit.py`

**Interfaces:**
- Produces: table `audit_log(id, ts, user_id nullable, action str, entity str nullable, entity_id str nullable, before JSONB nullable, after JSONB nullable, ip nullable)`. `record(session: Session, *, action: str, user_id: int | None = None, entity: str | None = None, entity_id: str | None = None, before: dict | None = None, after: dict | None = None, ip: str | None = None) -> AuditLog` (adds to the caller's session; does not commit). DB trigger raises on `UPDATE` or `DELETE` of `audit_log` rows.

- [ ] **Step 1: Write tests**: `test_record_persists_row`; `test_update_is_rejected` and `test_delete_is_rejected` (expect `sqlalchemy.exc.DBAPIError` / `InternalError`); `test_record_does_not_commit` (rollback removes the row).
- [ ] **Step 2: Run** — FAIL. **Step 3:** Implement model, migration (including the trigger), `record`. **Step 4:** Run — PASS.
- [ ] **Step 5: Wire** audit calls into `PayrollRunService` for save/delete of a run (`action="payroll_run.save"` / `"payroll_run.delete"`) with a test asserting one row each; `user_id` stays `None` until Task 10.
- [ ] **Step 6: Commit** `feat(audit): append-only audit log`.

---

### Task 9: Sessions, CSRF, security headers, and the login flow

**Files:**
- Create: `payroll/auth/{sessions,dependencies,middleware,routers}.py`, `payroll/auth/templates/{login,mfa,enroll,change_password}.html`, `tests/auth/{test_sessions,test_login_flow,test_csrf,test_headers}.py`
- Modify: `payroll/app.py`, `payroll/frontend/templates/base.html` (CSRF header on `<body hx-headers>`, logout button, user name)

**Interfaces:**
- Produces `sessions.py`: `create_session(db, user, *, ip, user_agent) -> tuple[str, AuthSession]` (returns the raw token once; stores sha256), `load_session(db, raw_token, now=None) -> AuthSession | None` (enforces idle + absolute expiry, updates `last_seen_at`), `revoke_session(db, session_id)`, `revoke_all(db, user_id)`, `rotate_session(db, old, ...) -> tuple[str, AuthSession]`. `register_failure(db, user, now=None)` increments `failed_logins`, locks after `max_failed_logins` for `lockout_minutes * 2**lock_count`; `register_success(db, user)` resets counters.
- Produces `dependencies.py`: `current_session`, `current_user` (401 for API/JSON, 303 redirect to `/login?next=<path>` for HTML; pending-MFA sessions count as unauthenticated except for MFA routes), `verify_csrf` applied as an app-wide dependency on unsafe methods (token from `X-CSRF-Token` header or `csrf_token` form field, compared with `hmac.compare_digest`).
- Routes: `GET/POST /login`, `GET/POST /login/mfa`, `GET/POST /enroll` (first login: shows QR SVG via `segno` plus recovery codes once), `POST /logout`, `GET/POST /account/password`.

- [ ] **Step 1: Write session tests**: expired by idle (31 min) and by absolute (12 h 1 min); token not stored in plaintext; `rotate_session` invalidates the old token; logout makes the old cookie unusable [session fixation]; lockout after 5 failures lasts 15 min and doubles the second time.
- [ ] **Step 2: Write flow tests (TestClient)**: wrong password and unknown email return identical status and body [Review Focus 1]; correct password without MFA does not reach `/`; first login redirects to `/enroll`, then TOTP success reaches `/`; recovery code works once; `?next=https://evil.example` and `?next=//evil.example` are ignored (redirect to `/`), `?next=/admin` is honored [Review Focus 4]; cookie flags `HttpOnly; Secure; SameSite=lax`; POST without CSRF → 403; headers test asserts CSP, `X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store` on `/`.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** sessions and dependencies; add the security-header middleware; implement routers and templates (reuse `base.html` styles, no external assets so CSP can stay self-only; HTMX already vendored under `/static`—if it is loaded from a CDN today, vendor it into `static/` in this step).
- [ ] **Step 5: Audit** every login success/failure, lockout, MFA enroll, logout and password change through `record(...)` with `ip` and `user_id`; add one assertion per event type.
- [ ] **Step 6: Run** `uv run pytest tests/auth` — Expected: PASS.
- [ ] **Step 7: Commit** `feat(auth): login with mandatory TOTP, server-side sessions, CSRF and security headers`.

---

### Task 10: Authorization on every route

**Files:**
- Create: `payroll/auth/permissions.py`, `tests/auth/test_route_protection.py`
- Modify: `payroll/app.py`, `payroll/common/routers.py`, `payroll/payroll_run/routers.py`, `payroll/frontend/routers.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `require_role(*roles: str)` dependency returning the `User`; constant `PUBLIC_PATHS = ("/login", "/healthcheck", "/static")` (prefix match, healthcheck at `{base_url_prefix}/healthcheck`). Fixtures `owner_client`, `viewer_client`, `anon_client` that log in through the real flow.
- Policy: payroll calculate/save/delete/config → `owner`; payroll history/read → `owner`, `accountant`, `viewer`; `/info` → any authenticated user.

- [ ] **Step 1: Write `test_every_route_requires_auth`**: iterate `app.routes` (including docs/openapi routes), and for each non-public route assert an unauthenticated request gets 401 (API) or 303 to `/login` (HTML). A route added later without protection fails this test. Write `test_viewer_cannot_mutate` (POST calculate with save, DELETE run, PUT config → 403 for `viewer` and `accountant`), `test_viewer_can_read_history`, `test_docs_require_auth`.
- [ ] **Step 2: Run** — FAIL. **Step 3:** Apply `require_role` on each router via `dependencies=[...]`; set `docs_url`, `redoc_url`, `openapi_url` behind auth (serve through authenticated routes) or disable in production when `debug_mode` is false. **Step 4:** Make the service layer take an `actor: User` for mutating methods and enforce role there as well; record `user_id` in the Task 8 audit calls.
- [ ] **Step 5: Run** full suite — PASS. **Step 6: Commit** `feat(auth): role-based authorization on every route`.

---

### Task 11: Bootstrap and recovery CLI

**Files:**
- Create: `payroll/auth/cli.py`, `tests/auth/test_cli.py`
- Modify: `README.md` (usage)

**Interfaces:**
- Produces: `python -m payroll.auth.cli create-owner --email E` (prompts for password twice with `getpass`; refuses if any user exists unless `--force-additional`), `create-user --email E --role accountant|viewer`, `reset-owner --email E` (new password, clears TOTP, revokes all sessions, clears lockout). Functions `create_user(db, email, password, role, display_name) -> User` and `reset_user(db, email, new_password) -> User` are importable and used by the CLI.

- [ ] **Step 1: Write tests**: `create_user` rejects a policy-failing password and a duplicate email (case-insensitive); `create_owner` command refuses when a user already exists; `reset_user` clears `totp_enabled`, zeroes counters and revokes sessions; no user exists after a fresh migration (no default credentials).
- [ ] **Step 2: Run** — FAIL. **Step 3:** Implement. **Step 4:** Run — PASS.
- [ ] **Step 5: Commit** `feat(auth): owner bootstrap and reset CLI`.

---

### Task 12: Deployment, docs, and release

**Files:**
- Modify: `Dockerfile`, `compose.yml`, `compose.synology.yml`, `.env.example`, `README.md`, `pyproject.toml` (version `2.0.0`), `payroll/__init__.py` (`app_version`), `.github/workflows/*.yml` (build job needs the test job)
- Create: `docs/RUNBOOK.md`
- Modify (outside repo): `/Users/jason/Documents/ProjectOS/01 Projects/Payroll App.md`

- [ ] **Step 1:** In both compose files remove the `frontend` service and the `db` `ports:` entry; put `db` and `api` (renamed `app`) on an internal network; publish only `9000` (or `8070` to preserve the old UI port; keep whichever the Synology reverse proxy points at, and note it in the runbook). Mount `ENCRYPTION_KEY` from a Docker secret or env file.
- [ ] **Step 2:** `docker compose up --build`, create the owner with the CLI inside the container, log in over HTTP using `SESSION_COOKIE_SECURE=false` for local only, run a calculation. Expected: works end to end; `docker compose port db 5432` prints nothing.
- [ ] **Step 3:** Write `docs/RUNBOOK.md`: generating `ENCRYPTION_KEY`, TLS via the Synology reverse proxy (cookie `Secure` requires HTTPS), pre-upgrade `pg_dump`, creating/resetting the owner, lost-authenticator recovery.
- [ ] **Step 4:** Update README (layout, auth, run, test commands), bump version to `2.0.0`, make the image-build workflow depend on the test workflow.
- [ ] **Step 5:** Run `uv run ruff check . && uv run ruff format --check . && uv run pytest` — Expected: clean, coverage ≥ 85%.
- [ ] **Step 6:** Update the ProjectOS note per `AGENTS.md`: Decision Log entries for any choices made during M0, Agent Activity Log report, `last_worked`, `updated`, Resume Here, and `next_step: Plan M1 (ledger and reports)`.
- [ ] **Step 7: Commit** `chore: v2.0.0 single-container deployment and runbook`.

---

## Self-Review

- **Spec coverage (M0 = Foundation):** auth + MFA (Tasks 6–11), single-app structure (5, 12), Postgres-only (1, 4), Decimal money (3, 4), audit log (8, 9, 10), CI green (1, 12). SEC1 → 10; SEC2–3 → 7; SEC4–5 → 9; SEC6 → 9; SEC7 → 10; SEC8 headers → 9 (TLS itself → runbook); SEC9 crypto helper → 6 (SSN fields arrive with M4); SEC10 backups → deferred to M7, per spec milestone; SEC11 → 6, 12; SEC12 → 12. Passkeys are out of M0 by design.
- **Deliberate deviations:** `segno` instead of `qrcode`; money serialized as JSON strings; docs routes protected; backups sidecar left to M7.
- **Type consistency:** `Money`/`round_money` (Task 3) used in Task 4; `encrypt_secret` (Task 6) used for `totp_secret_enc` (Task 7); `record()` (Task 8) used in 9 and 10; `current_user`/`require_role` (9, 10) and fixtures `owner_client` etc. defined in Task 10 and used only from Task 10 on.
- **Risk to watch:** Task 3's golden comparison — any difference over one cent between the float and Decimal engines is a payroll-correctness question for the owner, not something to silently accept.
