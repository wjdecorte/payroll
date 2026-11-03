from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import text

from payroll.ledger.models import SourceType
from payroll.ledger.services import EntryDraft, LedgerPostingService, LineDraft

PREFIX = "/payroll/api/v1/ledger"
URL = f"{PREFIX}/journal-entries"


def _body(chart, debit="100.00", credit="100.00", entry_date="2026-01-15", **kw) -> dict:
    return {
        "entryDate": entry_date,
        "memo": "Owner contribution",
        "lines": [
            {"accountId": chart["Business Checking"].id, "debit": debit, "memo": "in"},
            {"accountId": chart["Common Stock"].id, "credit": credit},
        ],
        **kw,
    }


def test_create_returns_201_with_money_strings(owner_client, chart):
    response = owner_client.post(URL, json=_body(chart, "1250.5", "1250.50"))
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["entryNo"] == 1
    assert data["sourceType"] == "manual"
    assert data["entryDate"] == "2026-01-15"
    assert data["memo"] == "Owner contribution"
    assert data["status"] == "posted"
    assert data["reversesEntryId"] is None
    assert data["reversedByEntryId"] is None
    assert data["totalDebit"] == "1250.50"
    assert data["totalCredit"] == "1250.50"
    first, second = data["lines"]
    assert first == {
        "lineNo": 1,
        "accountId": chart["Business Checking"].id,
        "accountNumber": "1000",
        "accountName": "Business Checking",
        "debit": "1250.50",
        "credit": "0.00",
        "memo": "in",
    }
    assert second["accountName"] == "Common Stock"
    assert second["credit"] == "1250.50"
    assert second["debit"] == "0.00"
    fetched = owner_client.get(f"{URL}/{data['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == data


def test_unbalanced_entry_returns_structured_422(owner_client, chart):
    response = owner_client.post(URL, json=_body(chart, credit="99.99"))
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["type"] == "UnbalancedEntryError"
    assert error["code"].startswith("payroll.error.")
    assert "100.00" in error["message"] and "99.99" in error["message"]


def test_invalid_amount_and_account_return_structured_422(owner_client, chart):
    huge = owner_client.post(URL, json=_body(chart, debit="-5", credit="-5"))
    assert huge.status_code == 422
    assert huge.json()["errors"][0]["type"] == "InvalidEntryError"
    body = _body(chart)
    body["lines"][0]["accountId"] = 999999
    unknown = owner_client.post(URL, json=body)
    assert unknown.status_code == 422
    assert unknown.json()["errors"][0]["type"] == "InvalidEntryError"


def test_three_decimal_amount_is_rejected_by_the_schema(owner_client, chart):
    response = owner_client.post(URL, json=_body(chart, "10.005", "10.005"))
    assert response.status_code == 422


def test_unknown_entry_is_404(owner_client, chart):
    response = owner_client.get(f"{URL}/999999")
    assert response.status_code == 404
    assert response.json()["errors"][0]["type"] == "EntryNotFoundError"


def test_viewer_reads_but_cannot_mutate(owner_client, viewer_client, chart):
    pk = owner_client.post(URL, json=_body(chart)).json()["id"]
    assert viewer_client.get(URL).status_code == 200
    assert viewer_client.get(f"{URL}/{pk}").status_code == 200
    assert viewer_client.post(URL, json=_body(chart)).status_code == 403
    action = {"entryDate": "2026-01-20", "memo": None}
    assert viewer_client.post(f"{URL}/{pk}/reverse", json=action).status_code == 403
    assert viewer_client.post(f"{URL}/{pk}/void", json=action).status_code == 403


def test_accountant_cannot_mutate(accountant_client, chart):
    assert accountant_client.post(URL, json=_body(chart)).status_code == 403


def test_reverse_round_trip(owner_client, chart):
    original = owner_client.post(URL, json=_body(chart)).json()
    response = owner_client.post(
        f"{URL}/{original['id']}/reverse", json={"entryDate": "2026-01-31", "memo": None}
    )
    assert response.status_code == 201, response.text
    reversal = response.json()
    assert reversal["entryNo"] == 2
    assert reversal["memo"] == "Reversal of manual #1"
    assert reversal["reversesEntryId"] == original["id"]
    assert [(ln["debit"], ln["credit"]) for ln in reversal["lines"]] == [
        ("0.00", "100.00"),
        ("100.00", "0.00"),
    ]
    again = owner_client.get(f"{URL}/{original['id']}").json()
    assert again["status"] == "posted"
    assert again["reversedByEntryId"] == reversal["id"]
    twice = owner_client.post(f"{URL}/{original['id']}/reverse", json={"entryDate": "2026-02-01"})
    assert twice.status_code == 409
    assert twice.json()["errors"][0]["type"] == "AlreadyReversedError"


def test_void_round_trip(owner_client, chart):
    original = owner_client.post(URL, json=_body(chart)).json()
    response = owner_client.post(
        f"{URL}/{original['id']}/void", json={"entryDate": "2026-01-16", "memo": "Duplicate"}
    )
    assert response.status_code == 201, response.text
    reversal = response.json()
    assert reversal["memo"] == "Duplicate"
    assert reversal["status"] == "posted"
    voided = owner_client.get(f"{URL}/{original['id']}").json()
    assert voided["status"] == "void"
    assert voided["reversedByEntryId"] == reversal["id"]
    accounts = owner_client.get(f"{PREFIX}/accounts").json()
    assert {a["balance"] for a in accounts} == {"0.00"}


@pytest.mark.parametrize("action", ["reverse", "void"])
def test_reverse_or_void_into_locked_period_is_409(owner_client, chart, session, action):
    original = owner_client.post(URL, json=_body(chart, entry_date="2026-04-02")).json()
    session.execute(text("UPDATE period_lock SET lock_date = '2026-03-31' WHERE id = 1"))
    session.commit()
    response = owner_client.post(
        f"{URL}/{original['id']}/{action}", json={"entryDate": "2026-03-31"}
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["type"] == "PeriodLockedError"
    again = owner_client.get(f"{URL}/{original['id']}").json()
    assert again["status"] == "posted"
    assert again["reversedByEntryId"] is None


def test_list_filters_by_date_range_and_source_type(owner_client, owner_user, chart, session):
    for day in ("2026-01-10", "2026-02-10", "2026-03-10"):
        assert owner_client.post(URL, json=_body(chart, entry_date=day)).status_code == 201
    cash, equity = chart["Business Checking"].id, chart["Opening Balance Equity"].id
    LedgerPostingService(session).post(
        EntryDraft(
            date(2026, 2, 1),
            [LineDraft(cash, debit=Decimal("7")), LineDraft(equity, credit=Decimal("7"))],
            source_type=SourceType.OPENING_BALANCE,
        ),
        actor=owner_user,
    )

    everything = owner_client.get(URL).json()
    assert [e["entryDate"] for e in everything] == [
        "2026-03-10",
        "2026-02-10",
        "2026-02-01",
        "2026-01-10",
    ]
    feb = owner_client.get(URL, params={"start": "2026-02-01", "end": "2026-02-28"}).json()
    assert [e["entryDate"] for e in feb] == ["2026-02-10", "2026-02-01"]
    opening = owner_client.get(URL, params={"sourceType": "opening_balance"}).json()
    assert [(e["sourceType"], e["entryNo"]) for e in opening] == [("opening_balance", 1)]
    assert opening[0]["lines"][1]["accountName"] == "Opening Balance Equity"
    paged = owner_client.get(URL, params={"limit": 1, "offset": 1}).json()
    assert [e["entryDate"] for e in paged] == ["2026-02-10"]
    assert owner_client.get(URL, params={"limit": 0}).status_code == 422


def test_voiding_a_voids_reversal_is_409(owner_client, chart):
    original = owner_client.post(URL, json=_body(chart)).json()
    reversal = owner_client.post(
        f"{URL}/{original['id']}/void", json={"entryDate": "2026-01-16"}
    ).json()
    for action in ("void", "reverse"):
        response = owner_client.post(
            f"{URL}/{reversal['id']}/{action}", json={"entryDate": "2026-01-17"}
        )
        assert response.status_code == 409
        assert response.json()["errors"][0]["type"] == "AlreadyReversedError"
    assert owner_client.get(f"{URL}/{original['id']}").json()["status"] == "void"


def test_void_of_locked_entry_is_409_but_reverse_after_lock_works(owner_client, chart, session):
    original = owner_client.post(URL, json=_body(chart, entry_date="2026-03-02")).json()
    session.execute(text("UPDATE period_lock SET lock_date = '2026-03-31' WHERE id = 1"))
    session.commit()
    voided = owner_client.post(f"{URL}/{original['id']}/void", json={"entryDate": "2026-04-02"})
    assert voided.status_code == 409
    assert voided.json()["errors"][0]["type"] == "PeriodLockedError"
    assert owner_client.get(f"{URL}/{original['id']}").json()["status"] == "posted"
    reversed_ = owner_client.post(
        f"{URL}/{original['id']}/reverse", json={"entryDate": "2026-04-02"}
    )
    assert reversed_.status_code == 201
