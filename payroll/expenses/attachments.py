"""Receipt attachments: append-only files on an expense, typed from their bytes.

Files live under ``{root}/{yyyy}/{mm}/{32 random hex}``; the database row keeps the
display name, the detected type, the sha256 and the path relative to ``root``. There is
deliberately no way to remove or change an attachment (the table refuses UPDATE and
DELETE too). Attachments carry no money, so neither the ledger nor the period lock is
involved. The per-expense limits are checked under a lock on the expense row, so
concurrent uploads cannot race past them.
"""

import contextlib
import hashlib
import logging
import os
import secrets
import unicodedata
from pathlib import Path
from typing import Literal

from sqlmodel import Session, col, func, select

from payroll import app_settings
from payroll.audit.services import record as record_audit
from payroll.auth.models import User
from payroll.auth.permissions import OWNER, ensure_role
from payroll.common.documents import commit_or_rollback, is_int, utcnow_naive
from payroll.expenses.exceptions import (
    AttachmentNotFoundError,
    ExpenseNotFoundError,
    InvalidAttachmentError,
)
from payroll.expenses.models import MAX_ATTACHMENT_BYTES, Attachment, Expense

logger = logging.getLogger(f"{app_settings.logger_name}.expenses.attachments")

MAX_FILE_BYTES = MAX_ATTACHMENT_BYTES
MAX_FILES = 10
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILENAME = 150
FALLBACK_NAME = "receipt"
HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Unicode categories removed from names: controls (incl. NUL), format characters (bidi
# overrides and isolates, zero-width), and line/paragraph separators.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _invalid(message: str) -> InvalidAttachmentError:
    return InvalidAttachmentError(message=message)


def detect_type(data: bytes) -> str | None:
    """The content type of ``data`` from its magic bytes alone, or None if not allowed."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS:
        return "image/heic"
    return None


def safe_filename(name: str) -> str:
    """A display-safe basename: no path, no control characters, at most 150 characters."""
    text = name if isinstance(name, str) else ""
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _STRIPPED_CATEGORIES)
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if len(text) > MAX_FILENAME:
        stem, ext = os.path.splitext(text)
        if ext and len(ext) < MAX_FILENAME // 2:
            text = stem[: MAX_FILENAME - len(ext)] + ext
        else:
            text = text[:MAX_FILENAME]
        text = text.strip()
    if text in ("", ".", ".."):
        return FALLBACK_NAME
    return text


def _make_dirs(root: Path, *parts: str) -> Path:
    """Create ``root/parts...`` with mode 0700 on every directory this call creates."""
    current = root
    for part in ("", *parts):
        current = current / part if part else current
        if not current.exists():
            current.mkdir(mode=0o700)
            current.chmod(0o700)
    return current


def _write_file(directory: Path, name: str, data: bytes) -> None:
    """Write ``data`` to ``directory/name`` via a temp file, fsync, then an atomic rename."""
    temp = directory / f".{name}.tmp"
    final = directory / name
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


class AttachmentService:
    def __init__(self, session: Session, root: Path | None = None) -> None:
        self.session = session
        self.root = Path(root) if root is not None else Path(app_settings.attachment_dir)

    # ------------------------------------------------------------------ reads

    def get(self, attachment_id: int) -> Attachment:
        row = self.session.get(Attachment, attachment_id)
        if row is None:
            raise AttachmentNotFoundError()
        return row

    def list(self, owner_type: str, owner_id: int) -> list[Attachment]:
        stmt = (
            select(Attachment)
            .where(Attachment.owner_type == owner_type, Attachment.owner_id == owner_id)
            .order_by(col(Attachment.id))
        )
        return list(self.session.exec(stmt).all())

    def open(self, attachment_id: int) -> tuple[Attachment, Path]:
        """The row and the path of its stored file; the file must exist inside the root."""
        row = self.get(attachment_id)
        root = self.root.resolve()
        path = (root / row.stored_name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise AttachmentNotFoundError()
        return row, path

    # ------------------------------------------------------------------ writes

    def add(
        self,
        *,
        actor: User,
        owner_type: Literal["expense"],
        owner_id: int,
        filename: str,
        data: bytes,
    ) -> Attachment:
        ensure_role(actor, OWNER)

        # (1) everything that needs no database access
        if owner_type != "expense":
            raise _invalid("Receipts can only be attached to an expense")
        if not is_int(owner_id):
            raise _invalid("Choose the expense to attach the receipt to")
        if not isinstance(data, bytes):
            raise _invalid("The file must be sent as bytes")
        if len(data) == 0:
            raise _invalid("The file is empty")
        if len(data) > MAX_FILE_BYTES:
            raise _invalid("A receipt cannot be larger than 10 MB")
        content_type = detect_type(data)
        if content_type is None:
            raise _invalid("Only PDF, PNG, JPEG and HEIC files can be attached")
        display_name = safe_filename(filename)
        digest = hashlib.sha256(data).hexdigest()
        stored: Path | None = None

        def work() -> Attachment:
            nonlocal stored
            # (2) serialise on the expense row, then check the per-expense limits
            expense = self.session.exec(
                select(Expense)
                .where(Expense.id == owner_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            ).first()
            if expense is None:
                raise ExpenseNotFoundError()
            count, total = self.session.exec(
                select(func.count(), func.coalesce(func.sum(Attachment.size), 0)).where(
                    Attachment.owner_type == owner_type, Attachment.owner_id == owner_id
                )
            ).one()
            if count >= MAX_FILES:
                raise _invalid("An expense can have at most 10 receipts")
            if total + len(data) > MAX_TOTAL_BYTES:
                raise _invalid("An expense's receipts cannot total more than 50 MB")
            duplicate = self.session.exec(
                select(Attachment.id).where(
                    Attachment.owner_type == owner_type,
                    Attachment.owner_id == owner_id,
                    Attachment.sha256 == digest,
                )
            ).first()
            if duplicate is not None:
                raise _invalid("This file is already attached to the expense")

            # (3) write the file, then the row
            now = utcnow_naive()
            year, month = f"{now.year:04d}", f"{now.month:02d}"
            directory = _make_dirs(self.root, year, month)
            name = secrets.token_hex(16)
            stored = directory / name
            _write_file(directory, name, data)
            attachment = Attachment(
                owner_type=owner_type,
                owner_id=owner_id,
                filename=display_name,
                content_type=content_type,
                sha256=digest,
                stored_name=f"{year}/{month}/{name}",
                size=len(data),
                uploaded_at=now,
                uploaded_by=actor.id,  # type: ignore[arg-type]
            )
            self.session.add(attachment)
            self.session.flush()
            record_audit(
                self.session,
                action="attachment.add",
                user_id=actor.id,
                entity="attachment",
                entity_id=str(attachment.id),
                after={
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "filename": display_name,
                    "content_type": content_type,
                    "sha256": digest,
                    "size": len(data),
                },
            )
            return attachment

        try:
            attachment = commit_or_rollback(self.session, work)
        except BaseException:
            if stored is not None:
                with contextlib.suppress(OSError):
                    stored.unlink(missing_ok=True)
            raise
        logger.info("Added attachment id=%s to expense id=%s", attachment.id, owner_id)
        return attachment
