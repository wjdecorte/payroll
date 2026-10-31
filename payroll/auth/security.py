"""
Password, TOTP and recovery-code primitives (SPEC SEC2/SEC3, DESIGN §8).

- Passwords: Unicode NFKC-normalized, then hashed with Argon2id (argon2-cffi
  defaults). Over-long input is refused before it can reach the hasher.
- TOTP: RFC 6238 via pyotp, 30 s step, ±1 step tolerance, replay-protected by
  ``User.totp_last_step``.
- Recovery codes: ``xxxxx-xxxxx`` from :mod:`secrets`, stored as a per-user
  HMAC-SHA256 keyed from the encryption key, consumed single-use with an atomic
  conditional UPDATE.
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
from sqlalchemy import delete, update
from sqlmodel import Session

from payroll import app_settings
from payroll.auth.models import RecoveryCode, User, utcnow
from payroll.common.crypto import decrypt_secret, derive_subkey

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept the previous, current and next step

RECOVERY_CODE_COUNT = 10
# Lowercase letters and digits minus the look-alikes 0/o, 1/l/i.
RECOVERY_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_CODE_KEY_LABEL = "payroll/recovery-code/v1"  # derive_subkey label for code hashes
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


def _unusable_reason(password: str) -> str | None:
    """
    Why ``password`` must never reach the hasher, or None if it may.

    Over-long input is refused (NFKC can expand text, so both the raw input, a
    cheap guard that keeps huge strings away from the normalizer, and the
    normalized form are checked), as is text that cannot be UTF-8 encoded (lone
    surrogates), which Argon2 would otherwise raise on.
    """
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        return "Password contains characters that cannot be used."
    if len(_normalize_password(password)) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters long."
    return None


def validate_password_policy(password: str) -> None:
    """Raise :class:`PasswordPolicyError` unless ``password`` meets the policy."""
    reason = _unusable_reason(password)
    if reason is not None:
        raise PasswordPolicyError(reason)
    normalized = _normalize_password(password)
    if len(normalized) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if normalized.lower() in COMMON_PASSWORDS:
        raise PasswordPolicyError("Password is too common; choose a less guessable one.")


def hash_password(password: str) -> str:
    """
    Argon2id hash of the NFKC-normalized password. Raises
    :class:`PasswordPolicyError` for over-long or non-encodable input.
    """
    reason = _unusable_reason(password)
    if reason is not None:
        raise PasswordPolicyError(reason)
    return _hasher.hash(_normalize_password(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    """
    True only if ``password`` matches ``password_hash``. Never raises.

    With ``password_hash=None`` (unknown user) or an unusable password (over-long
    or not UTF-8 encodable), a verify against a module-level dummy hash still runs
    so the response takes about as long as a real failed attempt; the result is
    always ``False``. A malformed stored hash also gives ``False``.
    """
    if password_hash is None or _unusable_reason(password) is not None:
        _dummy_verify()
        return False
    try:
        return _hasher.verify(password_hash, _normalize_password(password))
    except VerificationError, InvalidHashError, ValueError:
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True if ``password_hash`` used weaker parameters than the current hasher."""
    return _hasher.check_needs_rehash(password_hash)


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
    ``user.totp_last_step`` to the matched step. ``now`` defaults to the current
    time; a naive ``now`` is taken as UTC.

    The caller MUST load the user row with ``SELECT ... FOR UPDATE``
    (``select(User).where(...).with_for_update()``) and commit after this call;
    otherwise two concurrent logins can both accept the same code.

    ``totp_enabled`` is deliberately ignored: enrollment verifies a first code
    before TOTP is switched on. A corrupt ``totp_secret_enc`` (or one encrypted
    under a different key) raises ``ValueError`` from ``decrypt_secret``.
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


def hash_recovery_code(code: str, user_id: int) -> str:
    """
    Keyed hash of a recovery code for one user (64 hex chars):
    ``HMAC-SHA256(K, f"{user_id}:{normalized code}")`` with ``K`` derived from the
    encryption key. Case, whitespace and hyphens are ignored. The server key stops
    a leaked table being brute-forced offline, and binding the user id stops one
    guess being checked against every user's codes at once.
    """
    key = derive_subkey(RECOVERY_CODE_KEY_LABEL)
    message = f"{user_id}:{_normalize_recovery_code(code)}".encode("utf-8", "replace")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def issue_recovery_codes(session: Session, user_id: int, n: int = RECOVERY_CODE_COUNT) -> list[str]:
    """
    Replace ``user_id``'s recovery codes with ``n`` new ones and return them in
    plain text (show them once; only their keyed hashes are stored). Any earlier
    codes, used or not, stop working. The caller commits the transaction.
    """
    session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    codes = generate_recovery_codes(n)
    for code in codes:
        session.add(RecoveryCode(user_id=user_id, code_hash=hash_recovery_code(code, user_id)))
    return codes


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
            RecoveryCode.code_hash == hash_recovery_code(code, user_id),
            RecoveryCode.used_at.is_(None),
        )
        .values(used_at=utcnow(), modify_date=utcnow())
    )
    return result.rowcount == 1
