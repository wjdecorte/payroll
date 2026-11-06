"""Receipt attachments: type from bytes, limits, storage safety, append-only, concurrency."""

# ruff: noqa: F811  (fixtures re-exported by conftest are used as arguments)

import stat
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlmodel import Session, func, select

from payroll.audit.models import AuditLog
from payroll.auth.models import User
from payroll.auth.permissions import PermissionDeniedError
from payroll.exceptions import AppBaseError
from payroll.expenses import attachments as attachments_module
from payroll.expenses.attachments import AttachmentService, detect_type, safe_filename
from payroll.expenses.exceptions import (
    AttachmentNotFoundError,
    ExpenseNotFoundError,
    InvalidAttachmentError,
)
from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Attachment
from tests.expenses.test_expense_concurrency import exp_db  # noqa: F401
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
MB = 1024 * 1024
PDF = b"%PDF-1.4\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 10
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"
ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 50


@pytest.fixture(autouse=True)
def _committed_fixtures(session, owner_user, chart, expense_account, card):
    session.commit()


@pytest.fixture
def expense(session, owner_user, card, expense_account):
    row = ExpenseService(session).create(
        actor=owner_user,
        expense_date=date(2026, 3, 1),
        lines=[ExpenseLineInput(expense_account.id, D("10.00"))],
        source="account",
        paid_from_account_id=card.id,
        payee_text="Shop",
    )
    session.commit()
    return row


@pytest.fixture
def svc(session, tmp_path):
    return AttachmentService(session, tmp_path / "store")


def pdf(n: int, size: int = 0) -> bytes:
    """A distinct PDF per ``n``, padded to ``size`` bytes."""
    body = b"%PDF-1.4\n" + str(n).encode() + b"\n%%EOF"
    return body + b"0" * max(0, size - len(body))


def add(svc, user, expense, data=PDF, filename="r.pdf"):
    return svc.add(
        actor=user, owner_type="expense", owner_id=expense.id, filename=filename, data=data
    )


def count(session):
    return session.exec(select(func.count()).select_from(Attachment)).one()


def files(root):
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


# --------------------------------------------------------------- type detection


def test_detect_type_from_magic_bytes_only():
    assert detect_type(PDF) == "application/pdf"
    assert detect_type(PNG) == "image/png"
    assert detect_type(JPEG) == "image/jpeg"
    assert detect_type(HEIC) == "image/heic"
    assert detect_type(ELF) is None
    assert detect_type(b"  %PDF-1.4") is None  # offset 0 only
    assert detect_type(b"") is None
    assert detect_type(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8) is None  # an mp4, not HEIC


def test_elf_named_pdf_is_refused(session, svc, owner_user, expense):
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, ELF, "evil.pdf")
    assert count(session) == 0
    assert files(svc.root) == []


def test_real_pdf_named_exe_is_accepted_as_pdf_and_name_kept(svc, owner_user, expense):
    row = add(svc, owner_user, expense, PDF, "receipt.exe")
    assert row.content_type == "application/pdf"
    assert row.filename == "receipt.exe"


@pytest.mark.parametrize(
    ("data", "kind"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (HEIC, "image/heic"), (PDF, "application/pdf")],
)
def test_every_allowed_type_is_stored(svc, owner_user, expense, data, kind):
    row = add(svc, owner_user, expense, data, "x.bin")
    assert row.content_type == kind
    _, path = svc.open(row.id)
    assert path.read_bytes() == data


# --------------------------------------------------------------- file names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("a\\b.pdf", "b.pdf"),
        ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
        ("", "receipt"),
        ("   ", "receipt"),
        ("..", "receipt"),
        ("a/", "receipt"),
        ("rec\x00eipt.pdf", "receipt.pdf"),
        ("rec\u202eeipt.pdf", "receipt.pdf"),
        ("\u2066x\u2069.pdf", "x.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("e\u0301.pdf", "\u00e9.pdf"),  # NFC
        ("bad\ud800name.pdf", "badname.pdf"),  # lone surrogate (cannot be UTF-8 encoded)
        ("priv\ue000ate.pdf", "private.pdf"),  # private use (Co)
        ("un\u0378assigned.pdf", "unassigned.pdf"),  # unassigned (Cn)
        ("receipt.", "receipt"),
        ("receipt. . ", "receipt"),
        ("dir/receipt...", "receipt"),
        ("...", "receipt"),
        (".hidden", ".hidden"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_is_always_utf8_encodable_and_has_no_trailing_dot_or_space():
    name = safe_filename("x" * 149 + ". .pdf" + "\ud800" + " .")
    name.encode("utf-8")
    assert name == name.rstrip(" .") and len(name) <= 150


def test_long_filename_is_cut_to_150_keeping_the_extension():
    name = safe_filename("a" * 400 + ".pdf")
    assert len(name) == 150 and name.endswith(".pdf")
    assert len(safe_filename("b" * 400)) == 150


def test_hostile_names_are_stored_safely(svc, owner_user, expense):
    row = add(svc, owner_user, expense, PDF, "../../etc/passwd")
    assert row.filename == "passwd"
    _, path = svc.open(row.id)
    assert path.resolve().is_relative_to(svc.root.resolve())


# --------------------------------------------------------------- limits


def test_zero_byte_refused(session, svc, owner_user, expense):
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, b"")
    assert count(session) == 0


@pytest.mark.parametrize("bad", [bytearray(PDF), memoryview(PDF), PDF.decode(), None])
def test_data_must_be_bytes(svc, owner_user, expense, bad):
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, bad)


def test_exactly_10_mb_accepted_and_one_more_byte_refused(session, svc, owner_user, expense):
    row = add(svc, owner_user, expense, pdf(1, 10 * MB))
    assert row.size == 10 * MB
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, pdf(2, 10 * MB + 1))
    assert count(session) == 1


def test_eleventh_file_refused(session, svc, owner_user, expense):
    for n in range(10):
        add(svc, owner_user, expense, pdf(n))
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, pdf(10))
    assert count(session) == 10
    assert len(files(svc.root)) == 10


def test_total_over_50_mb_refused(session, svc, owner_user, expense):
    for n in range(5):
        add(svc, owner_user, expense, pdf(n, 10 * MB))  # exactly 50 MB so far
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, pdf(99))
    assert count(session) == 5


def test_duplicate_hash_on_same_expense_refused(session, svc, owner_user, expense):
    add(svc, owner_user, expense, PDF, "one.pdf")
    with pytest.raises(InvalidAttachmentError):
        add(svc, owner_user, expense, PDF, "two.pdf")
    assert count(session) == 1
    assert len(files(svc.root)) == 1


def test_same_bytes_allowed_on_a_different_expense(
    session, svc, owner_user, expense, card, expense_account
):
    other = ExpenseService(session).create(
        actor=owner_user,
        expense_date=date(2026, 3, 2),
        lines=[ExpenseLineInput(expense_account.id, D("5.00"))],
        source="account",
        paid_from_account_id=card.id,
        payee_text="Other",
    )
    session.commit()
    add(svc, owner_user, expense)
    add(svc, owner_user, other)
    assert count(session) == 2


def test_unknown_expense_and_owner_type(svc, owner_user, expense):
    with pytest.raises(ExpenseNotFoundError):
        svc.add(actor=owner_user, owner_type="expense", owner_id=99999, filename="a", data=PDF)
    with pytest.raises(InvalidAttachmentError):
        svc.add(actor=owner_user, owner_type="invoice", owner_id=expense.id, filename="a", data=PDF)
    with pytest.raises(InvalidAttachmentError):
        svc.add(actor=owner_user, owner_type="expense", owner_id="1", filename="a", data=PDF)


# --------------------------------------------------------------- roles, void, append-only


def test_viewer_and_accountant_refused(session, svc, viewer_user, accountant_user, expense):
    for user in (viewer_user, accountant_user):
        with pytest.raises(PermissionDeniedError):
            add(svc, user, expense)
    assert count(session) == 0


def test_attachment_on_a_void_expense(session, svc, owner_user, expense):
    ExpenseService(session).void(expense.id, actor=owner_user, void_date=date(2026, 3, 5))
    row = add(svc, owner_user, expense)
    assert row.owner_id == expense.id


def test_service_has_no_remove_or_delete(svc):
    names = [n for n in dir(svc) if not n.startswith("_")]
    assert not [n for n in names if "remove" in n or "delete" in n or "update" in n]
    assert not hasattr(svc, "remove") and not hasattr(svc, "delete")


def test_row_records_detected_type_hash_uploader_and_audit(session, svc, owner_user, expense):
    import hashlib

    row = add(svc, owner_user, expense, JPEG, "photo.png")
    assert row.sha256 == hashlib.sha256(JPEG).hexdigest()
    assert row.size == len(JPEG)
    assert row.uploaded_by == owner_user.id
    assert svc.get(row.id).id == row.id
    assert [a.id for a in svc.list("expense", expense.id)] == [row.id]
    audit = session.exec(select(AuditLog).where(AuditLog.action == "attachment.add")).one()
    assert audit.entity_id == str(row.id)


def test_get_unknown_raises(svc):
    with pytest.raises(AttachmentNotFoundError):
        svc.get(424242)


# --------------------------------------------------------------- storage


def test_storage_layout_and_modes(svc, owner_user, expense):
    row = add(svc, owner_user, expense)
    year, month, name = row.stored_name.split("/")
    assert len(year) == 4 and len(month) == 2 and len(name) == 32
    int(name, 16)
    root = svc.root.resolve()
    _, path = svc.open(row.id)
    assert path.resolve().is_relative_to(root)
    for directory in (root, root / year, root / year / month):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [name]  # no temp file left


def test_make_dirs_survives_a_racing_creator_and_only_chmods_what_it_made(tmp_path, monkeypatch):
    real_mkdir = Path.mkdir
    raced = []

    def racing_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if self.name == "2026" and not raced:
            raced.append(self)
            real_mkdir(self, mode=0o755)  # another request got there first
            raise FileExistsError(str(self))
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    root = tmp_path / "store"
    made = attachments_module._make_dirs(root, "2026", "11")
    assert made == root / "2026" / "11" and made.is_dir()
    assert stat.S_IMODE((root / "2026").stat().st_mode) == 0o755  # not ours: untouched
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(made.stat().st_mode) == 0o700
    # Calling again over existing directories is fine.
    assert attachments_module._make_dirs(root, "2026", "11") == made


def test_open_of_a_deleted_file_raises(svc, owner_user, expense):
    row = add(svc, owner_user, expense)
    _, path = svc.open(row.id)
    path.unlink()
    with pytest.raises(AttachmentNotFoundError):
        svc.open(row.id)


def test_open_refuses_a_stored_name_outside_the_root(session, svc, owner_user, expense, tmp_path):
    row = add(svc, owner_user, expense)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF)
    # the table refuses UPDATE, so build an unsaved row pointing outside the root
    forged = Attachment(
        id=None,
        owner_type="expense",
        owner_id=expense.id,
        filename="x",
        content_type="application/pdf",
        sha256="0" * 64,
        stored_name="../outside.pdf",
        size=1,
        uploaded_by=owner_user.id,
    )
    session.add(forged)
    session.flush()
    with pytest.raises(AttachmentNotFoundError):
        svc.open(forged.id)
    assert row.id != forged.id


def test_db_failure_on_commit_removes_the_file(session, svc, owner_user, expense, monkeypatch):
    def boom():
        raise RuntimeError("commit failed")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        add(svc, owner_user, expense)
    monkeypatch.undo()
    assert files(svc.root) == []
    assert count(session) == 0


def test_db_failure_on_flush_removes_the_file(session, svc, owner_user, expense, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(attachments_module, "record_audit", boom)
    with pytest.raises(RuntimeError):
        add(svc, owner_user, expense)
    monkeypatch.undo()
    assert files(svc.root) == []
    assert count(session) == 0


def test_write_failure_inserts_nothing(session, svc, owner_user, expense, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(attachments_module, "_write_file", boom)
    with pytest.raises(OSError):
        add(svc, owner_user, expense)
    monkeypatch.undo()
    assert count(session) == 0
    assert files(svc.root) == []


def test_default_root_is_the_configured_directory(session, monkeypatch, tmp_path):
    monkeypatch.setattr(attachments_module.app_settings, "attachment_dir", str(tmp_path / "cfg"))
    assert AttachmentService(session).root == tmp_path / "cfg"


# --------------------------------------------------------------- concurrency


def _waiters(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            sa.text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).scalar_one()


def test_two_concurrent_identical_uploads_store_exactly_one(exp_db, tmp_path):
    from tests.expenses.test_expense_concurrency import _create

    engine, ids = exp_db
    expense_id = _create(engine, ids, date(2026, 3, 1)).id
    root = tmp_path / "store"
    outcome: list[str] = ["", ""]

    def upload(index: int) -> None:
        with Session(engine) as db:
            actor = db.get(User, ids["user"])
            try:
                AttachmentService(db, root).add(
                    actor=actor,
                    owner_type="expense",
                    owner_id=expense_id,
                    filename="r.pdf",
                    data=PDF,
                )
                outcome[index] = "ok"
            except AppBaseError as exc:
                outcome[index] = type(exc).__name__

    with Session(engine) as locker:
        locker.execute(sa.select(sa.text("1")).select_from(sa.text("expense")).with_for_update())
        threads = [threading.Thread(target=upload, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 15
        while _waiters(engine) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _waiters(engine) == 2, "both uploads should be waiting on the expense row"
        locker.commit()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    assert sorted(outcome) == ["InvalidAttachmentError", "ok"], outcome
    with Session(engine) as db:
        assert db.exec(select(func.count()).select_from(Attachment)).one() == 1
    assert len(files(root)) == 1
