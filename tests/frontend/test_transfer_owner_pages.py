"""Transfer and owner pages: validation, owner numbers, reimburse flow, roles, escaping."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import func, select

from payroll.expenses.expenses import ExpenseLineInput, ExpenseService
from payroll.expenses.models import Reimbursement, Transfer
from payroll.expenses.owner import owner_summary
from payroll.expenses.reimbursements import ReimbursementService
from payroll.expenses.transfers import TransferService
from payroll.ledger.services import EntryDraft, JournalService, LineDraft
from tests.expenses.conftest import card, expense_account, owner_role  # noqa: F401
from tests.frontend.test_csp_compat import _assert_csp_safe
from tests.ledger.conftest import chart  # noqa: F401

D = Decimal
IMG_XSS = "<img src=x onerror=alert(1)>"
ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def owner_expense(session, owner_user, account, amount, **over):
    kwargs = {
        "actor": owner_user,
        "expense_date": date(2026, 3, 1),
        "lines": [ExpenseLineInput(account.id, D(amount))],
        "source": "owner",
        "paid_from_account_id": None,
        "payee_text": "Shop",
    }
    kwargs.update(over)
    row = ExpenseService(session).create(**kwargs)
    session.commit()
    return row


def transfer_form(chart, card, **over):
    data = {
        "transfer_date": "2026-03-05",
        "from_account_id": str(chart["Business Checking"].id),
        "to_account_id": str(card.id),
        "amount": "40.00",
        "memo": "Pay the card",
    }
    data.update(over)
    return data


# ------------------------------------------------------------------------- transfers


def test_transfer_happy_path(owner_client, session, chart, card):
    session.commit()
    form = owner_client.get("/transfers/new")
    assert form.status_code == 200
    _assert_csp_safe(form.text)
    response = owner_client.post(
        "/transfers/new", data=transfer_form(chart, card), follow_redirects=False
    )
    assert response.status_code == 303
    row = session.exec(select(Transfer)).one()
    assert response.headers["location"] == f"/transfers/{row.id}"
    assert (row.amount, row.date, row.memo) == (D("40.00"), date(2026, 3, 5), "Pay the card")

    listing = owner_client.get("/transfers")
    assert listing.status_code == 200 and "Pay the card" in listing.text
    detail = owner_client.get(f"/transfers/{row.id}")
    assert detail.status_code == 200
    assert f"/journal/{row.entry_id}" in detail.text
    assert f"/transfers/{row.id}/void" in detail.text
    _assert_csp_safe(detail.text)

    voided = owner_client.post(
        f"/transfers/{row.id}/void", data={"void_date": "2026-03-06"}, follow_redirects=False
    )
    assert voided.status_code == 303
    session.refresh(row)
    assert row.status == "void" and row.void_date == date(2026, 3, 6)
    assert f"/transfers/{row.id}/void" not in owner_client.get(f"/transfers/{row.id}").text
    again = owner_client.post(f"/transfers/{row.id}/void", data={"void_date": "2026-03-07"})
    assert again.status_code in (409, 422)


def bad_transfer_cases(chart, card):
    checking = str(chart["Business Checking"].id)
    return [
        {"to_account_id": checking},  # equal accounts
        {"to_account_id": str(chart["Due to Owner"].id)},  # bound-role account
        {"amount": "0.00"},
        {"amount": "10.123"},
        {"amount": "1E+30"},
        {"amount": "-5.00"},
        {"amount": ""},
        {"amount": "abc"},
        {"transfer_date": "2026-02-30"},
        {"transfer_date": "03/05/2026"},
        {"transfer_date": "2026-03-05T10:00:00"},
        {"transfer_date": ""},
        {"from_account_id": "abc"},
        {"from_account_id": "0"},
        {"from_account_id": "99999999999"},
        {"from_account_id": ""},
        {"to_account_id": "-1"},
        {"to_account_id": "2147483648"},
        {"memo": "x" * 501},
    ]


def test_transfer_validation_errors_are_422_with_values_kept(owner_client, session, chart, card):
    session.commit()
    for over in bad_transfer_cases(chart, card):
        data = transfer_form(chart, card, **over)
        response = owner_client.post("/transfers/new", data=data, follow_redirects=False)
        assert response.status_code == 422, (over, response.status_code)
        assert 'role="alert"' in response.text, over
        assert IMG_XSS not in response.text
    assert count(session, Transfer) == 0
    kept = owner_client.post(
        "/transfers/new", data=transfer_form(chart, card, amount="0.00", memo="Keep me")
    )
    assert 'value="Keep me"' in kept.text and 'value="0.00"' in kept.text
    assert 'value="2026-03-05"' in kept.text


@pytest.mark.parametrize("path", ["/transfers/abc", "/transfers/0", "/transfers/99999999999"])
def test_transfer_garbage_ids_are_422_not_500(owner_client, path):
    assert owner_client.get(path).status_code == 422
    assert owner_client.post(path + "/void", data={"void_date": "2026-03-05"}).status_code == 422


def test_transfer_missing_is_404(owner_client):
    assert owner_client.get("/transfers/12345").status_code == 404
    response = owner_client.post("/transfers/12345/void", data={"void_date": "2026-03-05"})
    assert response.status_code == 404


def test_transfer_void_bad_date_is_422_and_page_kept(
    owner_client, session, owner_user, chart, card
):
    row = TransferService(session).create(
        actor=owner_user,
        transfer_date=date(2026, 3, 5),
        from_account_id=chart["Business Checking"].id,
        to_account_id=card.id,
        amount=D("5.00"),
    )
    session.commit()
    for bad in ("", "nope", "2026-03-04", "2026-13-01"):
        response = owner_client.post(f"/transfers/{row.id}/void", data={"void_date": bad})
        assert response.status_code == 422, bad
        assert f"/transfers/{row.id}/void" in response.text
    session.refresh(row)
    assert row.status == "posted"


def test_transfer_list_pages_by_50(owner_client, session, owner_user, chart, card):
    service = TransferService(session)
    for i in range(51):
        service.create(
            actor=owner_user,
            transfer_date=date(2026, 3, 1),
            from_account_id=chart["Business Checking"].id,
            to_account_id=card.id,
            amount=D("1.00"),
            memo=f"T{i:02d}",
        )
    session.commit()
    first = owner_client.get("/transfers")
    assert first.text.count(">View</a>") == 50
    assert "Next" in first.text
    second = owner_client.get("/transfers?page=2")
    assert second.status_code == 200 and "Previous" in second.text
    for bad in ("0", "-1", "abc", "9999999999"):
        assert owner_client.get(f"/transfers?page={bad}").status_code == 422


def test_transfer_memo_is_escaped(owner_client, session, chart, card):
    session.commit()
    owner_client.post("/transfers/new", data=transfer_form(chart, card, memo=IMG_XSS))
    row = session.exec(select(Transfer)).one()
    for path in ("/transfers", f"/transfers/{row.id}"):
        page = owner_client.get(path)
        assert ESCAPED in page.text, path
        assert IMG_XSS not in page.text, path


# ---------------------------------------------------------------------------- owner


def seed_three(session, owner_user, expense_account):
    a = owner_expense(session, owner_user, expense_account, "30.00", payee_text="Alpha")
    b = owner_expense(session, owner_user, expense_account, "70.00", payee_text="Beta")
    c = owner_expense(session, owner_user, expense_account, "5.50", payee_text="Gamma")
    return a, b, c


def test_owner_page_numbers(owner_client, session, owner_user, chart, expense_account, owner_role):
    owner_expense(session, owner_user, expense_account, "30.00", payee_text="Alpha")
    owner_expense(session, owner_user, expense_account, "70.00", payee_text="Beta")
    page = owner_client.get("/owner")
    assert page.status_code == 200
    _assert_csp_safe(page.text)
    assert 'id="owed-to-me"' in page.text
    owed = page.text.split('id="owed-to-me"')[1].split("</")[0]
    assert "$100.00" in owed
    assert "$100.00" in page.text.split('id="open-total"')[1].split("</")[0]
    assert "Alpha" in page.text and "Beta" in page.text
    assert 'id="mismatch-note"' not in page.text
    assert "/static/js/owner.js" in page.text


def test_reimburse_two_of_three(owner_client, session, owner_user, chart, expense_account):
    a, b, c = seed_three(session, owner_user, expense_account)
    bank = chart["Business Checking"]
    response = owner_client.post(
        "/owner/reimburse",
        data={
            "reimbursement_date": "2026-03-10",
            "bank_account_id": str(bank.id),
            "expense_id": [str(a.id), str(b.id)],
            "memo": "March",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/owner"
    row = session.exec(select(Reimbursement)).one()
    assert row.total == D("100.00")
    page = owner_client.get("/owner")
    assert f'value="{c.id}"' in page.text
    assert f'value="{a.id}"' not in page.text.split('id="open-expenses"')[1].split("</table>")[0]
    assert "$5.50" in page.text.split('id="owed-to-me"')[1].split("</")[0]
    assert "$5.50" in page.text.split('id="open-total"')[1].split("</")[0]
    assert 'id="mismatch-note"' not in page.text
    assert f"/owner/reimbursements/{row.id}/void" in page.text
    assert "March" in page.text


def test_double_submit_creates_one_reimbursement(
    owner_client, session, owner_user, chart, expense_account
):
    a, b, _ = seed_three(session, owner_user, expense_account)
    data = {
        "reimbursement_date": "2026-03-10",
        "bank_account_id": str(chart["Business Checking"].id),
        "expense_id": [str(a.id), str(b.id)],
    }
    assert owner_client.post("/owner/reimburse", data=data).status_code == 200  # follows 303
    second = owner_client.post("/owner/reimburse", data=data, follow_redirects=False)
    assert second.status_code in (409, 422)
    assert 'role="alert"' in second.text
    assert count(session, Reimbursement) == 1


def test_reimburse_validation_errors_are_422(
    owner_client, session, owner_user, chart, expense_account
):
    a, b, _ = seed_three(session, owner_user, expense_account)
    bank = str(chart["Business Checking"].id)
    ok = {"reimbursement_date": "2026-03-10", "bank_account_id": bank, "expense_id": [str(a.id)]}
    cases = [
        ({"expense_id": []}, "Choose at least one expense"),
        ({"expense_id": [str(a.id), str(a.id)]}, None),
        ({"expense_id": ["abc"]}, None),
        ({"expense_id": ["0"]}, None),
        ({"expense_id": ["99999999999"]}, None),
        ({"expense_id": [str(a.id), ""]}, None),
        ({"reimbursement_date": ""}, None),
        ({"reimbursement_date": "2026-02-30"}, None),
        ({"reimbursement_date": "2026-03-10T00:00"}, None),
        ({"reimbursement_date": "2026-02-01"}, None),  # before the expense date
        ({"bank_account_id": ""}, None),
        ({"bank_account_id": "abc"}, None),
        ({"bank_account_id": str(chart["Due to Owner"].id)}, None),
        ({"bank_account_id": "4000000"}, None),
        ({"expense_id": ["987654"]}, None),
        ({"memo": "m" * 501}, None),
    ]
    for over, text in cases:
        data = {**ok, **over}
        response = owner_client.post("/owner/reimburse", data=data, follow_redirects=False)
        assert response.status_code == 422, (over, response.status_code)
        if text:
            assert text in response.text
        assert 'role="alert"' in response.text
    assert count(session, Reimbursement) == 0
    kept = owner_client.post("/owner/reimburse", data={**ok, "expense_id": [], "memo": "Keep"})
    assert 'value="Keep"' in kept.text and 'value="2026-03-10"' in kept.text


def test_mismatch_note_states_both_figures(
    owner_client, session, owner_user, chart, expense_account, owner_role
):
    owner_expense(session, owner_user, expense_account, "10.00")
    JournalService(session).create_manual(
        EntryDraft(
            date(2026, 3, 10),
            [
                LineDraft(chart["Business Checking"].id, debit=D("7.00")),
                LineDraft(owner_role.id, credit=D("7.00")),
            ],
            memo="Owner put in cash",
        ),
        actor=owner_user,
    )
    session.commit()
    assert owner_summary(session).matches is False
    page = owner_client.get("/owner")
    note = page.text.split('id="mismatch-note"')[1].split("</div>")[0]
    assert "$17.00" in note and "$10.00" in note
    assert "$17.00" in page.text.split('id="owed-to-me"')[1].split("</")[0]


def test_void_reimbursement_reopens_expenses(
    owner_client, session, owner_user, chart, expense_account
):
    a, b, c = seed_three(session, owner_user, expense_account)
    row = ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=date(2026, 3, 10),
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[a.id, b.id],
    )
    session.commit()
    before = owner_client.get("/owner").text
    assert f'name="expense_id" value="{a.id}"' not in before
    for bad in ("", "x", "2026-03-09"):
        response = owner_client.post(
            f"/owner/reimbursements/{row.id}/void", data={"void_date": bad}
        )
        assert response.status_code == 422, bad
    response = owner_client.post(
        f"/owner/reimbursements/{row.id}/void",
        data={"void_date": "2026-03-11"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/owner"
    after = owner_client.get("/owner").text
    assert f'name="expense_id" value="{a.id}"' in after
    assert f'name="expense_id" value="{b.id}"' in after
    assert "voided" in after
    assert f"/owner/reimbursements/{row.id}/void" not in after
    assert "$105.50" in after.split('id="owed-to-me"')[1].split("</")[0]
    again = owner_client.post(
        f"/owner/reimbursements/{row.id}/void", data={"void_date": "2026-03-12"}
    )
    assert again.status_code in (409, 422)
    for path in ("/owner/reimbursements/abc/void", "/owner/reimbursements/0/void"):
        assert owner_client.post(path, data={"void_date": "2026-03-12"}).status_code == 422
    assert (
        owner_client.post(
            "/owner/reimbursements/999999/void", data={"void_date": "2026-03-12"}
        ).status_code
        == 404
    )


def test_history_lists_the_expenses_each_reimbursement_covered(
    owner_client, session, owner_user, chart, expense_account
):
    a, b, c = seed_three(session, owner_user, expense_account)
    ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=date(2026, 3, 10),
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[a.id],
    )
    session.commit()
    history = owner_client.get("/owner").text.split('id="reimbursement-history"')[1]
    assert f"/expenses/{a.id}" in history


def test_owner_page_escapes_names_and_memos(
    owner_client, session, owner_user, chart, expense_account
):
    a = owner_expense(session, owner_user, expense_account, "12.00", payee_text=IMG_XSS)
    ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=date(2026, 3, 10),
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[a.id],
        memo=IMG_XSS,
    )
    owner_expense(session, owner_user, expense_account, "3.00", payee_text=IMG_XSS)
    page = owner_client.get("/owner")
    assert ESCAPED in page.text
    assert IMG_XSS not in page.text


# ----------------------------------------------------------------------------- roles


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_other_roles_read_but_cannot_post(
    request, role_client, session, owner_user, chart, expense_account, card
):
    row = owner_expense(session, owner_user, expense_account, "9.00")
    transfer = TransferService(session).create(
        actor=owner_user,
        transfer_date=date(2026, 3, 5),
        from_account_id=chart["Business Checking"].id,
        to_account_id=card.id,
        amount=D("5.00"),
    )
    reimbursement = ReimbursementService(session).create(
        actor=owner_user,
        reimbursement_date=date(2026, 3, 10),
        paid_from_account_id=chart["Business Checking"].id,
        expense_ids=[row.id],
    )
    session.commit()
    client = request.getfixturevalue(role_client)
    for path in ("/transfers", f"/transfers/{transfer.id}", "/owner"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "/void" not in page.text and "/owner/reimburse" not in page.text, path
        assert 'action="/transfers' not in page.text and 'action="/owner' not in page.text, path
    assert client.get("/transfers/new").status_code == 403
    posts = [
        ("/transfers/new", transfer_form(chart, card)),
        (f"/transfers/{transfer.id}/void", {"void_date": "2026-03-06"}),
        ("/owner/reimburse", {"expense_id": [str(row.id)]}),
        (f"/owner/reimbursements/{reimbursement.id}/void", {"void_date": "2026-03-11"}),
    ]
    for path, data in posts:
        assert client.post(path, data=data).status_code == 403, path
    assert count(session, Transfer) == 1 and count(session, Reimbursement) == 1


def test_anonymous_is_refused(anon_client):
    for path in ("/transfers", "/transfers/new", "/transfers/1", "/owner"):
        assert anon_client.get(path, follow_redirects=False).status_code in (303, 401), path
    for path in ("/transfers/new", "/owner/reimburse", "/owner/reimbursements/1/void"):
        assert anon_client.post(path, follow_redirects=False).status_code in (303, 401, 403), path


def test_nav_links(owner_client):
    page = owner_client.get("/owner")
    assert 'href="/transfers"' in page.text and 'href="/owner"' in page.text
