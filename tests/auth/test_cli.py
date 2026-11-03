"""Tests for the bootstrap/recovery CLI (payroll.auth.cli)."""

import getpass
import threading

import pytest
from sqlalchemy import delete, func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from payroll.audit.models import AuditLog
from payroll.auth import cli, sessions
from payroll.auth.models import AuthSession, RecoveryCode, User
from payroll.auth.security import PasswordPolicyError, verify_password
from payroll.migrations import alembic_runner

GOOD = "correct horse battery staple 42"
NEW = "another long passphrase 99 ok"


@pytest.fixture(autouse=True)
def empty_users(session):
    """Each test starts with no users (rolled back afterwards)."""
    session.exec(delete(User))
    session.flush()


def _prompts(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(it))


def _count(session, model=User):
    return session.exec(select(func.count()).select_from(model)).one()


def test_no_users_after_fresh_migration(engine):
    # A separate connection sees committed data only: migrations seed no accounts.
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM app_user").scalar() == 0


def test_create_user_hashes_with_argon2id(session):
    user = cli.create_user(session, "Owner@Example.com", GOOD, "owner", None)
    assert user.email == "owner@example.com"
    assert user.display_name == "owner"
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(GOOD, user.password_hash)


def test_create_user_rejects_weak_password(session):
    with pytest.raises(PasswordPolicyError):
        cli.create_user(session, "a@example.com", "short", "owner", None)
    assert _count(session) == 0


def test_create_user_rejects_duplicate_email_case_insensitive(session):
    cli.create_user(session, "a@example.com", GOOD, "owner", None)
    with pytest.raises(cli.CliError, match="already exists"):
        cli.create_user(session, "A@EXAMPLE.COM", GOOD, "viewer", None)
    assert _count(session) == 1


def test_create_user_audit_has_no_secrets(session):
    user = cli.create_user(session, "a@example.com", GOOD, "accountant", "Ann")
    row = session.exec(select(AuditLog).where(AuditLog.action == "auth.user.created")).one()
    assert row.user_id is None and row.entity == "user" and row.entity_id == str(user.id)
    assert row.after == {"email": "a@example.com", "role": "accountant"}
    assert GOOD not in str(row.after) and user.password_hash not in str(row.after)


def test_reset_user_clears_everything(session):
    user = cli.create_user(session, "a@example.com", GOOD, "owner", None)
    user.totp_enabled = True
    user.totp_secret_enc = "enc"
    user.totp_last_step = 5
    user.failed_logins = 3
    user.lock_count = 2
    user.is_active = False
    from payroll.auth.models import utcnow

    user.locked_until = utcnow()
    session.add(user)
    session.add(RecoveryCode(user_id=user.id, code_hash="x" * 64))
    sessions.create_session(session, user, ip=None, user_agent=None)
    session.flush()

    reset = cli.reset_user(session, "A@Example.com", NEW)
    session.flush()
    assert reset.totp_enabled is False
    assert reset.totp_secret_enc is None and reset.totp_last_step is None
    assert (reset.failed_logins, reset.lock_count, reset.locked_until) == (0, 0, None)
    assert reset.is_active is True
    assert verify_password(NEW, reset.password_hash)
    assert _count(session, RecoveryCode) == 0
    assert _count(session, AuthSession) == 0
    row = session.exec(select(AuditLog).where(AuditLog.action == "auth.user.reset")).one()
    assert row.after == {"email": "a@example.com", "role": "owner"}


def test_reset_user_unknown_email_and_weak_password(session):
    with pytest.raises(cli.CliError, match="No user"):
        cli.reset_user(session, "nobody@example.com", NEW)
    cli.create_user(session, "a@example.com", GOOD, "owner", None)
    with pytest.raises(PasswordPolicyError):
        cli.reset_user(session, "a@example.com", "short")


def test_create_owner_command(session, monkeypatch, capsys):
    _prompts(monkeypatch, GOOD, GOOD)
    assert (
        cli.main(["create-owner", "--email", "o@example.com", "--name", "Olive"], db=session) == 0
    )
    user = session.exec(select(User)).one()
    assert user.role == "owner" and user.display_name == "Olive"
    out = capsys.readouterr()
    assert GOOD not in out.out + out.err


def test_create_owner_refuses_when_user_exists(session, monkeypatch, capsys):
    cli.create_user(session, "a@example.com", GOOD, "owner", None)
    _prompts(monkeypatch, GOOD, GOOD)
    assert cli.main(["create-owner", "--email", "o@example.com"], db=session) == 1
    assert "--force-additional" in capsys.readouterr().err
    assert _count(session) == 1
    assert (
        cli.main(["create-owner", "--email", "o@example.com", "--force-additional"], db=session)
        == 0
    )
    assert _count(session) == 2


def test_mismatched_confirmation_creates_nothing(session, monkeypatch, capsys):
    _prompts(monkeypatch, GOOD, GOOD + "x")
    assert cli.main(["create-owner", "--email", "o@example.com"], db=session) == 1
    out = capsys.readouterr()
    assert "match" in out.err and GOOD not in out.out + out.err
    assert _count(session) == 0


def test_weak_password_exits_1_without_echo(session, monkeypatch, capsys):
    _prompts(monkeypatch, "short", "short")
    assert cli.main(["create-owner", "--email", "o@example.com"], db=session) == 1
    out = capsys.readouterr()
    assert out.err and "short" not in out.out + out.err
    assert _count(session) == 0


def test_create_user_command_refuses_owner_role(session, monkeypatch, capsys):
    _prompts(monkeypatch, GOOD, GOOD)
    with pytest.raises(SystemExit) as exc:
        cli.main(["create-user", "--email", "v@example.com", "--role", "owner"], db=session)
    assert exc.value.code == 2
    assert _count(session) == 0


def test_create_user_command_viewer(session, monkeypatch):
    _prompts(monkeypatch, GOOD, GOOD)
    assert (
        cli.main(["create-user", "--email", "v@example.com", "--role", "viewer"], db=session) == 0
    )
    assert session.exec(select(User)).one().role == "viewer"


def test_reset_owner_command(session, monkeypatch, capsys):
    cli.create_user(session, "a@example.com", GOOD, "owner", None)
    _prompts(monkeypatch, NEW, NEW)
    assert cli.main(["reset-owner", "--email", "a@example.com"], db=session) == 0
    user = session.exec(select(User)).one()
    assert verify_password(NEW, user.password_hash)
    out = capsys.readouterr()
    assert NEW not in out.out + out.err


def test_reset_owner_command_unknown_email(session, monkeypatch, capsys):
    _prompts(monkeypatch, NEW, NEW)
    assert cli.main(["reset-owner", "--email", "x@example.com"], db=session) == 1
    assert "No user" in capsys.readouterr().err


def test_integrity_error_on_duplicate_email_is_clean(session, monkeypatch, capsys):
    cli.create_user(session, "a@example.com", GOOD, "owner", None)
    monkeypatch.setattr(cli, "_find_user", lambda db, email: None)  # force the DB constraint
    _prompts(monkeypatch, GOOD, GOOD)
    assert (
        cli.main(["create-user", "--email", "a@example.com", "--role", "viewer"], db=session) == 1
    )
    out = capsys.readouterr()
    assert "already exists" in out.err
    text = out.out + out.err
    assert "$argon2" not in text and GOOD not in text and "parameters" not in text


def test_operational_error_prints_generic_message(session, monkeypatch, capsys):
    secret_hash = "$argon2id$v=19$SECRETHASH"

    def boom(*args, **kwargs):
        raise OperationalError("INSERT ...", {"password_hash": secret_hash}, Exception("down"))

    monkeypatch.setattr(cli, "create_user", boom)
    _prompts(monkeypatch, GOOD, GOOD)
    assert (
        cli.main(["create-user", "--email", "a@example.com", "--role", "viewer"], db=session) == 1
    )
    out = capsys.readouterr()
    assert "OperationalError" in out.err and "nothing was changed" in out.err
    text = out.out + out.err
    assert secret_hash not in text and GOOD not in text and "INSERT" not in text


def test_concurrent_create_owner_only_one_wins(scratch_engine, monkeypatch, capsys):
    # Own scratch DB: committed audit rows are append-only and cannot be cleaned up.
    alembic_runner.upgrade()
    engine = scratch_engine
    barrier = threading.Barrier(2, timeout=10)
    real_lock = cli._lock_bootstrap

    def synced_lock(db):
        barrier.wait()  # both runs are about to check; without the lock both would pass
        real_lock(db)

    monkeypatch.setattr(cli, "_lock_bootstrap", synced_lock)
    _prompts_forever(monkeypatch)
    results = {}

    def run(email):
        with Session(engine) as s:
            results[email] = cli.main(["create-owner", "--email", email], db=s)

    threads = [
        threading.Thread(target=run, args=(e,)) for e in ("x1@example.com", "x2@example.com")
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    assert sorted(results.values()) == [0, 1]
    assert "already exist" in capsys.readouterr().err
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(User)).one() == 1


def _prompts_forever(monkeypatch):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": GOOD)


def test_real_path_runs_migrations_commits_and_resets(scratch_engine, monkeypatch, capsys):
    """main(argv) without an injected db: upgrade, create-owner (committed), reset-owner."""
    _prompts_forever(monkeypatch)
    assert cli.main(["create-owner", "--email", "Real@Example.com"]) == 0
    with scratch_engine.connect() as conn:  # a separate connection sees only committed data
        rows = conn.exec_driver_sql("SELECT email, role FROM app_user").all()
        assert [tuple(r) for r in rows] == [("real@example.com", "owner")]
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": NEW)
    assert cli.main(["reset-owner", "--email", "real@example.com"]) == 0
    with scratch_engine.connect() as conn:
        actions = conn.exec_driver_sql("SELECT action FROM audit_log ORDER BY id").scalars().all()
        assert actions == ["auth.user.created", "auth.user.reset"]
        h = conn.exec_driver_sql("SELECT password_hash FROM app_user").scalar()
    assert verify_password(NEW, h)
    out = capsys.readouterr()
    assert GOOD not in out.out + out.err and NEW not in out.out + out.err
