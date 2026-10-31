"""
Password, TOTP and recovery-code primitives (SPEC SEC2/SEC3, DESIGN §8).

- Passwords: Unicode NFKC-normalized, then hashed with Argon2id (argon2-cffi
  defaults). Over-long input is refused before it can reach the hasher.
- TOTP: RFC 6238 via pyotp, 30 s step, ±1 step tolerance, replay-protected by
  ``User.totp_last_step``.
- Recovery codes: ``xxxxx-xxxxx`` from :mod:`secrets`, stored as sha256 hex and
  consumed single-use with an atomic conditional UPDATE.
"""

import hashlib
import hmac
import secrets
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import update
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.models import RecoveryCode, User, utcnow
from payroll.common.crypto import decrypt_secret

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept the previous, current and next step

RECOVERY_CODE_COUNT = 10
# Lowercase letters and digits minus the look-alikes 0/o, 1/l/i.
RECOVERY_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_RECOVERY_GROUP = 5

_COMMON_PASSWORDS_FILE = Path(__file__).with_name("common_passwords.txt")


class PasswordPolicyError(ValueError):
    """The password does not meet the policy; ``str(exc)`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_common_passwords() -> frozenset[str]:
    with _COMMON_PASSWORDS_FILE.open(encoding="utf-8") as fh:
        return frozenset(line.strip().lower() for line in fh if line.strip())


COMMON_PASSWORDS: frozenset[str] = _load_common_passwords()

_hasher = PasswordHasher()  # Argon2id with the library's recommended parameters
# Hash used to keep "unknown user" as slow as "wrong password" (timing equalization).
_DUMMY_HASH: str = _hasher.hash(secrets.token_urlsafe(32))
_DUMMY_PASSWORD = "dummy-password-for-timing"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def _normalize_password(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _too_long(password: str) -> bool:
    # NFKC can expand text, so check both the raw input (cheap guard that keeps
    # huge strings away from the normalizer) and the normalized form.
    return (
        len(password) > MAX_PASSWORD_LENGTH
        or len(_normalize_password(password)) > MAX_PASSWORD_LENGTH
    )


def validate_password_policy(password: str) -> None:
    """Raise :class:`PasswordPolicyError` unless ``password`` meets the policy."""
    if _too_long(password):
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
        )
    normalized = _normalize_password(password)
    if len(normalized) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if normalized.lower() in COMMON_PASSWORDS:
        raise PasswordPolicyError("Password is too common; choose a less guessable one.")


def hash_password(password: str) -> str:
    """Argon2id hash of the NFKC-normalized password. Refuses over-long input."""
    if _too_long(password):
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
        )
    return _hasher.hash(_normalize_password(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    """
    True only if ``password`` matches ``password_hash``. Never raises.

    With ``password_hash=None`` (unknown user) or an over-long password, a verify
    against a module-level dummy hash still runs so the response takes about as
    long as a real failed attempt; the result is always ``False``.
    """
    if password_hash is None or _too_long(password):
        _dummy_verify()
        return False
    try:
        return _hasher.verify(password_hash, _normalize_password(password))
    except VerificationError, InvalidHashError:
        return False


def _dummy_verify() -> None:
    try:
        _hasher.verify(_DUMMY_HASH, _DUMMY_PASSWORD)
    except VerificationError, InvalidHashError:
        pass


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def new_totp_secret() -> str:
    """A fresh random base32 TOTP secret (160 bits). Store it with ``encrypt_secret``."""
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    """``otpauth://`` provisioning URI for authenticator apps (render as a QR code)."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=app_settings.totp_issuer)


def verify_totp(user: User, code: str, now: datetime | None = None) -> bool:
    """
    Check a 6-digit TOTP code for ``user`` (whose ``totp_secret_enc`` is set).

    Accepts the current step ±1, but never a step at or before
    ``user.totp_last_step`` (replay protection). On success sets
    ``user.totp_last_step`` to the matched step; the caller commits it. ``now``
    defaults to the current time; a naive ``now`` is taken as UTC.
    """
    if user.totp_secret_enc is None:
        return False
    candidate = "".join(code.split())
    if len(candidate) != TOTP_DIGITS or not (candidate.isascii() and candidate.isdigit()):
        return False

    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    current_step = int(now.timestamp()) // TOTP_STEP_SECONDS

    totp = pyotp.TOTP(decrypt_secret(user.totp_secret_enc))
    last_step = user.totp_last_step
    matched: int | None = None
    for step in range(current_step - TOTP_WINDOW, current_step + TOTP_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), candidate):
            matched = step
    if matched is None:
        return False
    user.totp_last_step = matched
    return True


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def _normalize_recovery_code(code: str) -> str:
    return "".join(code.split()).replace("-", "").lower()


def generate_recovery_codes(n: int = RECOVERY_CODE_COUNT) -> list[str]:
    """``n`` distinct random recovery codes of the form ``xxxxx-xxxxx``."""
    codes: set[str] = set()
    while len(codes) < n:
        raw = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(2 * _RECOVERY_GROUP))
        codes.add(f"{raw[:_RECOVERY_GROUP]}-{raw[_RECOVERY_GROUP:]}")
    return list(codes)


def hash_recovery_code(code: str) -> str:
    """sha256 hex of the normalized code (case, whitespace and hyphens ignored)."""
    return hashlib.sha256(_normalize_recovery_code(code).encode("ascii", "replace")).hexdigest()


def consume_recovery_code(session: Session, user_id: int, code: str) -> bool:
    """
    Mark ``code`` used for ``user_id`` and return True, or return False if it does
    not exist or was already used. A single conditional UPDATE makes this safe
    against concurrent use; the caller commits the transaction.
    """
    if len(_normalize_recovery_code(code)) != 2 * _RECOVERY_GROUP:
        return False
    result = session.execute(
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == hash_recovery_code(code),
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=utcnow(), modify_date=utcnow())
    )
    return result.rowcount == 1
