import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.ledger.accounts import AccountService, NewAccount
from payroll.ledger.exceptions import AccountConflictError, InvalidAccountError
from payroll.ledger.models import Account, AccountType
from payroll.ledger.qbo_import import (
    MAX_ROWS,
    decode_upload,
    parse_qbo_csv,
    plan_import,
)

HEADER = "Account number,Account name,Account type,Detail type\n"

CHART = (
    HEADER
    + ",Business Checking,Bank,Checking\n"
    + "1200,Accounts Receivable,Accounts receivable (A/R),Accounts Receivable (A/R)\n"
    + "2100*OE,Payroll Liabilities,Other Current Liabilities,Other Current Liabilities\n"
    + ",Payroll Liabilities:GA Income Tax,Other Current Liabilities,Other Current Liabilities\n"
    + ",Payroll Expenses:Payroll Taxes Expense:Penalties,Expenses,Other Miscellaneous Service Cost\n"
    + ",Payroll Expenses:Payroll Taxes Expense,Expenses,Other Miscellaneous Service Cost\n"
    + ",Payroll Expenses,Expenses,Other Miscellaneous Service Cost\n"
    + "4100,Service Revenue,Income,Service/Fee Income\n"
    + "32000,Retained Earnings,Equity,Retained Earnings\n"
)


def _raises(match: str):
    """``pytest.raises`` for this app's errors, whose text lives in ``.message``."""

    class _Ctx:
        def __enter__(self):
            self._ctx = pytest.raises(InvalidAccountError)
            self._info = self._ctx.__enter__()
            return self

        def __exit__(self, *exc):
            suppressed = self._ctx.__exit__(*exc)
            assert match in self._info.value.message
            return suppressed

    return _Ctx()


def _plan(session, text):
    return plan_import(session, parse_qbo_csv(text))


def _actions(plan):
    return {p.row.full_name: p.action for p in plan.rows}


def test_parse_reads_the_chart_of_accounts_export():
    rows = parse_qbo_csv(CHART)
    assert len(rows) == 9
    assert rows[1].number == "1200"
    assert rows[1].qbo_type == "Accounts receivable (A/R)"
    assert rows[1].detail_type == "Accounts Receivable (A/R)"
    assert rows[0].line == 2


def test_parse_reads_account_list_report_with_title_rows_and_extra_columns():
    text = (
        "Account List\n"
        "DeCorte Industries\n"
        "\n"
        "Account,Type,Detail Type,Description,Balance\n"
        "Business Checking,Bank,Checking,,1500.00\n"
        "Utilities:Electric,Expenses,Utilities,,12.00\n"
        "\n"
    )
    rows = parse_qbo_csv(text)
    assert [(r.full_name, r.qbo_type, r.detail_type, r.number) for r in rows] == [
        ("Business Checking", "Bank", "Checking", ""),
        ("Utilities:Electric", "Expenses", "Utilities", ""),
    ]


def test_parse_accepts_a_utf8_bom_and_shuffled_columns():
    data = ("﻿Detail type,Account type,Account name\nChecking,Bank,Cash\n").encode()
    rows = parse_qbo_csv(decode_upload(data))
    assert rows[0].full_name == "Cash"
    assert rows[0].qbo_type == "Bank"


@pytest.mark.parametrize(
    "text",
    ["", "just,some,columns\n1,2,3\n", HEADER, "Account name\nCash\n"],
)
def test_parse_rejects_files_that_are_not_a_chart(text):
    with pytest.raises(InvalidAccountError):
        parse_qbo_csv(text)


def test_decode_rejects_oversize_and_non_utf8():
    with _raises("too large"):
        decode_upload(b"x" * (512 * 1024 + 1))
    with _raises("UTF-8"):
        decode_upload(b"Account name,Account type\n\xff\xfe,Bank\n")


def test_parse_caps_the_number_of_rows():
    text = HEADER + "".join(f",Acct {n},Bank,Checking\n" for n in range(MAX_ROWS + 1))
    with _raises("Too many"):
        parse_qbo_csv(text)


def test_plan_creates_everything_in_an_empty_chart_parents_first(session):
    plan = _plan(session, CHART)
    assert not plan.has_errors
    assert plan.count("create") == 9
    names = [a.name for a in plan.new_accounts]
    assert names.index("Payroll Expenses") < names.index("Payroll Taxes Expense")
    assert names.index("Payroll Taxes Expense") < names.index("Penalties")
    penalties = next(a for a in plan.new_accounts if a.name == "Penalties")
    assert plan.new_accounts[penalties.parent_index].name == "Payroll Taxes Expense"


def test_plan_maps_every_qbo_type(session):
    plan = _plan(session, CHART)
    by_name = {a.name: a.type for a in plan.new_accounts}
    assert by_name["Business Checking"] == AccountType.ASSET
    assert by_name["Accounts Receivable"] == AccountType.ASSET
    assert by_name["Payroll Liabilities"] == AccountType.LIABILITY
    assert by_name["Penalties"] == AccountType.EXPENSE
    assert by_name["Service Revenue"] == AccountType.INCOME
    assert by_name["Retained Earnings"] == AccountType.EQUITY


@pytest.mark.parametrize(
    ("qbo_type", "expected"),
    [
        ("Bank", AccountType.ASSET),
        ("Other Assets", AccountType.ASSET),
        ("Fixed Assets", AccountType.ASSET),
        ("Accounts payable (A/P)", AccountType.LIABILITY),
        ("Credit Card", AccountType.LIABILITY),
        ("Long Term Liabilities", AccountType.LIABILITY),
        ("Cost of Goods Sold", AccountType.COGS),
        ("Other Income", AccountType.OTHER_INCOME),
        ("Other Expense", AccountType.OTHER_EXPENSE),
        ("  expenses ", AccountType.EXPENSE),
    ],
)
def test_type_mapping(session, qbo_type, expected):
    plan = _plan(session, f"Account name,Account type\nThing,{qbo_type}\n")
    assert plan.new_accounts[0].type == expected


def test_unknown_type_is_an_error_row(session):
    plan = _plan(session, "Account name,Account type\nThing,Spaceship\n")
    assert plan.has_errors
    assert "Unknown account type 'Spaceship'" in plan.rows[0].reason
    assert plan.new_accounts == []


def test_existing_accounts_are_skipped_by_number_or_by_name(session, owner_user):
    service = AccountService(session)
    service.create(actor=owner_user, name="Cash In Bank", type=AccountType.ASSET, number="1200")
    service.create(actor=owner_user, name="service revenue", type=AccountType.INCOME)
    plan = _plan(session, CHART)
    actions = _actions(plan)
    assert actions["Accounts Receivable"] == "exists"  # same number, different name
    assert actions["Service Revenue"] == "exists"  # same name, case-insensitive
    assert actions["Business Checking"] == "create"
    assert plan.count("exists") == 2
    assert {a.name for a in plan.new_accounts}.isdisjoint(
        {"Accounts Receivable", "Service Revenue"}
    )


def test_children_attach_to_an_existing_parent(session, owner_user):
    parent = AccountService(session).create(
        actor=owner_user, name="Payroll Liabilities", type=AccountType.LIABILITY
    )
    plan = _plan(session, CHART)
    child = next(a for a in plan.new_accounts if a.name == "GA Income Tax")
    assert child.parent_id == parent.id
    assert child.parent_index is None


def test_missing_parent_is_an_error(session):
    plan = _plan(session, "Account name,Account type\nOrphans:Child,Expenses\n")
    assert "Parent 'Orphans' is not in the file or the chart" in plan.rows[0].reason


def test_child_type_must_match_the_parent(session):
    text = "Account name,Account type\nParent,Expenses\nParent:Child,Income\n"
    plan = _plan(session, text)
    assert _actions(plan) == {"Parent": "create", "Parent:Child": "error"}
    assert "same type as its parent" in plan.rows[1].reason


def test_children_of_a_failed_parent_fail_too(session):
    text = "Account name,Account type\nParent,Spaceship\nParent:Child,Expenses\n"
    plan = _plan(session, text)
    assert plan.rows[1].action == "error"
    assert "cannot be imported" in plan.rows[1].reason


def test_duplicates_inside_the_file_are_errors(session):
    text = "Account number,Account name,Account type\n10,Cash,Bank\n10,Other,Bank\n,cash,Bank\n"
    plan = _plan(session, text)
    assert [p.action for p in plan.rows] == ["create", "error", "error"]
    assert "also on line 2" in plan.rows[1].reason
    assert "Duplicate of line 2" in plan.rows[2].reason


def test_unusual_numbers_are_kept_with_a_warning(session):
    plan = _plan(session, CHART)
    liabilities = next(p for p in plan.rows if p.row.full_name == "Payroll Liabilities")
    assert liabilities.warning
    assert next(a for a in plan.new_accounts if a.name == "Payroll Liabilities").number == "2100*OE"
    assert not next(p for p in plan.rows if p.row.number == "1200").warning


@pytest.mark.parametrize(
    "line",
    [",,Bank", f",{'x' * 201},Bank", f"{'9' * 21},Cash,Bank", ",A::B,Bank", ",A:,Bank"],
)
def test_malformed_rows_are_errors(session, line):
    plan = _plan(session, "Account number,Account name,Account type\n" + line + "\n")
    assert plan.rows[0].action == "error"


def test_planning_writes_nothing(session):
    _plan(session, CHART)
    assert session.exec(select(Account)).all() == []


def test_create_batch_creates_the_tree_in_one_transaction(session, owner_user):
    plan = _plan(session, CHART)
    created = AccountService(session).create_batch(actor=owner_user, specs=plan.new_accounts)
    assert len(created) == 9
    tree = {n.full_name: n.account for n in AccountService(session).tree()}
    assert "Payroll Expenses:Payroll Taxes Expense:Penalties" in tree
    assert tree["Payroll Liabilities"].number == "2100*OE"
    assert tree["Business Checking"].detail_type == "Checking"
    actions = [a.action for a in session.exec(select(AuditLog)).all()]
    assert actions.count("account.create") == 9
    assert actions.count("account.import") == 1


def test_create_batch_is_all_or_nothing(session, owner_user):
    specs = [
        NewAccount(name="Fine", type=AccountType.ASSET, number="1"),
        NewAccount(name="Clash", type=AccountType.ASSET, number="1"),
    ]
    with pytest.raises(AccountConflictError):
        AccountService(session).create_batch(actor=owner_user, specs=specs)
    assert session.exec(select(Account)).all() == []
    assert session.exec(select(AuditLog).where(AuditLog.action == "account.import")).all() == []


def test_create_batch_rejects_a_forward_parent_reference(session, owner_user):
    specs = [NewAccount(name="Child", type=AccountType.ASSET, parent_index=1)]
    with _raises("before its children"):
        AccountService(session).create_batch(actor=owner_user, specs=specs)


def test_create_batch_is_owner_only(session, accountant_user):
    with pytest.raises(PermissionDeniedError):
        AccountService(session).create_batch(
            actor=accountant_user, specs=[NewAccount(name="X", type=AccountType.ASSET)]
        )


def test_importing_twice_creates_nothing_the_second_time(session, owner_user):
    service = AccountService(session)
    service.create_batch(actor=owner_user, specs=_plan(session, CHART).new_accounts)
    second = _plan(session, CHART)
    assert second.count("create") == 0
    assert second.count("exists") == 9


def _line(plan, name):
    return next(p.row.line for p in plan.rows if p.row.full_name == name)


def test_plan_with_a_selection_creates_only_the_selected_rows(session):
    everything = _plan(session, CHART)
    keep = {_line(everything, "Business Checking"), _line(everything, "Service Revenue")}
    plan = plan_import(session, parse_qbo_csv(CHART), selected=keep)
    assert [a.name for a in plan.new_accounts] == ["Business Checking", "Service Revenue"]
    assert plan.selected_count == 2
    assert plan.count("create") == 9  # the preview still lists what could be created
    assert {p.row.full_name for p in plan.rows if p.action == "create" and not p.selected} == {
        p.row.full_name for p in plan.rows if p.row.line not in keep and p.action == "create"
    }


def test_no_selection_means_every_creatable_row(session):
    plan = plan_import(session, parse_qbo_csv(CHART))
    assert plan.selected_count == 9
    assert all(p.selected for p in plan.rows)


def test_an_empty_selection_creates_nothing(session):
    assert plan_import(session, parse_qbo_csv(CHART), selected=set()).new_accounts == []


def test_a_selected_child_needs_its_parent_selected(session):
    plan = _plan(session, CHART)
    child = _line(plan, "Payroll Expenses:Payroll Taxes Expense")
    with _raises("needs its parent 'Payroll Expenses', which is not selected"):
        plan_import(session, parse_qbo_csv(CHART), selected={child})


def test_a_selected_child_may_use_a_parent_already_in_the_chart(session, owner_user):
    AccountService(session).create(
        actor=owner_user, name="Payroll Liabilities", type=AccountType.LIABILITY
    )
    plan = _plan(session, CHART)
    child = _line(plan, "Payroll Liabilities:GA Income Tax")
    chosen = plan_import(session, parse_qbo_csv(CHART), selected={child})
    assert [a.name for a in chosen.new_accounts] == ["GA Income Tax"]
    assert chosen.new_accounts[0].parent_id is not None


def test_parent_and_children_can_be_selected_without_the_rest(session):
    plan = _plan(session, CHART)
    chain = {
        _line(plan, "Payroll Expenses"),
        _line(plan, "Payroll Expenses:Payroll Taxes Expense"),
        _line(plan, "Payroll Expenses:Payroll Taxes Expense:Penalties"),
    }
    chosen = plan_import(session, parse_qbo_csv(CHART), selected=chain)
    assert [a.name for a in chosen.new_accounts] == [
        "Payroll Expenses",
        "Payroll Taxes Expense",
        "Penalties",
    ]


def test_only_creatable_rows_can_be_selected(session, owner_user):
    AccountService(session).create(
        actor=owner_user, name="Service Revenue", type=AccountType.INCOME
    )
    plan = _plan(session, CHART)
    with _raises("cannot be imported"):
        plan_import(session, parse_qbo_csv(CHART), selected={_line(plan, "Service Revenue")})
    with _raises("Line 999 cannot be imported"):
        plan_import(session, parse_qbo_csv(CHART), selected={999})


def test_unselected_error_rows_do_not_stop_the_rest(session):
    text = CHART + ",Mystery,Spaceship,\n"
    plan = _plan(session, text)
    assert plan.has_errors
    chosen = plan_import(session, parse_qbo_csv(text), selected={_line(plan, "Service Revenue")})
    assert [a.name for a in chosen.new_accounts] == ["Service Revenue"]


def test_row_key_is_the_lowercase_path(session):
    plan = _plan(session, "Account name,Account type\nParent,Expenses\nParent: Child ,Expenses\n")
    assert [p.key for p in plan.rows] == ["parent", "parent:child"]
