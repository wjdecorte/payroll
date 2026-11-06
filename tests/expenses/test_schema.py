"""
Expenses schema: constraints, indexes, append-only and integrity triggers.

The ``session`` fixture never really commits; ``_commit`` fires the deferred
constraint triggers exactly as COMMIT would (``SET CONSTRAINTS ALL IMMEDIATE``).
"""

from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import text

from payroll.expenses.models import (
    Attachment,
    Expense,
    ExpenseLine,
    Reimbursement,
    Transfer,
    Vendor,
)
from payroll.ledger.models import SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.migrations import alembic_runner

D = Decimal
JAN = date(2026, 1, 15)
PRE_REVISION = "c3d9f1a7e2b4"  # head before the expenses migration
EXPENSE_TABLES = {"vendor", "expense", "expense_line", "transfer", "reimbursement", "attachment"}
SHA = "a" * 64


def _commit(session) -> None:
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


def _entry(session, owner_user, chart) -> int:
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id
    draft = EntryDraft(
        JAN, [LineDraft(cash, debit=D("1")), LineDraft(sales, credit=D("1"))], memo="x"
    )
    return LedgerPostingService(session).post(draft, actor=owner_user).id


def _expense(session, **kw) -> Expense:
    kw.setdefault("date", JAN)
    kw.setdefault("total", D("100.00"))
    row = Expense(**kw)
    session.add(row)
    session.flush()
    return row


def _line(session, expense, account, amount="100.00", line_no=1) -> ExpenseLine:
    row = ExpenseLine(
        expense_id=expense.id, line_no=line_no, account_id=account.id, amount=D(amount)
    )
    session.add(row)
    session.flush()
    return row


def _card_expense(session, vendor, card, expense_account, total="100.00", **kw) -> Expense:
    exp = _expense(
        session,
        vendor_id=vendor.id,
        source="account",
        paid_from_account_id=card.id,
        total=D(total),
        **kw,
    )
    _line(session, exp, expense_account, total)
    return exp


def _owner_expense(session, owner_user, chart, expense_account, total="100.00") -> Expense:
    exp = _expense(
        session,
        payee_text="Cafe",
        source="owner",
        total=D(total),
        entry_id=_entry(session, owner_user, chart),
    )
    _line(session, exp, expense_account, total)
    return exp


def _reimbursement(session, owner_user, chart, bank, total="100.00", **kw) -> Reimbursement:
    kw.setdefault("entry_id", _entry(session, owner_user, chart))
    row = Reimbursement(date=JAN, paid_from_account_id=bank.id, total=D(total), **kw)
    session.add(row)
    session.flush()
    return row


def test_migration_upgrade_and_downgrade_roundtrip(scratch_engine):
    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert EXPENSE_TABLES <= set(sa.inspect(conn).get_table_names())
        version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert version == alembic_runner._HEAD_REVISION

    alembic_runner.downgrade(PRE_REVISION)
    with scratch_engine.connect() as conn:
        assert not EXPENSE_TABLES & set(sa.inspect(conn).get_table_names())
        names = {r[0] for r in conn.exec_driver_sql("SELECT proname FROM pg_proc")}
        assert not {n for n in names if n.startswith("expenses_")}
        triggers = {r[0] for r in conn.exec_driver_sql("SELECT tgname FROM pg_trigger")}
        assert not {t for t in triggers if t.startswith(("expense", "reimbursement", "attachment"))}

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        assert EXPENSE_TABLES <= set(sa.inspect(conn).get_table_names())


def test_models_match_migration(scratch_engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlmodel import SQLModel

    alembic_runner.upgrade()
    with scratch_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, SQLModel.metadata) == []


def test_transfer_source_type_exists():
    assert SourceType.TRANSFER == "transfer"


def test_duplicate_vendor_name_case_insensitive_rejected(session, vendor):
    session.add(Vendor(name="OFFICE depot"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_expense_needs_exactly_one_of_vendor_or_payee(session, vendor, card):
    with pytest.raises(IntegrityError):
        _expense(session, source="account", paid_from_account_id=card.id)  # neither
    session.rollback()


def test_expense_both_vendor_and_payee_rejected(session, vendor, card):
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            payee_text="x",
            source="account",
            paid_from_account_id=card.id,
        )


def test_owner_source_iff_no_paid_from_account(session, vendor, card):
    with pytest.raises(IntegrityError):
        _expense(session, vendor_id=vendor.id, source="owner", paid_from_account_id=card.id)


def test_account_source_requires_paid_from_account(session, vendor):
    with pytest.raises(IntegrityError):
        _expense(session, vendor_id=vendor.id, source="account")


def test_expense_source_value_limited(session, vendor, card):
    with pytest.raises(IntegrityError):
        _expense(session, vendor_id=vendor.id, source="cash", paid_from_account_id=card.id)


@pytest.mark.parametrize("total", ["0.00", "-1.00"])
def test_expense_total_positive(session, vendor, card, total):
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            source="account",
            paid_from_account_id=card.id,
            total=D(total),
        )


def test_expense_void_date_not_before_date(session, owner_user, chart, vendor, card):
    e1, e2 = _entry(session, owner_user, chart), _entry(session, owner_user, chart)
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            source="account",
            paid_from_account_id=card.id,
            status="void",
            entry_id=e1,
            void_entry_id=e2,
            void_date=date(2026, 1, 14),
        )


def test_expense_void_requires_all_void_fields(session, owner_user, chart, vendor, card):
    e1 = _entry(session, owner_user, chart)
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            source="account",
            paid_from_account_id=card.id,
            status="void",
            entry_id=e1,
        )


def test_expense_posted_must_not_have_void_fields(session, owner_user, chart, vendor, card):
    e1 = _entry(session, owner_user, chart)
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            source="account",
            paid_from_account_id=card.id,
            entry_id=e1,
            void_date=JAN,
        )


def test_expense_status_value_limited(session, vendor, card):
    with pytest.raises(IntegrityError):
        _expense(
            session,
            vendor_id=vendor.id,
            source="account",
            paid_from_account_id=card.id,
            status="draft",
        )


def test_void_expense_with_all_fields_ok(session, owner_user, chart, vendor, card):
    e1, e2 = _entry(session, owner_user, chart), _entry(session, owner_user, chart)
    exp = _expense(
        session,
        vendor_id=vendor.id,
        source="account",
        paid_from_account_id=card.id,
        status="void",
        entry_id=e1,
        void_entry_id=e2,
        void_date=JAN,
    )
    _line(session, exp, card)
    _commit(session)


@pytest.mark.parametrize("amount", ["0.00", "-5.00"])
def test_expense_line_amount_positive(session, vendor, card, expense_account, amount):
    exp = _card_expense(session, vendor, card, expense_account)
    with pytest.raises(IntegrityError):
        _line(session, exp, expense_account, amount, line_no=2)


def test_expense_line_no_unique_per_expense(session, vendor, card, expense_account):
    exp = _card_expense(session, vendor, card, expense_account)
    with pytest.raises(IntegrityError):
        _line(session, exp, expense_account, "1.00", line_no=1)


def test_lines_matching_total_commit_ok(session, vendor, card, expense_account):
    exp = _expense(
        session,
        vendor_id=vendor.id,
        source="account",
        paid_from_account_id=card.id,
        total=D("100.00"),
    )
    _line(session, exp, expense_account, "60.00", line_no=1)
    _line(session, exp, expense_account, "40.00", line_no=2)
    _commit(session)


def test_line_sum_differing_from_total_rejected_at_commit(session, vendor, card, expense_account):
    exp = _expense(
        session,
        vendor_id=vendor.id,
        source="account",
        paid_from_account_id=card.id,
        total=D("100.00"),
    )
    _line(session, exp, expense_account, "60.00")  # accepted until commit
    with pytest.raises(DBAPIError, match="lines"):
        _commit(session)


def test_expense_without_lines_rejected_at_commit(session, vendor, card):
    _expense(session, vendor_id=vendor.id, source="account", paid_from_account_id=card.id)
    with pytest.raises(DBAPIError, match="lines"):
        _commit(session)


def test_total_change_away_from_lines_rejected_at_commit(session, vendor, card, expense_account):
    exp = _card_expense(session, vendor, card, expense_account)
    _commit(session)
    session.execute(text("UPDATE expense SET total = 90 WHERE id = :i"), {"i": exp.id})
    with pytest.raises(DBAPIError, match="lines"):
        _commit(session)


def test_expense_lines_are_append_only(session, vendor, card, expense_account):
    exp = _card_expense(session, vendor, card, expense_account)
    for sql in (
        "UPDATE expense_line SET amount = 5 WHERE expense_id = :i",
        "DELETE FROM expense_line WHERE expense_id = :i",
    ):
        with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
            session.execute(text(sql), {"i": exp.id})


def test_reimbursement_link_requires_owner_source(
    session, owner_user, chart, vendor, card, expense_account
):
    bank = chart["Business Checking"]
    exp = _card_expense(session, vendor, card, expense_account)
    reim = _reimbursement(session, owner_user, chart, bank)
    with pytest.raises(DBAPIError, match="owner"):
        session.execute(
            text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"),
            {"r": reim.id, "e": exp.id},
        )


def test_reimbursement_link_requires_posted_expense(session, owner_user, chart, expense_account):
    bank = chart["Business Checking"]
    e2 = _entry(session, owner_user, chart)
    exp = _expense(
        session,
        payee_text="Cafe",
        source="owner",
        entry_id=_entry(session, owner_user, chart),
        status="void",
        void_entry_id=e2,
        void_date=JAN,
    )
    _line(session, exp, expense_account)
    reim = _reimbursement(session, owner_user, chart, bank)
    with pytest.raises(DBAPIError, match="posted"):
        session.execute(
            text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"),
            {"r": reim.id, "e": exp.id},
        )


def test_reimbursement_link_sets_and_commits(session, owner_user, chart, expense_account):
    bank = chart["Business Checking"]
    exp = _owner_expense(session, owner_user, chart, expense_account)
    reim = _reimbursement(session, owner_user, chart, bank)
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": reim.id, "e": exp.id}
    )
    _commit(session)


def test_reimbursement_link_is_set_once(session, owner_user, chart, expense_account):
    bank = chart["Business Checking"]
    exp = _owner_expense(session, owner_user, chart, expense_account)
    r1 = _reimbursement(session, owner_user, chart, bank)
    r2 = _reimbursement(session, owner_user, chart, bank)
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": r1.id, "e": exp.id}
    )
    with pytest.raises(DBAPIError, match="set once"):
        session.execute(
            text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"),
            {"r": r2.id, "e": exp.id},
        )


def test_reimbursement_link_clear_refused_while_reimbursement_posted(
    session, owner_user, chart, expense_account
):
    bank = chart["Business Checking"]
    exp = _owner_expense(session, owner_user, chart, expense_account)
    reim = _reimbursement(session, owner_user, chart, bank)
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": reim.id, "e": exp.id}
    )
    with pytest.raises(DBAPIError, match="void"):
        session.execute(
            text("UPDATE expense SET reimbursement_id = NULL WHERE id = :e"), {"e": exp.id}
        )


def test_reimbursement_link_clear_allowed_once_reimbursement_void(
    session, owner_user, chart, expense_account
):
    bank = chart["Business Checking"]
    exp = _owner_expense(session, owner_user, chart, expense_account)
    reim = _reimbursement(session, owner_user, chart, bank)
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": reim.id, "e": exp.id}
    )
    _commit(session)
    session.execute(
        text(
            "UPDATE reimbursement SET status = 'void', void_entry_id = :v, void_date = :d "
            "WHERE id = :r"
        ),
        {"v": _entry(session, owner_user, chart), "d": JAN, "r": reim.id},
    )
    session.execute(text("UPDATE expense SET reimbursement_id = NULL WHERE id = :e"), {"e": exp.id})
    _commit(session)  # a void reimbursement need not match its (now unlinked) expenses
    # and the expense can be reimbursed again by a new reimbursement
    r2 = _reimbursement(session, owner_user, chart, bank)
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": r2.id, "e": exp.id}
    )
    _commit(session)


def test_reimbursement_total_must_equal_expense_sum_at_commit(
    session, owner_user, chart, expense_account
):
    bank = chart["Business Checking"]
    exp = _owner_expense(session, owner_user, chart, expense_account, "100.00")
    reim = _reimbursement(session, owner_user, chart, bank, total="150.00")
    session.execute(
        text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"), {"r": reim.id, "e": exp.id}
    )
    with pytest.raises(DBAPIError, match="reimbursement"):
        _commit(session)


def test_reimbursement_without_expenses_rejected_at_commit(session, owner_user, chart):
    _reimbursement(session, owner_user, chart, chart["Business Checking"])
    with pytest.raises(DBAPIError, match="reimbursement"):
        _commit(session)


def test_reimbursement_sums_multiple_expenses(session, owner_user, chart, expense_account):
    bank = chart["Business Checking"]
    e1 = _owner_expense(session, owner_user, chart, expense_account, "100.00")
    e2 = _owner_expense(session, owner_user, chart, expense_account, "50.25")
    reim = _reimbursement(session, owner_user, chart, bank, total="150.25")
    for exp in (e1, e2):
        session.execute(
            text("UPDATE expense SET reimbursement_id = :r WHERE id = :e"),
            {"r": reim.id, "e": exp.id},
        )
    _commit(session)


def test_reimbursement_status_and_total(session, owner_user, chart):
    bank = chart["Business Checking"]
    with pytest.raises(IntegrityError):
        _reimbursement(session, owner_user, chart, bank, total="0.00")


def test_transfer_rejects_equal_accounts(session, chart):
    bank = chart["Business Checking"]
    with pytest.raises(IntegrityError):
        session.add(
            Transfer(date=JAN, from_account_id=bank.id, to_account_id=bank.id, amount=D("5"))
        )
        session.flush()


def test_transfer_amount_positive(session, chart, card):
    bank = chart["Business Checking"]
    with pytest.raises(IntegrityError):
        session.add(
            Transfer(date=JAN, from_account_id=bank.id, to_account_id=card.id, amount=D("0"))
        )
        session.flush()


def test_transfer_ok_and_void_fields_consistent(session, owner_user, chart, card):
    bank = chart["Business Checking"]
    session.add(
        Transfer(
            date=JAN,
            from_account_id=bank.id,
            to_account_id=card.id,
            amount=D("5"),
            entry_id=_entry(session, owner_user, chart),
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        session.add(
            Transfer(
                date=JAN,
                from_account_id=bank.id,
                to_account_id=card.id,
                amount=D("5"),
                status="void",
                entry_id=_entry(session, owner_user, chart),
            )
        )
        session.flush()


def _attachment(session, owner_user, **kw) -> Attachment:
    base = dict(
        owner_type="expense",
        owner_id=1,
        filename="r.pdf",
        content_type="application/pdf",
        sha256=SHA,
        stored_name="abc",
        size=10,
        uploaded_by=owner_user.id,
    )
    row = Attachment(**(base | kw))
    session.add(row)
    session.flush()
    return row


def test_attachment_insert_ok(session, owner_user):
    assert _attachment(session, owner_user).id is not None


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"size": 0}, id="size-zero"),
        pytest.param({"size": 10_485_761}, id="size-over-10mb"),
        pytest.param({"owner_type": "invoice"}, id="owner-type"),
        pytest.param({"sha256": "a" * 63}, id="sha-short"),
        pytest.param({"sha256": "A" * 64}, id="sha-uppercase"),
        pytest.param({"sha256": "g" * 64}, id="sha-nonhex"),
    ],
)
def test_attachment_checks(session, owner_user, kw):
    with pytest.raises(IntegrityError):
        _attachment(session, owner_user, **kw)


def test_attachment_size_upper_bound_allowed(session, owner_user):
    _attachment(session, owner_user, size=10_485_760)


def test_attachment_is_append_only(session, owner_user):
    row = _attachment(session, owner_user)
    for sql in (
        "UPDATE attachment SET filename = 'x' WHERE id = :i",
        "DELETE FROM attachment WHERE id = :i",
    ):
        with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
            session.execute(text(sql), {"i": row.id})


def test_journal_entry_accepts_transfer_source_type(session, chart, owner_user):
    cash, sales = chart["Business Checking"].id, chart["Services Revenue"].id
    draft = EntryDraft(
        JAN,
        [LineDraft(cash, debit=D("1")), LineDraft(sales, credit=D("1"))],
        memo="t",
        source_type=SourceType.TRANSFER,
    )
    assert LedgerPostingService(session).post(draft, actor=owner_user).id
