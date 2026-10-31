"""Tests for server-side sessions and login lockout (payroll.auth.sessions)."""

import hashlib
from datetime import datetime, timedelta

from sqlmodel import select

from payroll import app_settings
from payroll.auth import sessions
from payroll.auth.models import AuthSession, User, utcnow

T0 = datetime(2026, 10, 31, 9, 0, 0)


def _user(session, email="owner@example.com") -> User:
    user = User(email=email, display_name="Owner", password_hash="$argon2id$x", role="owner")
    session.add(user)
    session.flush()
    return user


def _create(session, user, now=None):
    raw, auth = sessions.create_session(session, user, ip="10.0.0.1", user_agent="pytest")
    if now is not None:
        auth.created_at = auth.last_seen_at = now
        auth.expires_at = now + timedelta(hours=app_settings.session_absolute_hours)
    session.flush()
    return raw, auth


# ---------------------------------------------------------------------------
# create / load
# ---------------------------------------------------------------------------


def test_create_session_stores_only_the_token_hash(session):
    user = _user(session)
    raw, auth = _create(session, user)
    assert len(raw) >= 40
    assert auth.id == hashlib.sha256(raw.encode()).hexdigest()
    assert auth.id != raw
    rows = session.exec(select(AuthSession)).all()
    assert all(raw not in (r.id, r.csrf_token) for r in rows)
    assert auth.user_id == user.id
    assert auth.mfa_verified is False
    assert auth.csrf_token and auth.csrf_token != raw
    assert auth.ip == "10.0.0.1"
    assert auth.user_agent == "pytest"


def test_create_session_sets_absolute_expiry_and_truncates_user_agent(session):
    user = _user(session)
    before = utcnow()
    _, auth = sessions.create_session(session, user, ip=None, user_agent="x" * 2000)
    assert len(auth.user_agent) == 512
    expected = before + timedelta(hours=app_settings.session_absolute_hours)
    assert abs((auth.expires_at - expected).total_seconds()) < 5


def test_tokens_are_unique(session):
    user = _user(session)
    raw1, _ = _create(session, user)
    raw2, _ = _create(session, user)
    assert raw1 != raw2


def test_load_session_returns_live_session(session):
    user = _user(session)
    raw, auth = _create(session, user, now=T0)
    assert sessions.load_session(session, raw, now=T0 + timedelta(minutes=5)) is auth


def test_load_session_unknown_or_empty_token(session):
    assert sessions.load_session(session, "nope") is None
    assert sessions.load_session(session, "") is None
    assert sessions.load_session(session, None) is None


def test_idle_expiry_at_31_minutes(session):
    user = _user(session)
    raw, auth = _create(session, user, now=T0)
    session_id = auth.id
    assert sessions.load_session(session, raw, now=T0 + timedelta(minutes=29)) is not None
    # last_seen_at was refreshed at +29 min, so idle is measured from there.
    assert sessions.load_session(session, raw, now=T0 + timedelta(minutes=29 + 31)) is None
    session.flush()
    assert session.get(AuthSession, session_id) is None  # expired row deleted


def test_idle_expiry_from_creation(session):
    user = _user(session)
    raw, _ = _create(session, user, now=T0)
    assert sessions.load_session(session, raw, now=T0 + timedelta(minutes=31)) is None


def test_absolute_expiry_at_12h_1min_even_when_active(session):
    user = _user(session)
    raw, _ = _create(session, user, now=T0)
    now = T0
    # Keep the session busy every 20 minutes up to the 12 h mark.
    while now < T0 + timedelta(hours=12):
        assert sessions.load_session(session, raw, now=now) is not None
        now += timedelta(minutes=20)
    assert sessions.load_session(session, raw, now=T0 + timedelta(hours=11, minutes=59)) is not None
    assert sessions.load_session(session, raw, now=T0 + timedelta(hours=12, minutes=1)) is None


def test_last_seen_only_written_when_stale(session):
    user = _user(session)
    raw, auth = _create(session, user, now=T0)
    sessions.load_session(session, raw, now=T0 + timedelta(seconds=30))
    assert auth.last_seen_at == T0  # fresh enough: no write
    sessions.load_session(session, raw, now=T0 + timedelta(seconds=90))
    assert auth.last_seen_at == T0 + timedelta(seconds=90)


# ---------------------------------------------------------------------------
# rotate / revoke
# ---------------------------------------------------------------------------


def test_rotate_invalidates_old_token_and_keeps_user_and_mfa(session):
    user = _user(session)
    old_raw, old = _create(session, user)
    old.mfa_verified = True
    old_id = old.id
    new_raw, new = sessions.rotate_session(session, old, ip="10.0.0.2", user_agent="ua2")
    session.flush()
    assert new_raw != old_raw
    assert new.id != old_id
    assert new.user_id == user.id
    assert new.mfa_verified is True
    assert new.csrf_token != old.csrf_token
    assert new.ip == "10.0.0.2"
    assert sessions.load_session(session, old_raw) is None
    assert sessions.load_session(session, new_raw) is new


def test_revoke_session_makes_cookie_unusable(session):
    user = _user(session)
    raw, auth = _create(session, user)
    sessions.revoke_session(session, auth.id)
    session.flush()
    assert sessions.load_session(session, raw) is None


def test_revoke_all_only_affects_that_user(session):
    alice = _user(session, "alice@example.com")
    bob = _user(session, "bob@example.com")
    a1, _ = _create(session, alice)
    a2, _ = _create(session, alice)
    b1, _ = _create(session, bob)
    sessions.revoke_all(session, alice.id)
    session.flush()
    assert sessions.load_session(session, a1) is None
    assert sessions.load_session(session, a2) is None
    assert sessions.load_session(session, b1) is not None


# ---------------------------------------------------------------------------
# lockout
# ---------------------------------------------------------------------------


def test_lockout_after_five_failures_for_15_minutes(session):
    user = _user(session)
    for _ in range(4):
        sessions.register_failure(session, user, now=T0)
        assert not sessions.is_locked(user, now=T0)
    assert user.failed_logins == 4
    sessions.register_failure(session, user, now=T0)
    assert user.locked_until == T0 + timedelta(minutes=15)
    assert user.failed_logins == 0
    assert user.lock_count == 1
    assert sessions.is_locked(user, now=T0 + timedelta(minutes=14, seconds=59))
    assert not sessions.is_locked(user, now=T0 + timedelta(minutes=15))


def test_second_lockout_doubles(session):
    user = _user(session)
    for _ in range(5):
        sessions.register_failure(session, user, now=T0)
    t1 = T0 + timedelta(minutes=16)
    for _ in range(5):
        sessions.register_failure(session, user, now=t1)
    assert user.locked_until == t1 + timedelta(minutes=30)
    assert user.lock_count == 2
    assert sessions.is_locked(user, now=t1 + timedelta(minutes=29))
    assert not sessions.is_locked(user, now=t1 + timedelta(minutes=30))


def test_success_resets_counters(session):
    user = _user(session)
    for _ in range(5):
        sessions.register_failure(session, user, now=T0)
    sessions.register_failure(session, user, now=T0 + timedelta(minutes=16))
    sessions.register_success(session, user)
    assert user.failed_logins == 0
    assert user.locked_until is None
    assert user.lock_count == 0
    assert not sessions.is_locked(user)
    # After a reset the next lockout is back to the base 15 minutes.
    t2 = T0 + timedelta(hours=1)
    for _ in range(5):
        sessions.register_failure(session, user, now=t2)
    assert user.locked_until == t2 + timedelta(minutes=15)


def test_is_locked_with_no_lock(session):
    user = _user(session)
    assert sessions.is_locked(user) is False


def test_functions_do_not_commit(session):
    user = _user(session)
    _create(session, user)
    sessions.register_failure(session, user, now=T0)
    # Nothing was committed: rolling back undoes the session row too.
    session.rollback()
    assert session.exec(select(AuthSession)).all() == []
