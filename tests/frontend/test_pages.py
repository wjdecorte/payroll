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


def _calculate_json(client, save: bool = True) -> dict:
    response = client.post(
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


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "DCI Payroll" in response.text


def test_admin_renders(client):
    response = client.get("/admin")
    assert response.status_code == 200


def test_static_served(client):
    assert client.get("/static/favicon-32.png").status_code == 200


def test_ui_routes_hidden_from_openapi(client):
    paths = client.get(f"{PREFIX}/openapi.json").json()["paths"]
    assert "/" not in paths
    assert not any(p.startswith("/htmx") for p in paths)


def test_calculate_returns_results_partial(client):
    expected = _calculate_json(client, save=False)
    response = client.post("/htmx/calculate", data=FORM)
    assert response.status_code == 200
    assert f"{expected['taxDetail']['netPay']:,.2f}" in response.text


def test_calculate_invalid_period_renders_error_partial(client):
    response = client.post("/htmx/calculate", data={**FORM, "pay_period": "bad"})
    assert response.status_code == 422
    assert "Invalid pay period" in response.text


def test_save_run_then_duplicate_conflict(client):
    assert client.post("/htmx/save-run", data=FORM).status_code == 200
    response = client.post("/htmx/save-run", data=FORM)
    assert response.status_code == 409


def test_history_lists_saved_runs(client):
    _calculate_json(client)
    response = client.get("/htmx/history")
    assert response.status_code == 200
    assert "2026-01" in response.text


def test_get_run_partial_and_missing(client):
    run = _calculate_json(client)
    assert client.get(f"/htmx/runs/{run['id']}").status_code == 200
    assert client.get("/htmx/runs/999999").status_code == 404


def test_delete_run_refreshes_history(client):
    run = _calculate_json(client)
    response = client.delete(f"/htmx/runs/{run['id']}")
    assert response.status_code == 200
    assert "2026-01" not in response.text
    assert client.get(f"{PREFIX}/payroll/runs").json() == []


def test_increment_journal_no_returns_badge(client):
    before = client.get(f"{PREFIX}/payroll/config").json()["nextJournalNo"]
    response = client.post("/htmx/increment-journal-no?count=2")
    assert f"Next JE #: {before + 2}" in response.text


def test_admin_save_roundtrip(client):
    config = client.get(f"{PREFIX}/payroll/config").json()
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
    response = client.post("/admin", data=form)
    assert response.status_code == 200
    assert client.get(f"{PREFIX}/payroll/config").json()["defaultGrossSalary"] == 9000
    assert client.get(f"{PREFIX}/payroll/config").json()["nextJournalNo"] == 300


def test_admin_save_missing_fields_renders_error(client):
    response = client.post("/admin", data={"default_gross_salary": "1"})
    assert response.status_code == 422
