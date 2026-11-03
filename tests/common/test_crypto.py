import base64
import os

import pytest
from pydantic import ValidationError

from payroll import Settings
from payroll.common.crypto import decrypt_secret, derive_subkey, encrypt_secret

OTHER_KEY = os.urandom(32)


def test_round_trip():
    assert decrypt_secret(encrypt_secret("s3cret-ünïcode")) == "s3cret-ünïcode"


def test_same_plaintext_encrypts_differently():
    assert encrypt_secret("abc") != encrypt_secret("abc")


def test_flipped_byte_raises():
    raw = bytearray(base64.urlsafe_b64decode(encrypt_secret("abc")))
    raw[-1] ^= 0x01
    with pytest.raises(ValueError):
        decrypt_secret(base64.urlsafe_b64encode(bytes(raw)).decode())


def test_wrong_key_raises():
    token = encrypt_secret("abc")
    with pytest.raises(ValueError):
        decrypt_secret(token, key=OTHER_KEY)


def test_explicit_key_round_trip():
    token = encrypt_secret("abc", key=OTHER_KEY)
    assert decrypt_secret(token, key=OTHER_KEY) == "abc"


@pytest.mark.parametrize("token", ["", "not base64!!", "AAAA", "x" * 5])
def test_malformed_token_raises(token):
    with pytest.raises(ValueError):
        decrypt_secret(token)


@pytest.mark.parametrize("bad", ["", "not-base64!", base64.b64encode(b"short").decode()])
def test_settings_rejects_bad_key(bad):
    with pytest.raises(ValidationError, match="32 bytes"):
        Settings(encryption_key=bad)


def test_settings_auth_defaults():
    s = Settings(encryption_key=base64.b64encode(os.urandom(32)).decode())
    assert s.session_cookie_name == "dci_session"
    assert s.session_cookie_secure is True
    assert (s.session_idle_minutes, s.session_absolute_hours) == (30, 12)
    assert (s.max_failed_logins, s.lockout_minutes) == (5, 15)
    assert s.totp_issuer == "DCI Books"


@pytest.mark.parametrize("bad", ["Zm9vYmFyYmF6cXV4LW5lYXItdmFsaWQta2V5", "not-base64-SECRETVALUE!"])
def test_settings_error_does_not_echo_key(bad):
    with pytest.raises(ValidationError) as exc_info:
        Settings(encryption_key=bad)
    for text in (str(exc_info.value), repr(exc_info.value)):
        assert bad not in text
    assert "ENCRYPTION_KEY must" in str(exc_info.value)


def test_settings_repr_hides_key():
    key = base64.b64encode(os.urandom(32)).decode()
    s = Settings(encryption_key=key)
    assert key not in repr(s)
    assert key not in str(s)


@pytest.mark.parametrize("bad_key", [b"", b"short", os.urandom(16), os.urandom(33)])
def test_key_override_must_be_32_bytes(bad_key):
    with pytest.raises(ValueError):
        encrypt_secret("abc", key=bad_key)
    with pytest.raises(ValueError):
        decrypt_secret(encrypt_secret("abc"), key=bad_key)


def test_derive_subkey_is_deterministic_and_label_separated():
    key = derive_subkey("payroll/recovery-code/v1")
    assert len(key) == 32
    assert derive_subkey("payroll/recovery-code/v1") == key
    assert derive_subkey("payroll/other/v1") != key
    # Derived from, but not equal to, the configured encryption key.
    assert key != base64.b64decode(os.environ["ENCRYPTION_KEY"])
