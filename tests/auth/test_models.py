"""Tests for the auth models: User, RecoveryCode and AuthSession."""

import hashlib
import secrets
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from payroll.auth.models import AuthSession, RecoveryCode, User, UserRole


def _user(**overrides) -> User:
    values = {
        "email": "owner@example.com",
        "display_name": "Owner",
        "password_hash": "$argon2id$placeholder",
        "role": "owner",
    }
    return User(**(values | overrides))


def test_user_defaults():
    user = _user()
    assert user.totp_enabled is False
    assert user.totp_secret_enc is None
    assert user.totp_last_step is None
    assert user.failed_logins == 0
    assert user.locked_until is None
    assert user.lock_count == 0
    assert user.is_active is True


def test_user_email_is_lower_cased_and_stripped():
    assert _user(email="  Jason@Example.COM ").email == "jason@example.com"
    user = _user()
    user.email = "MiXeD@Example.com"
    assert user.email == "mixed@example.com"


@pytest.mark.parametrize("role", ["owner", "accountant", "viewer", UserRole.VIEWER])
def test_user_accepts_known_roles(role):
    assert _user(role=role).role == str(role)


@pytest.mark.parametrize("role", ["admin", "Owner", "", "root"])
def test_user_rejects_unknown_role(role):
    with pytest.raises(ValueError):
        _user(role=role)
    user = _user()
    with pytest.raises(ValueError):
        user.role = role


def test_user_email_unique_case_insensitively(session):
    session.add(_user(email="owner@example.com"))
    session.flush()
    session.add(_user(email="OWNER@example.com"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_db_rejects_bad_role_and_upper_case_email(session):
    insert = sa.text(
        "INSERT INTO app_user (create_date, modify_date, email, display_name, password_hash,"
        " role, totp_enabled, failed_logins, lock_count, is_active)"
        " VALUES (now(), now(), :email, 'X', 'h', :role, false, 0, 0, true)"
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(insert, {"email": "a@example.com", "role": "admin"})
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(insert, {"email": "A@example.com", "role": "owner"})


def test_auth_session_and_recovery_code_persist(session):
    user = _user()
    session.add(user)
    session.flush()

    token = secrets.token_urlsafe(32)
    session_id = hashlib.sha256(token.encode()).hexdigest()
    now = datetime(2026, 10, 31, 12, 0, 0)
    auth_session = AuthSession(
        id=session_id,
        user_id=user.id,
        expires_at=now + timedelta(hours=12),
        ip="203.0.113.7",
        user_agent="pytest",
        csrf_token=secrets.token_urlsafe(32),
    )
    session.add(auth_session)
    session.add(RecoveryCode(user_id=user.id, code_hash="0" * 64))
    session.flush()
    session.expire_all()

    loaded = session.get(AuthSession, session_id)
    assert loaded is not None
    assert loaded.user_id == user.id
    assert loaded.mfa_verified is False
    assert loaded.created_at is not None
    assert loaded.last_seen_at is not None
    code = session.exec(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).one()
    assert code.used_at is None
