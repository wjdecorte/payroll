"""
Integration tests for the payroll_run HTTP endpoints.
Uses TestClient against the Postgres test database. Money is exchanged as JSON
strings ("8000.00"); inputs may also be JSON numbers.
"""

from decimal import Decimal

import pytest

PREFIX = "/payroll/api/v1"

# Saving a run needs the year's FUTA / SUTA settings.
pytestmark = pytest.mark.usefixtures("year_settings")


class TestCalculateEndpoint:
    def test_calculate_returns_201(self, owner_client, payroll_input):
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 201

    def test_calculate_returns_net_pay(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxDetail" in data
        net_pay = data["taxDetail"]["netPay"]
        assert isinstance(net_pay, str)
        assert Decimal(net_pay) > 0

    def test_calculate_returns_journal_entries(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "journalEntries" in data
        assert len(data["journalEntries"]) > 0

    def test_calculate_journal_entries_balanced(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        for entry in data["journalEntries"]:
            dr = sum(Decimal(ln["debit"]) for ln in entry["lines"])
            cr = sum(Decimal(ln["credit"]) for ln in entry["lines"])
            assert dr == cr, f"Unbalanced entry: {entry['title']}"
            assert entry["drTotal"] == entry["crTotal"] == f"{dr:.2f}"
            assert entry["isBalanced"] is True

    def test_calculate_saves_run(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert data["saved"] is True
        assert data["id"] is not None

    def test_calculate_without_saving(self, owner_client, payroll_input):
        payroll_input["saveRun"] = False
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert data["saved"] is False
        assert data["id"] is None

    def test_duplicate_period_returns_409(self, owner_client, payroll_input):
        owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 409

    def test_invalid_pay_period_returns_422(self, owner_client, payroll_input):
        payroll_input["payPeriod"] = "bad-date"
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 422

    def test_zero_salary_returns_422(self, owner_client, payroll_input):
        payroll_input["grossSalary"] = 0
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 422

    def test_money_serializes_as_cent_strings(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert data["grossSalary"] == "8000.00"
        assert data["healthInsurance"] == "500.00"
        assert data["taxDetail"]["ficaWages"] == "8000.00"
        assert data["taxDetail"]["ssEe"] == "496.00"
        line = data["journalEntries"][0]["lines"][0]
        assert line["debit"] == "8800.00"
        assert line["credit"] == "0.00"

    def test_accepts_json_number_inputs(self, owner_client, payroll_input):
        payroll_input |= {"grossSalary": 8000, "healthInsurance": 500.5, "saveRun": False}
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 201
        assert response.json()["healthInsurance"] == "500.50"

    def test_sub_cent_input_returns_422(self, owner_client, payroll_input):
        payroll_input["grossSalary"] = "8000.005"
        response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        assert response.status_code == 422

    def test_returns_tax_constants(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxConstants" in data
        assert data["taxConstants"]["taxYear"] == 2026

    def test_returns_tax_payment_summary(self, owner_client, payroll_input):
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        assert "taxPaymentSummary" in data
        summary = data["taxPaymentSummary"]
        assert Decimal(summary["totalFederalEftps"]) > 0
        assert Decimal(summary["georgiaIncomeTax"]) > 0


class TestListRunsEndpoint:
    def test_list_returns_200(self, owner_client):
        response = owner_client.get(f"{PREFIX}/payroll/runs")
        assert response.status_code == 200

    def test_list_initially_empty(self, owner_client):
        data = owner_client.get(f"{PREFIX}/payroll/runs").json()
        assert data == []

    def test_list_contains_saved_run(self, owner_client, payroll_input):
        owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        data = owner_client.get(f"{PREFIX}/payroll/runs").json()
        assert len(data) == 1
        assert data[0]["payPeriod"] == "2026-01"

    def test_list_pagination(self, owner_client, payroll_input, payroll_input_no_benefits):
        owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input_no_benefits)

        data = owner_client.get(f"{PREFIX}/payroll/runs?limit=1").json()
        assert len(data) == 1


class TestGetRunEndpoint:
    def test_get_existing_run_returns_200(self, owner_client, payroll_input):
        created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        run_id = created["id"]
        response = owner_client.get(f"{PREFIX}/payroll/runs/{run_id}")
        assert response.status_code == 200

    def test_get_run_returns_full_result(self, owner_client, payroll_input):
        created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        data = owner_client.get(f"{PREFIX}/payroll/runs/{created['id']}").json()
        assert data["payPeriod"] == "2026-01"
        assert "journalEntries" in data

    def test_get_run_returns_same_money_as_calculate(self, owner_client, payroll_input):
        created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        data = owner_client.get(f"{PREFIX}/payroll/runs/{created['id']}").json()
        for key in ("grossSalary", "healthInsurance", "hsaContribution"):
            assert data[key] == created[key]
        projections = {"annualFedTax", "gaAnnualTaxable", "annualGaTax"}  # not stored
        stored = {k: v for k, v in created["taxDetail"].items() if k not in projections}
        assert {k: data["taxDetail"][k] for k in stored} == stored
        assert all(data["taxDetail"][k] is None for k in projections)
        assert data["journalEntries"] == created["journalEntries"]
        assert data["taxPaymentSummary"] == created["taxPaymentSummary"]

    def test_list_returns_money_strings(self, owner_client, payroll_input):
        owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
        run = owner_client.get(f"{PREFIX}/payroll/runs").json()[0]
        assert run["grossSalary"] == "8000.00"
        assert isinstance(run["netPay"], str)

    def test_get_nonexistent_run_returns_404(self, owner_client):
        response = owner_client.get(f"{PREFIX}/payroll/runs/99999")
        assert response.status_code == 404


class TestDeleteRunEndpoint:
    def test_delete_existing_run_returns_204(self, owner_client, payroll_input):
        created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        response = owner_client.delete(f"{PREFIX}/payroll/runs/{created['id']}")
        assert response.status_code == 204

    def test_delete_removes_from_list(self, owner_client, payroll_input):
        created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
        owner_client.delete(f"{PREFIX}/payroll/runs/{created['id']}")
        data = owner_client.get(f"{PREFIX}/payroll/runs").json()
        assert data == []

    def test_delete_nonexistent_returns_404(self, owner_client):
        response = owner_client.delete(f"{PREFIX}/payroll/runs/99999")
        assert response.status_code == 404


class TestTaxConstantsEndpoint:
    def test_returns_200(self, owner_client):
        response = owner_client.get(f"{PREFIX}/payroll/tax-constants")
        assert response.status_code == 200

    def test_returns_correct_tax_year(self, owner_client):
        data = owner_client.get(f"{PREFIX}/payroll/tax-constants").json()
        assert data["taxYear"] == 2026

    def test_returns_ss_wage_base(self, owner_client):
        data = owner_client.get(f"{PREFIX}/payroll/tax-constants").json()
        assert data["ssWageBase"] == "184500.00"
        assert data["ssEeRate"] == "0.062"


class TestConfigEndpoint:
    def test_get_config_returns_defaults(self, owner_client):
        response = owner_client.get(f"{PREFIX}/payroll/config")
        assert response.status_code == 200
        data = response.json()
        assert data["acctChecking"] == "Business Checking"
        assert data["defaultHealthInIncomeTax"] is True

    def test_update_config_persists_accounts_and_benefits(self, owner_client):
        data = owner_client.get(f"{PREFIX}/payroll/config").json()
        data["defaultHealthInsurance"] = 725.5  # a JSON number is accepted
        data["defaultHsaContribution"] = "350"
        data["acctChecking"] = "Operating Bank"

        response = owner_client.put(f"{PREFIX}/payroll/config", json=data)
        assert response.status_code == 200

        updated = owner_client.get(f"{PREFIX}/payroll/config").json()
        assert updated["defaultHealthInsurance"] == "725.50"
        assert updated["defaultHsaContribution"] == "350.00"
        assert updated["acctChecking"] == "Operating Bank"

    def test_calculate_uses_configured_checking_account(self, owner_client, payroll_input):
        config = owner_client.get(f"{PREFIX}/payroll/config").json()
        config["acctChecking"] = "Operating Bank"
        owner_client.put(f"{PREFIX}/payroll/config", json=config)

        payroll_input["saveRun"] = False
        data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()

        accounts = [line["account"] for entry in data["journalEntries"] for line in entry["lines"]]
        assert "Operating Bank" in accounts
