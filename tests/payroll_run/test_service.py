"""
Unit tests for PayrollRunService — exercises the service layer directly
with a real in-memory database session.
"""

import pytest

from payroll.payroll_run.exceptions import (
    InvalidPayPeriodError,
    PayrollRunAlreadyExistsError,
    PayrollRunNotFoundError,
)
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService


@pytest.fixture
def service(session):
    return PayrollRunService(session)


@pytest.fixture
def base_input() -> PayrollInput:
    return PayrollInput(
        pay_period="2026-03",
        gross_salary=8_000.00,
        health_insurance=500.00,
        hsa_contribution=300.00,
        health_in_income_tax=True,
        hsa_in_income_tax=False,
        ytd_fica_wages=0.0,
        save_run=True,
        notes="Test run",
    )


class TestCalculate:
    def test_returns_payroll_result(self, service, base_input):
        result = service.calculate(base_input)
        assert result.pay_period == "2026-03"
        assert result.gross_salary == 8_000.00

    def test_net_pay_is_positive(self, service, base_input):
        result = service.calculate(base_input)
        assert result.tax_detail.net_pay > 0

    def test_journal_entries_are_balanced(self, service, base_input):
        result = service.calculate(base_input)
        for entry in result.journal_entries:
            assert entry.is_balanced, f"Entry '{entry.title}' is not balanced"

    def test_saved_run_has_id(self, service, base_input):
        result = service.calculate(base_input)
        assert result.saved is True
        assert result.id is not None

    def test_unsaved_run_has_no_id(self, service, base_input):
        base_input.save_run = False
        result = service.calculate(base_input)
        assert result.saved is False
        assert result.id is None

    def test_duplicate_period_raises_error(self, service, base_input):
        service.calculate(base_input)
        with pytest.raises(PayrollRunAlreadyExistsError):
            service.calculate(base_input)

    def test_invalid_pay_period_raises_error(self, service, base_input):
        base_input.pay_period = "January-2026"
        with pytest.raises(InvalidPayPeriodError):
            service.calculate(base_input)

    def test_invalid_pay_period_bad_month(self, service, base_input):
        base_input.pay_period = "2026-13"
        with pytest.raises(InvalidPayPeriodError):
            service.calculate(base_input)

    def test_tax_payment_summary_totals(self, service, base_input):
        result = service.calculate(base_input)
        summary = result.tax_payment_summary
        expected_eftps = round(
            summary.federal_income_tax + summary.social_security_total + summary.medicare_total,
            2,
        )
        assert abs(summary.total_federal_eftps - expected_eftps) < 0.01


class TestGetRun:
    def test_get_saved_run(self, service, base_input):
        created = service.calculate(base_input)
        retrieved = service.get_run(created.id)
        assert retrieved.id == created.id
        assert retrieved.pay_period == created.pay_period

    def test_get_nonexistent_run_raises_error(self, service):
        with pytest.raises(PayrollRunNotFoundError):
            service.get_run(99999)

    def test_get_run_rebuilds_journal_entries(self, service, base_input):
        created = service.calculate(base_input)
        retrieved = service.get_run(created.id)
        assert len(retrieved.journal_entries) > 0
        for entry in retrieved.journal_entries:
            assert entry.is_balanced


class TestListRuns:
    def test_list_returns_empty_initially(self, service):
        runs = service.list_runs()
        assert runs == []

    def test_list_returns_saved_runs(self, service, base_input):
        service.calculate(base_input)
        runs = service.list_runs()
        assert len(runs) == 1

    def test_list_respects_limit(self, service, base_input):
        # Save two runs
        service.calculate(base_input)
        second = base_input.model_copy()
        second.pay_period = "2026-04"
        service.calculate(second)

        runs = service.list_runs(limit=1)
        assert len(runs) == 1

    def test_list_ordered_newest_first(self, service, base_input):
        service.calculate(base_input)
        second = base_input.model_copy()
        second.pay_period = "2026-04"
        service.calculate(second)

        runs = service.list_runs()
        assert runs[0].pay_period >= runs[-1].pay_period


class TestDeleteRun:
    def test_delete_removes_run(self, service, base_input):
        created = service.calculate(base_input)
        service.delete_run(created.id)
        assert service.list_runs() == []

    def test_delete_nonexistent_raises_error(self, service):
        with pytest.raises(PayrollRunNotFoundError):
            service.delete_run(99999)

    def test_can_recalculate_after_delete(self, service, base_input):
        created = service.calculate(base_input)
        service.delete_run(created.id)
        # Should not raise PayrollRunAlreadyExistsError
        new_result = service.calculate(base_input)
        assert new_result.id is not None


class TestUsePreviousYtdFica:
    """Tests for auto-pulling YTD FICA wages from the previous saved run."""

    def test_uses_previous_run_ytd_fica_after(self, service):
        """Second run should pick up ytd_fica_after from the first."""
        jan = PayrollInput(
            pay_period="2026-01",
            gross_salary=8_000.00,
            save_run=True,
        )
        jan_result = service.calculate(jan)
        jan_ytd_after = jan_result.tax_detail.ytd_fica_after

        feb = PayrollInput(
            pay_period="2026-02",
            gross_salary=8_000.00,
            use_previous_ytd_fica=True,
            save_run=True,
        )
        feb_result = service.calculate(feb)

        # The YTD before Feb should equal the YTD after Jan
        assert feb_result.tax_detail.ytd_fica_after == jan_ytd_after + 8_000.00

    def test_defaults_to_zero_when_no_prior_run(self, service):
        """If no saved run exists, ytd_fica_wages should default to 0."""
        result = service.calculate(
            PayrollInput(
                pay_period="2026-01",
                gross_salary=8_000.00,
                use_previous_ytd_fica=True,
                save_run=False,
            )
        )
        assert result.tax_detail.ytd_fica_after == 8_000.00  # 0 + 8000

    def test_chains_across_multiple_months(self, service):
        """Three consecutive runs should chain YTD correctly."""
        for month in ["01", "02", "03"]:
            service.calculate(
                PayrollInput(
                    pay_period=f"2026-{month}",
                    gross_salary=10_000.00,
                    use_previous_ytd_fica=True,
                    save_run=True,
                )
            )
        march = service.list_runs(limit=1)[0]  # newest first
        assert march.pay_period == "2026-03"
        # After 3 months at $10k gross: YTD after = $30,000
        run = service.get_run(march.id)
        assert run.tax_detail.ytd_fica_after == 30_000.00

    def test_does_not_cross_year_boundary(self, service):
        """A run in 2027-01 should NOT pull YTD from a 2026 run."""
        service.calculate(
            PayrollInput(
                pay_period="2026-12",
                gross_salary=8_000.00,
                save_run=True,
            )
        )
        jan_2027 = service.calculate(
            PayrollInput(
                pay_period="2027-01",
                gross_salary=8_000.00,
                use_previous_ytd_fica=True,
                save_run=False,
            )
        )
        # Should start from 0, not carry over from 2026-12
        assert jan_2027.tax_detail.ytd_fica_after == 8_000.00

    def test_overrides_manual_ytd_fica_wages(self, service):
        """When use_previous_ytd_fica=True, the manual ytd_fica_wages is ignored."""
        service.calculate(
            PayrollInput(
                pay_period="2026-01",
                gross_salary=8_000.00,
                save_run=True,
            )
        )
        result = service.calculate(
            PayrollInput(
                pay_period="2026-02",
                gross_salary=8_000.00,
                ytd_fica_wages=99_999.00,  # should be ignored
                use_previous_ytd_fica=True,
                save_run=False,
            )
        )
        # ytd_fica_after = previous ytd_fica_after (8000) + current gross (8000)
        assert result.tax_detail.ytd_fica_after == 16_000.00
