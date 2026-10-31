"""Tests for the bootstrap/recovery CLI (payroll.auth.cli)."""

import getpass

import pytest
from sqlalchemy import delete, func
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.auth import cli, sessions
from payroll.auth.models import AuthSession, RecoveryCode, User
from payroll.auth.security import PasswordPolicyError, verify_password

GOOD = "correct horse battery staple 42"
NEW = "another long passphrase 99 ok"


@pytest.fixture(autouse=True)
def empty_users(session):
    """Each test starts with no users (rolled back afterwards)."""
    session.exec(delete(AuditLog))
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
    assert out.err and "short" not in out.out
    assert _count(session) == 0


def test_create_user_command_refuses_owner_role(session, monkeypatch, capsys):
    _prompts(monkeypatch, GOOD, GOOD)
    with pytest.raises(SystemExit) as exc:
        cli.main(["create-user", "--email", "v@example.com", "--role", "owner"], db=session)
    assert exc.value.code == 2 or exc.value.code == 1
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
