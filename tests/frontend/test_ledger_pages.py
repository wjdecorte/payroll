"""Chart of accounts, account register and ledger settings pages."""

from datetime import date

import pytest
from sqlmodel import select

from payroll.auth.security import hash_password
from payroll.ledger.accounts import AccountService
from payroll.ledger.models import Account, AccountType, SourceType, SystemRole
from payroll.ledger.period_lock import PeriodLockService
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft
from payroll.reports.periods import company_today

PASSWORD = "correct horse battery staple"
XSS = "<script>alert(1)</script>"


def _create(client, **fields):
    data = {"name": "Cash", "type": "asset", "number": "", "parent_id": "", "detail_type": ""}
    data.update(fields)
    return client.post("/accounts/new", data=data, follow_redirects=False)


def _account(session, name: str) -> Account:
    return session.exec(select(Account).where(Account.name == name)).one()


def test_owner_creates_parent_and_child_shown_nested(owner_client, session):
    assert _create(owner_client, name="Bank", number="1000").status_code == 303
    parent = _account(session, "Bank")
    assert _create(owner_client, name="Checking", parent_id=str(parent.id)).status_code == 303
    page = owner_client.get("/accounts")
    assert page.status_code == 200
    assert page.text.index("Bank") < page.text.index("Checking")
    assert "acct-indent-1" in page.text
    assert "1000" in page.text
    assert "$0.00" in page.text


def test_duplicate_number_rerenders_with_error_and_typed_values(owner_client):
    _create(owner_client, name="Bank", number="1000")
    response = _create(owner_client, name="Savings Typed", number="1000", detail_type="Savings")
    assert response.status_code == 422
    assert "already in use" in response.text
    assert 'value="Savings Typed"' in response.text
    assert 'value="1000"' in response.text
    assert 'value="Savings"' in response.text
    assert '<option value="asset" selected>' in response.text


def test_missing_type_is_a_form_error(owner_client):
    response = _create(owner_client, type="")
    assert response.status_code == 422
    assert "Choose an account type" in response.text


def test_edit_and_deactivate_and_inactive_toggle(owner_client, session):
    _create(owner_client, name="Old Name", number="2000")
    account = _account(session, "Old Name")
    form = owner_client.get(f"/accounts/{account.id}/edit")
    assert form.status_code == 200 and 'value="Old Name"' in form.text
    edit = owner_client.post(
        f"/accounts/{account.id}/edit",
        data={"name": "New Name", "type": "asset", "number": "2000", "parent_id": ""},
        follow_redirects=False,
    )
    assert edit.status_code == 303
    clash = owner_client.post(
        f"/accounts/{account.id}/edit",
        data={"name": "", "type": "asset", "number": "2000", "parent_id": ""},
    )
    assert clash.status_code == 422 and "needs a name" in clash.text
    off = owner_client.post(
        f"/accounts/{account.id}/active", data={"active": "0"}, follow_redirects=False
    )
    assert off.status_code == 303
    assert "New Name" not in owner_client.get("/accounts").text
    shown = owner_client.get("/accounts?inactive=1").text
    assert "New Name" in shown and "Reactivate" in shown
    owner_client.post(f"/accounts/{account.id}/active", data={"active": "1"})
    assert "New Name" in owner_client.get("/accounts").text


def test_missing_account_pages_show_error(owner_client):
    assert owner_client.get("/accounts/9999/edit").status_code == 404
    assert owner_client.get("/accounts/9999/register").status_code == 404


def test_viewer_sees_pages_without_controls_and_posts_are_403(viewer_client, owner_user, session):
    account = AccountService(session).create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    for path in ("/accounts", f"/accounts/{account.id}/register", "/settings/ledger"):
        response = viewer_client.get(path)
        assert response.status_code == 200, path
    listing = viewer_client.get("/accounts").text
    for hidden in ("/accounts/new", "/edit", "Deactivate"):
        assert hidden not in listing
    settings = viewer_client.get("/settings/ledger").text
    assert '<form method="post" action="/settings/ledger' not in settings
    assert 'type="password"' not in settings
    assert viewer_client.get("/accounts/new").status_code == 403
    assert _create(viewer_client).status_code == 403
    for path in (
        f"/accounts/{account.id}/edit",
        f"/accounts/{account.id}/active",
        "/settings/ledger/roles",
        "/settings/ledger/lock",
    ):
        response = viewer_client.post(path, data={})
        assert response.status_code == 403, path
        assert response.json()["errors"][0]["type"] == "PermissionDeniedError"


def test_account_name_is_escaped_everywhere(owner_client, session):
    _create(owner_client, name=XSS)
    account = _account(session, XSS)
    pages = [
        owner_client.get("/accounts"),
        owner_client.get(f"/accounts/{account.id}/edit"),
        owner_client.get(f"/accounts/{account.id}/register"),
        _create(owner_client, name=XSS),  # duplicate re-render keeps the typed value
    ]
    for page in pages:
        assert XSS not in page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_register_shows_opening_running_balance_and_entry_links(owner_client, owner_user, session):
    service = AccountService(session)
    cash = service.create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    income = service.create(actor=owner_user, name="Sales", type=AccountType.INCOME)
    posting = LedgerPostingService(session)

    def post(day: date, amount: str, memo: str):
        from decimal import Decimal

        entry = posting.post(
            EntryDraft(
                entry_date=day,
                memo=memo,
                source_type=SourceType.MANUAL,
                lines=[
                    LineDraft(cash.id, debit=Decimal(amount)),
                    LineDraft(income.id, credit=Decimal(amount)),
                ],
            ),
            actor=owner_user,
        )
        session.commit()
        return entry

    before = post(date(2025, 12, 15), "100.00", "December sale")
    first = post(date(2026, 1, 10), "250.00", f"January {XSS}")
    page = owner_client.get(f"/accounts/{cash.id}/register?start=2026-01-01&end=2026-12-31")
    assert page.status_code == 200
    text = page.text
    assert "Opening balance" in text and "$100.00" in text
    assert "$350.00" in text  # running and closing balance
    assert f'href="/journal/{first.id}"' in text
    assert f'href="/journal/{before.id}"' not in text
    assert XSS not in text
    bad = owner_client.get(f"/accounts/{cash.id}/register?start=nonsense")
    assert bad.status_code == 422
    backwards = owner_client.get(f"/accounts/{cash.id}/register?start=2026-02-01&end=2026-01-01")
    assert backwards.status_code == 422


def test_list_balance_is_as_of_today(owner_client, owner_user, session):
    service = AccountService(session)
    cash = service.create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    income = service.create(actor=owner_user, name="Sales", type=AccountType.INCOME)
    from decimal import Decimal

    LedgerPostingService(session).post(
        EntryDraft(
            entry_date=company_today(),
            memo=None,
            source_type=SourceType.MANUAL,
            lines=[
                LineDraft(cash.id, debit=Decimal("75.00")),
                LineDraft(income.id, credit=Decimal("75.00")),
            ],
        ),
        actor=owner_user,
    )
    session.commit()
    text = owner_client.get("/accounts").text
    assert text.count("$75.00") == 2  # normal-side balance for both accounts


def test_settings_lock_needs_the_right_password(owner_client, owner_user, session):
    owner_user.password_hash = hash_password(PASSWORD)
    session.commit()
    wrong = owner_client.post(
        "/settings/ledger/lock", data={"lock_date": "2026-01-31", "password": "nope"}
    )
    assert wrong.status_code == 403
    assert 'value="2026-01-31"' in wrong.text
    assert "nope" not in wrong.text
    assert PeriodLockService(session).get() is None
    ok = owner_client.post(
        "/settings/ledger/lock", data={"lock_date": "2026-01-31", "password": PASSWORD}
    )
    assert ok.status_code == 200 and "Lock date saved" in ok.text
    assert PASSWORD not in ok.text
    assert PeriodLockService(session).get() == date(2026, 1, 31)
    bad_date = owner_client.post(
        "/settings/ledger/lock", data={"lock_date": "31/01/2026", "password": PASSWORD}
    )
    assert bad_date.status_code == 422
    cleared = owner_client.post(
        "/settings/ledger/lock", data={"lock_date": "", "password": PASSWORD}
    )
    assert cleared.status_code == 200
    assert PeriodLockService(session).get() is None


def test_settings_roles_bind_unbind_and_errors(owner_client, owner_user, session):
    service = AccountService(session)
    equity = service.create(actor=owner_user, name="Retained", type=AccountType.EQUITY)
    asset = service.create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    page = owner_client.get("/settings/ledger")
    assert page.status_code == 200 and "Retained" in page.text
    ok = owner_client.post(
        "/settings/ledger/roles", data={"role_retained_earnings": str(equity.id)}
    )
    assert ok.status_code == 200 and "System roles saved" in ok.text
    assert service.account_by_role(SystemRole.RETAINED_EARNINGS).id == equity.id
    wrong_type = owner_client.post(
        "/settings/ledger/roles", data={"role_retained_earnings": str(asset.id)}
    )
    assert wrong_type.status_code == 422 and "equity account" in wrong_type.text
    garbage = owner_client.post("/settings/ledger/roles", data={"role_due_to_owner": "abc"})
    assert garbage.status_code == 422
    unbind = owner_client.post("/settings/ledger/roles", data={})
    assert unbind.status_code == 200
    assert service.account_by_role(SystemRole.RETAINED_EARNINGS) is None


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/", "Payroll"),
        ("/accounts", "Accounts"),
        ("/accounts/new", "Accounts"),
        ("/settings/ledger", "Settings"),
        ("/admin", "Admin"),
    ],
)
def test_navigation_links_and_active_marker(owner_client, path, label):
    text = owner_client.get(path).text
    for href in ("/", "/accounts", "/journal", "/reports", "/settings/ledger"):
        assert f'href="{href}"' in text
    active = [line for line in text.splitlines() if 'aria-current="page"' in line]
    assert len(active) == 1 and label in active[0]


def test_pages_are_no_store_and_csp_safe(owner_client):
    from tests.frontend.test_csp_compat import _assert_csp_safe

    for path in ("/accounts", "/accounts/new", "/settings/ledger"):
        response = owner_client.get(path)
        assert response.headers["cache-control"] == "no-store"
        _assert_csp_safe(response.text)


def test_settings_roles_partial_failure_reports_what_was_saved(owner_client, owner_user, session):
    service = AccountService(session)
    cash = service.create(actor=owner_user, name="Cash", type=AccountType.ASSET)
    response = owner_client.post(
        "/settings/ledger/roles",
        data={"role_accounts_receivable": str(cash.id), "role_due_to_owner": "abc"},
    )
    assert response.status_code == 422
    assert "Due to owner" in response.text and "Choose a valid account" in response.text
    assert "Saved before the error: Accounts receivable" in response.text
    # Honest: the first binding really is stored.
    assert service.account_by_role(SystemRole.ACCOUNTS_RECEIVABLE).id == cash.id


def test_blocking_routes_are_threadpool_functions_and_engine_has_lock_timeout():
    import inspect

    from sqlalchemy import text

    from payroll.app import app
    from payroll.common.dependencies import engine

    heavy = [
        route
        for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and (
            route.path.endswith((".pdf", ".csv"))
            or (
                route.path.endswith(("/period-lock", "/settings/ledger/lock"))
                and "GET" not in route.methods
            )
        )
    ]
    paths = {r.path for r in heavy}
    assert "/settings/ledger/lock" in paths and "/reports/profit-and-loss.pdf" in paths
    assert any(p.endswith("/period-lock") for p in paths)
    assert (
        len(heavy) == 28
    )  # 24 export routes (6 reports, csv and pdf, page and API twins) + 2 locks + invoice PDF
    # + the expenses CSV
    for route in heavy:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path
    with engine.connect() as conn:
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "10s"
