# DCI Payroll runbook (v2.0.0)

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
   Restore (stop the app first, into an empty/recreated database):
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
   must print `7c1f3a5e8b92` for v2.0.0 (the expected revision changes in future releases;
   see `_HEAD_REVISION` in `payroll/migrations/alembic_runner.py`), and
   `curl -s -o /dev/null -w '%{http_code}' http://localhost:8070/payroll/api/v1/healthcheck` prints 200.
6. Create the owner (see First run), then **re-enable Watchtower:**
   `docker compose -f compose.synology.yml start watchtower`

What changes for you:
- Saved runs display their stored values. Calculation now rounds half-up on exact
  decimals, so a new run can differ by $0.01 from the old float rounding on half-cent ties.
- API money values are JSON **strings** now. Scripts using the API must parse them as
  decimals (breaking change).
- Every route requires sign-in with MFA. There is no API-only anonymous access.

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

## System of record

QuickBooks Online remains the system of record until cutover. Do not delete anything
there; this app produces journal entries to enter into it.
