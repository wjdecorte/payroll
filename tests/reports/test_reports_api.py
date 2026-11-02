import pytest

URL = "/payroll/api/v1/ledger/reports"
YEAR = "start=2026-01-01&end=2026-12-31"


def test_trial_balance_endpoint_viewer(viewer_client, books):
    response = viewer_client.get(f"{URL}/trial-balance?asOf=2026-12-31")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["asOf"] == "2026-12-31"
    assert data["totalDebit"] == "16500.00"
    assert data["totalCredit"] == "16500.00"
    assert data["balanced"] is True
    assert data["rows"][0] == {
        "accountId": books.chart["Business Checking"].id,
        "number": "1000",
        "name": "Business Checking",
        "type": "asset",
        "debit": "14999.25",
        "credit": "0.00",
    }
    assert len(data["rows"]) == 5
    zero = viewer_client.get(f"{URL}/trial-balance?asOf=2026-12-31&includeZero=true").json()
    assert len(zero["rows"]) > 5


def test_general_ledger_endpoint(viewer_client, books):
    response = viewer_client.get(f"{URL}/general-ledger?{YEAR}")
    assert response.status_code == 200, response.text
    accounts = response.json()["accounts"]
    assert [a["name"] for a in accounts][0] == "Business Checking"
    assert accounts[0]["closingBalance"] == "14999.25"
    one = viewer_client.get(
        f"{URL}/general-ledger?{YEAR}&accountId={books.chart['Office Expenses'].id}"
    ).json()
    assert [a["name"] for a in one["accounts"]] == ["Office Expenses"]


def test_journal_endpoint(viewer_client, books):
    response = viewer_client.get(f"{URL}/journal?{YEAR}")
    assert response.status_code == 200, response.text
    entries = response.json()
    assert len(entries) == 7
    assert entries[0]["sourceType"] == "opening_balance"
    assert entries[0]["lines"][0]["debit"] == "10000.00"
    assert entries[-1]["isReversal"] is True


def test_account_register_endpoint(viewer_client, books):
    account_id = books.chart["Business Checking"].id
    response = viewer_client.get(
        f"{URL}/account-register/{account_id}?start=2026-02-01&end=2026-02-28"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["openingBalance"] == "12799.50"
    assert data["closingBalance"] == "14999.25"
    assert data["lines"][0]["balance"] == "15299.50"
    assert data["lines"][0]["entryNo"] == 2


@pytest.mark.parametrize(
    "path",
    [
        "/trial-balance?asOf=2026-12-31",
        f"/general-ledger?{YEAR}",
        f"/journal?{YEAR}",
        f"/account-register/1?{YEAR}",
    ],
)
def test_anonymous_is_rejected(anon_client, path):
    assert anon_client.get(f"{URL}{path}").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/general-ledger?start=2026-03-01&end=2026-02-01",
        "/journal?start=2026-03-01&end=2026-02-01",
        "/account-register/1?start=2026-03-01&end=2026-02-01",
    ],
)
def test_start_after_end_is_422(viewer_client, books, path):
    if path.startswith("/account-register"):
        path = path.replace("/1?", f"/{books.chart['Business Checking'].id}?")
    response = viewer_client.get(f"{URL}{path}")
    assert response.status_code == 422
    assert "start date" in response.text


def test_unknown_account_is_404(viewer_client, books):
    assert viewer_client.get(f"{URL}/account-register/999999?{YEAR}").status_code == 404
    assert viewer_client.get(f"{URL}/general-ledger?{YEAR}&accountId=999999").status_code == 404


def test_missing_date_is_422(viewer_client):
    assert viewer_client.get(f"{URL}/trial-balance").status_code == 422


@pytest.fixture
def april_15(monkeypatch):
    """Freeze the company's "today" at 2026-04-15 for preset resolution."""
    from datetime import date

    from payroll.reports import routers

    monkeypatch.setattr(routers, "company_today", lambda: date(2026, 4, 15))


def test_profit_and_loss_endpoint_preset_and_compare(viewer_client, books, april_15):
    response = viewer_client.get(
        f"{URL}/profit-and-loss?preset=last_quarter&basis=cash&compare=previous_period"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["title"] == "Profit and Loss"
    assert data["basis"] == "cash"
    assert data["basisNote"].startswith("Cash and accrual agree")
    assert data["columns"] == [
        {"label": "2026-01-01 to 2026-03-31", "start": "2026-01-01", "end": "2026-03-31"},
        {"label": "2025-10-01 to 2025-12-31", "start": "2025-10-01", "end": "2025-12-31"},
    ]
    net = data["rows"][-1]
    assert net == {
        "kind": "total",
        "label": "Net Income",
        "accountId": None,
        "depth": 0,
        "amounts": ["4999.25", "0.00"],
        "change": "4999.25",
        "pctChange": None,
    }
    revenue = next(r for r in data["rows"] if r["label"] == "Services Revenue")
    assert revenue["accountId"] == books.chart["Services Revenue"].id
    assert [i["name"] for i in data["invariants"]] == [
        "assets_equal_liabilities_plus_equity",
        "trial_balance_balanced",
    ]
    assert all(i["ok"] for i in data["invariants"])


def test_profit_and_loss_endpoint_defaults_and_custom(viewer_client, books, april_15):
    default = viewer_client.get(f"{URL}/profit-and-loss").json()
    assert default["basis"] == "cash"
    assert default["columns"][0]["start"] == "2026-04-01"  # this_month
    custom = viewer_client.get(f"{URL}/profit-and-loss?{YEAR}&basis=accrual").json()
    assert custom["basis"] == "accrual"
    assert custom["rows"][-1]["amounts"] == ["4999.25"]
    explicit = viewer_client.get(f"{URL}/profit-and-loss?preset=custom&{YEAR}").json()
    assert explicit["rows"][-1]["amounts"] == ["4999.25"]


def test_balance_sheet_endpoint(viewer_client, books, april_15):
    response = viewer_client.get(f"{URL}/balance-sheet?asOf=2026-02-28&compare=previous_year")
    assert response.status_code == 200, response.text
    data = response.json()
    assert [c["label"] for c in data["columns"]] == ["As of 2026-02-28", "As of 2025-02-28"]
    totals = {r["label"]: r["amounts"] for r in data["rows"]}
    assert totals["Total Assets"] == ["14999.25", "0.00"]
    assert totals["Liabilities + Equity"] == ["14999.25", "0.00"]
    default = viewer_client.get(f"{URL}/balance-sheet").json()
    assert default["columns"][0]["label"] == "As of 2026-04-15"


@pytest.mark.parametrize(
    "path",
    [
        "/profit-and-loss?preset=this_month&basis=modified_cash",
        "/profit-and-loss?preset=this_month&compare=next_year",
        "/profit-and-loss?preset=fortnight",
        "/profit-and-loss?preset=custom&start=2026-01-01",
        "/profit-and-loss?start=2026-03-01&end=2026-02-01",
        "/balance-sheet?asOf=2026-12-31&basis=modified_cash",
        "/balance-sheet?asOf=2026-12-31&compare=next_year",
    ],
)
def test_statement_bad_params_are_422(viewer_client, books, path):
    assert viewer_client.get(f"{URL}{path}").status_code == 422


@pytest.mark.parametrize("path", ["/profit-and-loss", "/balance-sheet?asOf=2026-12-31"])
def test_statements_anonymous_is_rejected(anon_client, path):
    assert anon_client.get(f"{URL}{path}").status_code == 401
