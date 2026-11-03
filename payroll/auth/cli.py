"""
Bootstrap and recovery CLI: ``python -m payroll.auth.cli <command>``.

Commands (passwords are always prompted for twice with ``getpass``; they are never
accepted via argv or the environment and never printed):

- ``create-owner --email E [--name N] [--force-additional]``: first run. Refuses
  when any user already exists unless ``--force-additional``.
- ``create-user --email E --role accountant|viewer [--name N]``: other roles
  (an owner is created with ``create-owner``).
- ``reset-owner --email E``: recovery. Sets a new password, clears TOTP and recovery
  codes, zeroes the lockout counters, reactivates the account and revokes every
  session; the user re-enrolls MFA at the next login. Despite the name it works for
  any user's email.

When run as a program the CLI first applies pending migrations
(:func:`payroll.migrations.alembic_runner.upgrade`), so it works on a brand-new
database before the app has ever started. Exit codes: 0 ok, 1 error (message on
stderr), 2 argparse usage error.
"""

import argparse
import getpass
import sys
from collections.abc import Sequence

from alembic.util.exc import CommandError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, func, select

from payroll import app_settings
from payroll.audit.services import record
from payroll.auth import sessions
from payroll.auth.models import RecoveryCode, User, UserRole
from payroll.auth.security import PasswordPolicyError, hash_password, validate_password_policy

# Serializes concurrent create-owner runs (arbitrary constant, unique to this app).
_BOOTSTRAP_LOCK_KEY = 7_322_918_604_150_422


class CliError(Exception):
    """A user-facing error: shown on stderr, exit code 1."""


def _lock_bootstrap(db: Session) -> None:
    """Transaction-scoped advisory lock: held until the caller commits or rolls back."""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK_KEY})


def _find_user(db: Session, email: str) -> User | None:
    return db.exec(select(User).where(User.email == email.strip().lower())).first()


def create_user(
    db: Session, email: str, password: str, role: str, display_name: str | None
) -> User:
    """
    Add a user (no commit). Validates the password policy, rejects a duplicate email
    case-insensitively and writes an ``auth.user.created`` audit row (email and role
    only, never the password or hash). ``display_name`` defaults to the email's
    local part.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise CliError(f"Invalid email address: {email!r}")
    role = UserRole(role).value
    validate_password_policy(password)
    if _find_user(db, email) is not None:
        raise CliError(f"A user with email {email} already exists.")
    user = User(
        email=email,
        display_name=(display_name or email.split("@", 1)[0]).strip(),
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.flush()
    record(
        db,
        action="auth.user.created",
        entity="user",
        entity_id=str(user.id),
        after={"email": user.email, "role": user.role},
    )
    return user


def reset_user(db: Session, email: str, new_password: str) -> User:
    """
    Recover an account (no commit): new password, TOTP cleared, recovery codes
    deleted, lockout counters zeroed, reactivated, every session revoked. Writes an
    ``auth.user.reset`` audit row (email and role only).
    """
    user = _find_user(db, email)
    if user is None:
        raise CliError(f"No user with email {email.strip().lower()}.")
    validate_password_policy(new_password)
    user.password_hash = hash_password(new_password)
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_last_step = None
    user.failed_logins = 0
    user.lock_count = 0
    user.locked_until = None
    user.is_active = True
    db.add(user)
    for code in db.exec(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).all():
        db.delete(code)
    sessions.revoke_all(db, user.id)
    db.flush()
    record(
        db,
        action="auth.user.reset",
        entity="user",
        entity_id=str(user.id),
        after={"email": user.email, "role": user.role},
    )
    return user


def _prompt_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm password: "):
        raise CliError("Passwords do not match.")
    return first


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m payroll.auth.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    owner = sub.add_parser("create-owner", help="create the first owner account")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name")
    owner.add_argument(
        "--force-additional", action="store_true", help="allow when users already exist"
    )

    user = sub.add_parser("create-user", help="create an accountant or viewer")
    user.add_argument("--email", required=True)
    user.add_argument("--role", required=True, choices=[UserRole.ACCOUNTANT, UserRole.VIEWER])
    user.add_argument("--name")

    reset = sub.add_parser("reset-owner", help="reset a user's password and MFA")
    reset.add_argument("--email", required=True)
    return parser


def _run(args: argparse.Namespace, db: Session) -> str:
    if args.command == "create-owner":
        if not args.force_additional:
            # Lock BEFORE counting so two simultaneous runs cannot both see zero users.
            _lock_bootstrap(db)
        existing = db.exec(select(func.count()).select_from(User)).one()
        if existing and not args.force_additional:
            raise CliError("Users already exist; pass --force-additional to add another owner.")
        user = create_user(db, args.email, _prompt_password(), UserRole.OWNER, args.name)
        return f"Created owner {user.email}."
    if args.command == "create-user":
        user = create_user(db, args.email, _prompt_password(), args.role, args.name)
        return f"Created {user.role} {user.email}."
    user = reset_user(db, args.email, _prompt_password())
    return f"Reset {user.email}; MFA must be re-enrolled at next login."


def main(argv: Sequence[str] | None = None, db: Session | None = None) -> int:
    """
    Run one command. ``db`` is for tests; otherwise migrations are applied and a
    session on the application engine is used. The command's transaction is
    committed only on success.
    """
    args = _build_parser().parse_args(argv)
    try:
        if db is not None:
            message = _run(args, db)
            db.commit()
        else:
            from payroll.migrations import alembic_runner

            alembic_runner.upgrade()
            engine = create_engine(app_settings.database_url)
            try:
                with Session(engine) as session:
                    message = _run(args, session)
                    session.commit()
            finally:
                engine.dispose()
    except (CliError, PasswordPolicyError) as exc:
        # An injected ``db`` is the caller's transaction; our own session rolls back on close.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except IntegrityError as exc:
        # Never print str(exc): it includes the bound parameters (the password hash).
        if db is not None:
            db.rollback()
        if "app_user_email" in str(exc.orig) or "email" in str(exc.orig):
            print("error: A user with that email already exists.", file=sys.stderr)
        else:
            print(
                f"error: Database error: {type(exc).__name__}; nothing was changed.",
                file=sys.stderr,
            )
        return 1
    except (SQLAlchemyError, CommandError) as exc:
        if db is not None:
            db.rollback()
        print(f"error: Database error: {type(exc).__name__}; nothing was changed.", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
