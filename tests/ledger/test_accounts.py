from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import select
from sqlmodel import text as sql_text

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.ledger.accounts import AccountService
from payroll.ledger.default_chart import DEFAULT_CHART, seed_default_chart
from payroll.ledger.exceptions import (
    AccountConflictError,
    AccountInUseError,
    AccountNotFoundError,
    InvalidAccountError,
)
from payroll.ledger.models import (
    AccountType,
    JournalEntry,
    JournalLine,
    SourceType,
    SystemRole,
)


def _post_entry(session, user, debit_account, credit_account, amount="100.00"):
    entry = JournalEntry(
        entry_no=1,
        source_type=SourceType.MANUAL,
        entry_date=date(2026, 1, 15),
        posted_by=user.id,
    )
    session.add(entry)
    session.flush()
    for n, (acct, dr, cr) in enumerate(
        [
            (debit_account, Decimal(amount), Decimal(0)),
            (credit_account, Decimal(0), Decimal(amount)),
        ],
        start=1,
    ):
        session.add(
            JournalLine(entry_id=entry.id, line_no=n, account_id=acct.id, debit=dr, credit=cr)
        )
    session.flush()
    session.execute(sql_text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_create_sets_full_name_and_normal_balance(session, owner_user):
    svc = AccountService(session)
    parent = svc.create(actor=owner_user, name="Payroll", type=AccountType.EXPENSE, number="6000")
    child = svc.create(
        actor=owner_user, name="Wages", type=AccountType.EXPENSE, parent_id=parent.id
    )
    liab = svc.create(actor=owner_user, name="Loan", type=AccountType.LIABILITY)
    nodes = {n.full_name: n for n in svc.tree()}
    assert nodes["Payroll:Wages"].depth == 1
    assert nodes["Payroll"].depth == 0
    assert svc.normal_balance(child) == "debit"
    assert svc.normal_balance(liab) == "credit"


def test_child_must_match_parent_type(session, owner_user):
    svc = AccountService(session)
    parent = svc.create(actor=owner_user, name="Payroll", type=AccountType.EXPENSE)
    with pytest.raises(InvalidAccountError):
        svc.create(actor=owner_user, name="X", type=AccountType.ASSET, parent_id=parent.id)
    other = svc.create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    with pytest.raises(InvalidAccountError):
        svc.update(other.id, actor=owner_user, parent_id=parent.id)
    with pytest.raises(AccountNotFoundError):
        svc.create(actor=owner_user, name="X", type=AccountType.EXPENSE, parent_id=99999)


def test_cycle_rejected(session, owner_user):
    svc = AccountService(session)
    a = svc.create(actor=owner_user, name="A", type=AccountType.EXPENSE)
    b = svc.create(actor=owner_user, name="B", type=AccountType.EXPENSE, parent_id=a.id)
    c = svc.create(actor=owner_user, name="C", type=AccountType.EXPENSE, parent_id=b.id)
    with pytest.raises(InvalidAccountError):
        svc.update(a.id, actor=owner_user, parent_id=c.id)
    with pytest.raises(InvalidAccountError):
        svc.update(a.id, actor=owner_user, parent_id=a.id)
    # clearing the parent is allowed
    assert svc.update(c.id, actor=owner_user, parent_id=None).parent_id is None


def test_duplicate_number_and_duplicate_sibling_name_rejected(session, owner_user):
    svc = AccountService(session)
    p = svc.create(actor=owner_user, name="Parent", type=AccountType.EXPENSE, number="6000")
    svc.create(actor=owner_user, name="Wages", type=AccountType.EXPENSE, parent_id=p.id)
    with pytest.raises(AccountConflictError):
        svc.create(actor=owner_user, name="Other", type=AccountType.EXPENSE, number="6000")
    with pytest.raises(AccountConflictError):
        svc.create(actor=owner_user, name="WAGES", type=AccountType.EXPENSE, parent_id=p.id)
    # same name under a different parent is fine
    svc.create(actor=owner_user, name="Wages", type=AccountType.EXPENSE)
    top = svc.create(actor=owner_user, name="Top", type=AccountType.EXPENSE)
    with pytest.raises(AccountConflictError):
        svc.update(top.id, actor=owner_user, number="6000")
    with pytest.raises(AccountConflictError):
        svc.create(actor=owner_user, name="top", type=AccountType.EXPENSE)
    # updating an account to its own number/name is not a conflict
    svc.update(p.id, actor=owner_user, number="6000", name="Parent")


def test_type_change_blocked_once_account_has_lines(session, owner_user):
    svc = AccountService(session)
    a = svc.create(actor=owner_user, name="A", type=AccountType.ASSET)
    b = svc.create(actor=owner_user, name="B", type=AccountType.EQUITY)
    free = svc.create(actor=owner_user, name="Free", type=AccountType.EXPENSE)
    assert svc.update(free.id, actor=owner_user, type=AccountType.COGS).type == AccountType.COGS
    _post_entry(session, owner_user, a, b)
    with pytest.raises(InvalidAccountError):
        svc.update(a.id, actor=owner_user, type=AccountType.LIABILITY)
    # unchanged type is fine; other fields still editable
    svc.update(a.id, actor=owner_user, type=AccountType.ASSET, detail_type="Bank")


def test_type_change_blocked_with_children_or_role(session, owner_user):
    svc = AccountService(session)
    p = svc.create(actor=owner_user, name="P", type=AccountType.EXPENSE)
    svc.create(actor=owner_user, name="C", type=AccountType.EXPENSE, parent_id=p.id)
    with pytest.raises(InvalidAccountError):
        svc.update(p.id, actor=owner_user, type=AccountType.COGS)
    ar = svc.create(actor=owner_user, name="AR", type=AccountType.ASSET)
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, ar.id, actor=owner_user)
    with pytest.raises(InvalidAccountError):
        svc.update(ar.id, actor=owner_user, type=AccountType.LIABILITY)


def test_deactivate_nonzero_balance_rejected(session, owner_user):
    svc = AccountService(session)
    a = svc.create(actor=owner_user, name="A", type=AccountType.ASSET)
    b = svc.create(actor=owner_user, name="B", type=AccountType.EQUITY)
    _post_entry(session, owner_user, a, b)
    with pytest.raises(AccountInUseError):
        svc.set_active(a.id, False, actor=owner_user)
    # a zero-balance account with history can be deactivated, and reactivated
    c = svc.create(actor=owner_user, name="C", type=AccountType.ASSET)
    assert svc.set_active(c.id, False, actor=owner_user).is_active is False
    assert [x.name for x in svc.list()] == ["A", "B"]
    assert len(svc.list(include_inactive=True)) == 3
    assert svc.set_active(c.id, True, actor=owner_user).is_active is True


def test_deactivate_role_account_rejected(session, owner_user):
    svc = AccountService(session)
    ar = svc.create(actor=owner_user, name="AR", type=AccountType.ASSET)
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, ar.id, actor=owner_user)
    with pytest.raises(AccountInUseError):
        svc.set_active(ar.id, False, actor=owner_user)


def test_bind_role_type_mismatch_rejected(session, owner_user):
    svc = AccountService(session)
    liab = svc.create(actor=owner_user, name="L", type=AccountType.LIABILITY)
    with pytest.raises(InvalidAccountError):
        svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, liab.id, actor=owner_user)
    inactive = svc.create(actor=owner_user, name="I", type=AccountType.ASSET)
    svc.set_active(inactive.id, False, actor=owner_user)
    with pytest.raises(InvalidAccountError):
        svc.bind_role(SystemRole.UNDEPOSITED_FUNDS, inactive.id, actor=owner_user)
    with pytest.raises(AccountNotFoundError):
        svc.bind_role(SystemRole.UNDEPOSITED_FUNDS, 99999, actor=owner_user)


def test_bind_role_moves_binding_and_unbind(session, owner_user):
    svc = AccountService(session)
    one = svc.create(actor=owner_user, name="One", type=AccountType.ASSET)
    two = svc.create(actor=owner_user, name="Two", type=AccountType.ASSET)
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, one.id, actor=owner_user)
    assert svc.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id == one.id
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, two.id, actor=owner_user)
    assert svc.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id == two.id
    assert svc.get(one.id).system_role is None
    # rebinding the same account is a no-op
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, two.id, actor=owner_user)
    # an account holds at most one role
    with pytest.raises(AccountConflictError):
        svc.bind_role(SystemRole.UNDEPOSITED_FUNDS, two.id, actor=owner_user)
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, None, actor=owner_user)
    assert svc.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE) is None
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, None, actor=owner_user)  # idempotent


def test_get_missing_raises(session):
    with pytest.raises(AccountNotFoundError):
        AccountService(session).get(99999)


def test_seed_default_chart_is_idempotent(session, owner_user):
    created = seed_default_chart(session, actor=owner_user)
    assert created == len(DEFAULT_CHART)
    assert seed_default_chart(session, actor=owner_user) == 0
    svc = AccountService(session)
    for role in SystemRole:
        assert svc.account_by_role(role) is not None
    assert svc.account_by_role(SystemRole.DUE_TO_OWNER).name == "Due to Owner"
    nodes = {n.full_name: n for n in svc.tree()}
    assert nodes["Payroll Liabilities:GA Income Tax"].depth == 1
    assert "Payroll Expenses:Wages" in nodes
    assert len(nodes) == len(DEFAULT_CHART)


def test_seed_default_chart_skips_inactive_rows(session, owner_user):
    svc = AccountService(session)
    seed_default_chart(session, actor=owner_user)
    wages = next(n.account for n in svc.tree() if n.full_name == "Payroll Expenses:Wages")
    svc.set_active(wages.id, False, actor=owner_user)
    assert seed_default_chart(session, actor=owner_user) == 0  # exists, even inactive


def test_tree_orders_children_under_parent(session, chart):
    svc = AccountService(session)
    names = [n.full_name for n in svc.tree()]
    i = names.index("Payroll Liabilities")
    assert names[i + 1].startswith("Payroll Liabilities:")


def test_mutations_require_owner(session, owner_user, accountant_user, viewer_user):
    svc = AccountService(session)
    acct = svc.create(actor=owner_user, name="A", type=AccountType.ASSET)
    for actor in (accountant_user, viewer_user):
        with pytest.raises(PermissionDeniedError):
            svc.create(actor=actor, name="B", type=AccountType.ASSET)
        with pytest.raises(PermissionDeniedError):
            svc.update(acct.id, actor=actor, name="Z")
        with pytest.raises(PermissionDeniedError):
            svc.set_active(acct.id, False, actor=actor)
        with pytest.raises(PermissionDeniedError):
            svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, acct.id, actor=actor)


def test_every_mutation_is_audited(session, owner_user):
    svc = AccountService(session)
    a = svc.create(actor=owner_user, name="A", type=AccountType.ASSET)
    svc.update(a.id, actor=owner_user, name="A2")
    svc.set_active(a.id, False, actor=owner_user)
    svc.set_active(a.id, True, actor=owner_user)
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, a.id, actor=owner_user)
    actions = [
        r.action
        for r in session.exec(select(AuditLog).order_by(AuditLog.id)).all()
        if r.action.startswith("account.")
    ]
    assert actions == [
        "account.create",
        "account.update",
        "account.deactivate",
        "account.activate",
        "account.bind_role",
    ]


def test_blank_name_rejected(session, owner_user):
    with pytest.raises(InvalidAccountError):
        AccountService(session).create(actor=owner_user, name="  ", type=AccountType.ASSET)


def test_racing_writers_get_conflict_not_integrity_error(session, owner_user, monkeypatch):
    svc = AccountService(session)
    first = svc.create(actor=owner_user, name="A", type=AccountType.ASSET, number="1000")
    other = svc.create(actor=owner_user, name="B", type=AccountType.ASSET)
    # Simulate two requests that both passed the select-then-insert checks.
    monkeypatch.setattr(svc, "_check_unique", lambda *a, **k: None)
    with pytest.raises(AccountConflictError):
        svc.create(actor=owner_user, name="C", type=AccountType.ASSET, number="1000")
    with pytest.raises(AccountConflictError):
        svc.update(other.id, actor=owner_user, number="1000")
    with pytest.raises(AccountConflictError):
        svc.create(actor=owner_user, name="a", type=AccountType.ASSET)
    # role slot race: another account already holds the role at the database level
    svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, first.id, actor=owner_user)
    monkeypatch.setattr(svc, "account_by_role", lambda role: None)
    with pytest.raises(AccountConflictError):
        svc.bind_role(SystemRole.ACCOUNTS_RECEIVABLE, other.id, actor=owner_user)
    monkeypatch.undo()
    # the session is usable, nothing from the failed calls (audit rows included) remains
    assert [a.name for a in svc.list()] == ["A", "B"]
    actions = [r.action for r in session.exec(select(AuditLog)).all()]
    assert actions.count("account.create") == 2
    assert actions.count("account.update") == 0
    assert svc.create(actor=owner_user, name="D", type=AccountType.ASSET).id
