"""Tests for the password, TOTP and recovery-code primitives in payroll.auth.security."""

import re
import statistics
import time
import unicodedata
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from sqlmodel import select

from payroll.auth import security
from payroll.auth.models import RecoveryCode, User
from payroll.auth.security import (
    PasswordPolicyError,
    consume_recovery_code,
    generate_recovery_codes,
    hash_password,
    hash_recovery_code,
    new_totp_secret,
    totp_uri,
    validate_password_policy,
    verify_password,
    verify_totp,
)
from payroll.common.crypto import encrypt_secret

GOOD_PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 10, 31, 12, 0, 10, tzinfo=UTC)
NOW_STEP = int(NOW.timestamp()) // 30


def _user(**overrides) -> User:
    values = {
        "email": "owner@example.com",
        "display_name": "Owner",
        "password_hash": "x",
        "role": "owner",
    }
    return User(**(values | overrides))


class _SpyHasher:
    """Wraps the real hasher and records every password it is handed."""

    def __init__(self, real):
        self.real = real
        self.hashed: list[str] = []
        self.verified: list[str] = []

    def hash(self, password):
        self.hashed.append(password)
        return self.real.hash(password)

    def verify(self, password_hash, password):
        self.verified.append(password)
        return self.real.verify(password_hash, password)


@pytest.fixture
def spy_hasher(monkeypatch):
    spy = _SpyHasher(security._hasher)
    monkeypatch.setattr(security, "_hasher", spy)
    return spy


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_is_argon2id():
    password_hash = hash_password(GOOD_PASSWORD)
    assert password_hash.startswith("$argon2id$")
    assert verify_password(GOOD_PASSWORD, password_hash) is True
    assert verify_password(GOOD_PASSWORD + "!", password_hash) is False


def test_hash_is_salted():
    assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)


def test_verify_unknown_user_hash_none_returns_false_and_takes_similar_time(spy_hasher):
    """Review Focus 1: an unknown user (no hash) costs a full Argon2 verify."""
    real_hash = security._hasher.real.hash(GOOD_PASSWORD)

    def timed(password_hash) -> float:
        start = time.perf_counter()
        assert verify_password("wrong password!!", password_hash) is False
        return time.perf_counter() - start

    unknown = statistics.median(timed(None) for _ in range(5))
    known = statistics.median(timed(real_hash) for _ in range(5))

    # The dummy verify really ran against an Argon2 hash, every time.
    assert len(spy_hasher.verified) == 10
    assert security._DUMMY_HASH.startswith("$argon2id$")
    # Within an order of magnitude of a real failed verify.
    assert known / 10 < unknown < known * 10


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$argon2id$v=19$m=1,t=1,p=1$bad"])
def test_verify_malformed_hash_returns_false(bad_hash):
    assert verify_password(GOOD_PASSWORD, bad_hash) is False


def test_unicode_password_roundtrip():
    password = "Grüße-秘密-パスワード-🔒-ünïcödé"
    password_hash = hash_password(password)
    assert verify_password(password, password_hash) is True
    assert verify_password("Gruße-秘密-パスワード-🔒-ünïcödé", password_hash) is False


def test_unicode_password_is_nfkc_normalized():
    """Composed and decomposed forms of the same text are the same password."""
    composed = unicodedata.normalize("NFC", "café-crème-brûlée")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    password_hash = hash_password(composed)
    assert verify_password(decomposed, password_hash) is True
    # NFKC also folds compatibility forms (fullwidth letters -> ASCII).
    assert verify_password("ｃａｆé-crème-brûlée", password_hash) is True


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------


def test_policy_rejects_short_long_and_common(spy_hasher):
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("a1b2c3d4e5f")  # 11 chars
    with pytest.raises(PasswordPolicyError):
        validate_password_policy("x" * 129)

    # Review Focus 3: a 10 MB password is refused before it can reach Argon2.
    giant = "x" * (10 * 1024 * 1024)
    with pytest.raises(PasswordPolicyError):
        validate_password_policy(giant)
    with pytest.raises(PasswordPolicyError):
        hash_password(giant)
    assert verify_password(giant, security._hasher.real.hash(GOOD_PASSWORD)) is False
    assert verify_password(giant, None) is False
    assert spy_hasher.hashed == []
    assert all(len(p) <= security.MAX_PASSWORD_LENGTH for p in spy_hasher.verified)

    # Common passwords are rejected case-insensitively.
    for common in ("unbelievable", "UNBELIEVABLE", "UnBelievable"):
        with pytest.raises(PasswordPolicyError):
            validate_password_policy(common)
    if "password12345" in security.COMMON_PASSWORDS:
        with pytest.raises(PasswordPolicyError):
            validate_password_policy("Password12345")


def test_policy_accepts_bounds():
    validate_password_policy("q7#Lm2!vX9zR")  # exactly 12
    validate_password_policy("q7#Lm2!vX9zR" * 10 + "abcdefgh")  # exactly 128
    validate_password_policy(GOOD_PASSWORD)


def test_policy_error_has_message():
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password_policy("short")
    assert "12" in str(exc.value)


def test_common_password_list_is_vendored_10k():
    assert len(security.COMMON_PASSWORDS) >= 10_000
    assert "password" in security.COMMON_PASSWORDS
    assert all(p == p.lower() for p in security.COMMON_PASSWORDS)


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def _totp_user(secret: str, **overrides) -> User:
    return _user(totp_secret_enc=encrypt_secret(secret), **overrides)


def _code(secret: str, step: int) -> str:
    return pyotp.TOTP(secret).at(step * 30)


def test_new_totp_secret_is_base32_and_random():
    secret = new_totp_secret()
    assert re.fullmatch(r"[A-Z2-7]{32}", secret)
    assert secret != new_totp_secret()


def test_totp_uri_contains_issuer_and_email():
    secret = new_totp_secret()
    uri = totp_uri(secret, "owner@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "owner%40example.com" in uri
    assert f"secret={secret}" in uri
    assert "issuer=DCI%20Books" in uri


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_totp_accepts_current_and_adjacent_step(offset):
    secret = new_totp_secret()
    user = _totp_user(secret)
    assert verify_totp(user, _code(secret, NOW_STEP + offset), now=NOW) is True
    assert user.totp_last_step == NOW_STEP + offset


@pytest.mark.parametrize("offset", [-3, -2, 2, 3])
def test_totp_rejects_steps_outside_window(offset):
    secret = new_totp_secret()
    user = _totp_user(secret)
    assert verify_totp(user, _code(secret, NOW_STEP + offset), now=NOW) is False
    assert user.totp_last_step is None


def test_totp_rejects_replay():
    """Review Focus 2: a code that already succeeded cannot be used again."""
    secret = new_totp_secret()
    user = _totp_user(secret)
    code = _code(secret, NOW_STEP)
    assert verify_totp(user, code, now=NOW) is True
    assert verify_totp(user, code, now=NOW) is False
    # Still rejected 30 s later, while the step is inside the ±1 window.
    assert verify_totp(user, code, now=NOW + timedelta(seconds=30)) is False
    assert user.totp_last_step == NOW_STEP


def test_totp_rejects_old_step():
    """Any step <= the last accepted step is rejected, even inside the window."""
    secret = new_totp_secret()
    user = _totp_user(secret)
    assert verify_totp(user, _code(secret, NOW_STEP + 1), now=NOW) is True
    assert verify_totp(user, _code(secret, NOW_STEP), now=NOW) is False
    assert verify_totp(user, _code(secret, NOW_STEP - 1), now=NOW) is False
    assert user.totp_last_step == NOW_STEP + 1


def test_totp_rejects_step_equal_to_last_step_set_elsewhere():
    secret = new_totp_secret()
    user = _totp_user(secret, totp_last_step=NOW_STEP)
    assert verify_totp(user, _code(secret, NOW_STEP), now=NOW) is False
    assert verify_totp(user, _code(secret, NOW_STEP + 1), now=NOW) is True


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5x"])
def test_totp_rejects_malformed_codes(bad):
    user = _totp_user(new_totp_secret())
    assert verify_totp(user, bad, now=NOW) is False
    assert user.totp_last_step is None


def test_totp_wrong_code_does_not_move_last_step():
    secret = new_totp_secret()
    user = _totp_user(secret)
    good = _code(secret, NOW_STEP)
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    assert verify_totp(user, wrong, now=NOW) is False
    assert user.totp_last_step is None


def test_totp_tolerates_spaces_and_naive_utc_now():
    secret = new_totp_secret()
    user = _totp_user(secret)
    code = _code(secret, NOW_STEP)
    naive_now = NOW.replace(tzinfo=None)
    assert verify_totp(user, f" {code[:3]} {code[3:]} ", now=naive_now) is True


def test_totp_without_secret_is_false():
    assert verify_totp(_user(), "123456", now=NOW) is False


def test_totp_defaults_to_current_time():
    secret = new_totp_secret()
    user = _totp_user(secret)
    assert verify_totp(user, pyotp.TOTP(secret).now()) is True


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

CODE_RE = re.compile(r"[a-z2-9]{5}-[a-z2-9]{5}")


def test_generate_recovery_codes_format():
    codes = generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for code in codes:
        assert CODE_RE.fullmatch(code), code
        assert not set(code) & set("01ilo")  # no ambiguous characters
    assert len(generate_recovery_codes(3)) == 3


def test_hash_recovery_code_is_sha256_hex_and_normalized():
    digest = hash_recovery_code("abcde-fghjk")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert hash_recovery_code("  ABCDE-FGHJK\n") == digest
    assert hash_recovery_code("abcde fghjk") == digest
    assert hash_recovery_code("abcdefghjk") == digest
    assert hash_recovery_code("abcde-fghjm") != digest


def _persist_user_with_codes(session, email: str) -> tuple[User, list[str]]:
    user = _user(email=email)
    session.add(user)
    session.flush()
    codes = generate_recovery_codes()
    for code in codes:
        session.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)))
    session.flush()
    return user, codes


def test_recovery_code_single_use(session):
    user, codes = _persist_user_with_codes(session, "owner@example.com")

    assert consume_recovery_code(session, user.id, codes[0]) is True
    assert consume_recovery_code(session, user.id, codes[0]) is False

    # Case and whitespace are normalized.
    assert consume_recovery_code(session, user.id, f"  {codes[1].upper()} ") is True
    assert consume_recovery_code(session, user.id, codes[1]) is False

    assert consume_recovery_code(session, user.id, "zzzzz-zzzzz") is False
    assert consume_recovery_code(session, user.id, "") is False

    rows = session.exec(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).all()
    used = [r for r in rows if r.used_at is not None]
    assert len(used) == 2


def test_recovery_code_is_scoped_to_its_user(session):
    owner, owner_codes = _persist_user_with_codes(session, "owner@example.com")
    other, _ = _persist_user_with_codes(session, "other@example.com")

    assert consume_recovery_code(session, other.id, owner_codes[0]) is False
    assert consume_recovery_code(session, owner.id, owner_codes[0]) is True
