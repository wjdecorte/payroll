"""
Integration tests for the payroll_run HTTP endpoints.
Uses TestClient with the test in-memory database.
"""

PREFIX = "/payroll/api/v1"


class TestCalculateEndpoint:
    def test_calculate_returns_201(self, client, payroll_input):
        response = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 201

    def test_calculate_returns_net_pay(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxDetail" in data
        assert data["taxDetail"]["netPay"] > 0

    def test_calculate_returns_journal_entries(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "journalEntries" in data
        assert len(data["journalEntries"]) > 0

    def test_calculate_journal_entries_balanced(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        for entry in data["journalEntries"]:
            dr = sum(ln["debit"] for ln in entry["lines"])
            cr = sum(ln["credit"] for ln in entry["lines"])
            assert abs(dr - cr) < 0.02, f"Unbalanced entry: {entry['title']}"

    def test_calculate_saves_run(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert data["saved"] is True
        assert data["id"] is not None

    def test_calculate_without_saving(self, client, payroll_input):
        payroll_input["saveRun"] = False
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert data["saved"] is False
        assert data["id"] is None

    def test_duplicate_period_returns_409(self, client, payroll_input):
        client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        response = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 409

    def test_invalid_pay_period_returns_422(self, client, payroll_input):
        payroll_input["payPeriod"] = "bad-date"
        response = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 422

    def test_zero_salary_returns_422(self, client, payroll_input):
        payroll_input["grossSalary"] = 0
        response = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 422

    def test_returns_tax_constants(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxConstants" in data
        assert data["taxConstants"]["taxYear"] == 2026

    def test_returns_tax_payment_summary(self, client, payroll_input):
        data = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxPaymentSummary" in data
        summary = data["taxPaymentSummary"]
        assert summary["totalFederalEftps"] > 0
        assert summary["georgiaIncomeTax"] > 0


class TestListRunsEndpoint:
    def test_list_returns_200(self, client):
        response = client.get(f"{PREFIX}/payroll/runs")
        assert response.status_code == 200

    def test_list_initially_empty(self, client):
        data = client.get(f"{PREFIX}/payroll/runs").json()
        assert data == []

    def test_list_contains_saved_run(self, client, payroll_input):
        client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        data = client.get(f"{PREFIX}/payroll/runs").json()
        assert len(data) == 1
        assert data[0]["payPeriod"] == "2026-01"

    def test_list_pagination(self, client, payroll_input, payroll_input_no_benefits):
        client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input_no_benefits)

        data = client.get(f"{PREFIX}/payroll/runs?limit=1").json()
        assert len(data) == 1


class TestGetRunEndpoint:
    def test_get_existing_run_returns_200(self, client, payroll_input):
        created = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        run_id = created["id"]
        response = client.get(f"{PREFIX}/payroll/runs/{run_id}")
        assert response.status_code == 200

    def test_get_run_returns_full_result(self, client, payroll_input):
        created = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        data = client.get(f"{PREFIX}/payroll/runs/{created['id']}").json()
        assert data["payPeriod"] == "2026-01"
        assert "journalEntries" in data

    def test_get_nonexistent_run_returns_404(self, client):
        response = client.get(f"{PREFIX}/payroll/runs/99999")
        assert response.status_code == 404


class TestDeleteRunEndpoint:
    def test_delete_existing_run_returns_204(self, client, payroll_input):
        created = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        response = client.delete(f"{PREFIX}/payroll/runs/{created['id']}")
        assert response.status_code == 204

    def test_delete_removes_from_list(self, client, payroll_input):
        created = client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        client.delete(f"{PREFIX}/payroll/runs/{created['id']}")
        data = client.get(f"{PREFIX}/payroll/runs").json()
        assert data == []

    def test_delete_nonexistent_returns_404(self, client):
        response = client.delete(f"{PREFIX}/payroll/runs/99999")
        assert response.status_code == 404


class TestTaxConstantsEndpoint:
    def test_returns_200(self, client):
        response = client.get(f"{PREFIX}/payroll/tax-constants")
        assert response.status_code == 200

    def test_returns_correct_tax_year(self, client):
        data = client.get(f"{PREFIX}/payroll/tax-constants").json()
        assert data["taxYear"] == 2026

    def test_returns_ss_wage_base(self, client):
        data = client.get(f"{PREFIX}/payroll/tax-constants").json()
        assert data["ssWageBase"] == 184_500.0
