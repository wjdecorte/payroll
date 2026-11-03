"""
Auth tables: users, their one-time recovery codes, and login sessions.

Table names avoid the SQL reserved words ``user`` and ``session``. Timestamps are
naive UTC, like :class:`~payroll.common.models.AppBaseModel`.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint
from sqlmodel import Field

from payroll.common.models import AppBaseModel


def utcnow() -> datetime:
    """The current time as a naive UTC datetime (the convention for every table)."""
    return datetime.now(UTC).replace(tzinfo=None)


class UserRole(StrEnum):
    OWNER = "owner"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"


_ROLE_VALUES = ", ".join(f"'{role.value}'" for role in UserRole)


class User(AppBaseModel, table=True):
    """
    A person who can sign in. ``email`` is stored lower-cased and is unique;
    ``role`` is one of :class:`UserRole`. Both are normalized/validated on
    assignment (table models skip Pydantic validation) and enforced by CHECK
    constraints in the database.
    """

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="ck_app_user_role"),
        CheckConstraint("email = lower(email)", name="ck_app_user_email_lower"),
    )

    email: str = Field(max_length=320, unique=True, index=True)
    display_name: str = Field(max_length=200)
    password_hash: str = Field(max_length=255)
    role: str = Field(max_length=20)
    # TOTP secret encrypted with payroll.common.crypto.encrypt_secret.
    totp_secret_enc: str | None = Field(default=None)
    totp_enabled: bool = Field(default=False)
    # Last accepted TOTP time step (unix time // 30); later codes must be newer.
    totp_last_step: int | None = Field(default=None)
    failed_logins: int = Field(default=0)
    locked_until: datetime | None = Field(default=None)
    lock_count: int = Field(default=0)
    is_active: bool = Field(default=True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "email" and isinstance(value, str):
            value = value.strip().lower()
        elif name == "role":
            value = UserRole(value).value  # ValueError for an unknown role
        super().__setattr__(name, value)


class RecoveryCode(AppBaseModel, table=True):
    """A one-time MFA recovery code, stored as the sha256 hex of its normalized form."""

    __tablename__ = "recovery_code"

    user_id: int = Field(foreign_key="app_user.id", ondelete="CASCADE", index=True)
    code_hash: str = Field(max_length=64)
    used_at: datetime | None = Field(default=None)


class AuthSession(AppBaseModel, table=True):
    """
    A login session. ``id`` is the sha256 hex of the random cookie token, so the
    token itself is never stored.
    """

    __tablename__ = "auth_session"

    id: str = Field(primary_key=True, max_length=64)
    user_id: int = Field(foreign_key="app_user.id", ondelete="CASCADE", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    ip: str | None = Field(default=None, max_length=45)
    user_agent: str | None = Field(default=None, max_length=512)
    mfa_verified: bool = Field(default=False)
    csrf_token: str = Field(max_length=128)
