import re
from decimal import Decimal

import pytest

from payroll.app import app
from payroll.frontend.routers import templates

PREFIX = "/payroll/api/v1"

FORM = {
    "pay_period": "2026-01",
    "gross_salary": "8000",
    "health_insurance": "500",
    "hsa_contribution": "300",
    "health_in_income_tax": "true",
    "ytd_fica_wages": "0",
    "notes": "January payroll",
}


def _calculate_json(owner_client, save: bool = True) -> dict:
    response = owner_client.post(
        f"{PREFIX}/payroll/runs/calculate",
        json={
            "payPeriod": "2026-01",
            "grossSalary": 8000,
            "healthInsurance": 500,
            "hsaContribution": 300,
            "healthInIncomeTax": True,
            "hsaInIncomeTax": False,
            "ytdFicaWages": 0,
            "saveRun": save,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_index_renders(owner_client):
    response = owner_client.get("/")
    assert response.status_code == 200
    assert "DCI Payroll" in response.text


def test_admin_renders(owner_client):
    response = owner_client.get("/admin")
    assert response.status_code == 200


def test_static_served(client):
    assert client.get("/static/favicon-32.png").status_code == 200


def test_ui_routes_hidden_from_openapi():
    # The schema is only served in debug mode; check the app's generated one.
    paths = app.openapi()["paths"]
    assert "/" not in paths
    assert not any(p.startswith("/htmx") for p in paths)


def test_calculate_returns_results_partial(owner_client):
    expected = _calculate_json(owner_client, save=False)
    response = owner_client.post("/htmx/calculate", data=FORM)
    assert response.status_code == 200
    net_pay = expected["taxDetail"]["netPay"]
    assert isinstance(net_pay, str)  # money is a JSON string
    assert f"${Decimal(net_pay):,.2f}" in response.text


def test_calculate_invalid_period_renders_error_partial(owner_client):
    response = owner_client.post("/htmx/calculate", data={**FORM, "pay_period": "bad"})
    assert response.status_code == 422
    assert "Invalid pay period" in response.text


def test_save_run_then_duplicate_conflict(owner_client):
    assert owner_client.post("/htmx/save-run", data=FORM).status_code == 200
    response = owner_client.post("/htmx/save-run", data=FORM)
    assert response.status_code == 409


def test_history_lists_saved_runs(owner_client):
    _calculate_json(owner_client)
    response = owner_client.get("/htmx/history")
    assert response.status_code == 200
    assert "2026-01" in response.text


def test_get_run_partial_and_missing(owner_client):
    run = _calculate_json(owner_client)
    assert owner_client.get(f"/htmx/runs/{run['id']}").status_code == 200
    assert owner_client.get("/htmx/runs/999999").status_code == 404


def test_delete_run_refreshes_history(owner_client):
    run = _calculate_json(owner_client)
    response = owner_client.delete(f"/htmx/runs/{run['id']}")
    assert response.status_code == 200
    assert "2026-01" not in response.text
    assert owner_client.get(f"{PREFIX}/payroll/runs").json() == []


def test_increment_journal_no_returns_badge(owner_client):
    before = owner_client.get(f"{PREFIX}/payroll/config").json()["nextJournalNo"]
    response = owner_client.post("/htmx/increment-journal-no?count=2")
    assert f"Next JE #: {before + 2}" in response.text


def test_admin_save_roundtrip(owner_client):
    config = owner_client.get(f"{PREFIX}/payroll/config").json()
    form = {
        "default_gross_salary": "9000",
        "default_notes": "hello",
        "next_journal_no": "300",
        "federal_due_date_note": config["federalDueDateNote"],
        "georgia_due_date_note": config["georgiaDueDateNote"],
    }
    for key, value in config.items():
        if key.startswith("acct"):
            snake = "".join("_" + c.lower() if c.isupper() else c for c in key)
            form[snake] = value
    response = owner_client.post("/admin", data=form)
    assert response.status_code == 200
    assert owner_client.get(f"{PREFIX}/payroll/config").json()["defaultGrossSalary"] == "9000.00"
    assert owner_client.get(f"{PREFIX}/payroll/config").json()["nextJournalNo"] == 300


def test_admin_save_missing_fields_renders_error(owner_client):
    response = owner_client.post("/admin", data={"default_gross_salary": "1"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Money rendering: services are dumped with mode="json", so money arrives in the
# templates as strings ("8000.00"); filters and conditions must handle that.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5123.45", "$5,123.45"),
        (Decimal("1234567.8"), "$1,234,567.80"),
        (8000, "$8,000.00"),
        (0.1, "$0.10"),
        ("0.00", "$0.00"),
        (None, "$0.00"),
        ("-12.50", "-$12.50"),
    ],
)
def test_currency_filter_accepts_str_decimal_int_float(value, expected):
    assert templates.env.filters["currency"](value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-12.50", Decimal("12.50")),
        (Decimal("-3"), Decimal("3")),
        (-2, 2),
        ("4.00", Decimal("4.00")),
    ],
)
def test_abs_filter_accepts_money_strings(value, expected):
    assert templates.env.filters["abs"](value) == expected


def test_money_template_conditions_use_amount_not_string_truthiness():
    tmpl = templates.env.from_string(
        "{% if v | nonzero %}yes{% else %}no{% endif %} {{ v | amount }}"
    )
    assert tmpl.render(v="0.00") == "no 0.00"
    assert tmpl.render(v="12.5") == "yes 12.50"
    assert tmpl.render(v=None) == "no 0.00"


def test_results_partial_hides_zero_benefits_and_splits_debits_credits(owner_client):
    form = {**FORM, "health_insurance": "0", "hsa_contribution": "0"}
    expected = owner_client.post(
        f"{PREFIX}/payroll/runs/calculate",
        json={
            "payPeriod": "2026-01",
            "grossSalary": "8000",
            "healthInsurance": "0",
            "hsaContribution": "0",
            "healthInIncomeTax": True,
            "ytdFicaWages": "0",
            "saveRun": False,
        },
    ).json()
    response = owner_client.post("/htmx/calculate", data=form)
    assert response.status_code == 200
    html = response.text

    assert "Medical Insurance Premium" not in html
    assert "HSA Contribution" not in html
    assert "Additional Medicare" not in html

    lines = [ln for je in expected["journalEntries"] for ln in je["lines"]]
    debits = [ln for ln in lines if Decimal(ln["debit"])]
    credits = [ln for ln in lines if Decimal(ln["credit"])]
    assert html.count('class="dr-row"') == len(debits)
    assert html.count('class="cr-row"') == len(credits)
    assert "Unbalanced" not in html and "not balanced" not in html.lower()


def test_index_prefills_money_inputs_from_saved_data(owner_client):
    config = owner_client.get(f"{PREFIX}/payroll/config").json()
    config["defaultGrossSalary"] = "8123.4"
    config["defaultHealthInsurance"] = "725.5"
    assert owner_client.put(f"{PREFIX}/payroll/config", json=config).status_code == 200
    _calculate_json(owner_client)

    response = owner_client.get("/")
    assert response.status_code == 200
    assert re.search(r'name="gross_salary"[^>]*value="8123\.40"', response.text, re.S)
    assert 'value="725.50"' in response.text


def test_admin_save_invalid_money_rerenders_form_with_input(owner_client):
    response = owner_client.post("/admin", data={"default_gross_salary": "abc"})
    assert response.status_code == 422
    assert 'value="abc"' in response.text
