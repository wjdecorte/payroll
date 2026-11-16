"""
Unit tests for PayrollRunService — exercises the service layer directly
with a real (Postgres, rolled-back) database session. All money is Decimal.
"""

from decimal import Decimal

import pytest
from sqlmodel import func, select

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.payroll_run.config_schemas import PayrollConfigUpdate
from payroll.payroll_run.config_services import PayrollConfigService
from payroll.payroll_run.exceptions import (
    InvalidPayPeriodError,
    PayrollRunAlreadyExistsError,
    PayrollRunNotFoundError,
)
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService
from tests.tax.make_golden import build_inputs

D = Decimal

# Saving a run needs the year's FUTA / SUTA settings.
pytestmark = pytest.mark.usefixtures("year_settings")


@pytest.fixture
def service(session):
    return PayrollRunService(session)


@pytest.fixture
def base_input() -> PayrollInput:
    return PayrollInput(
        pay_period="2026-03",
        gross_salary="8000.00",
        health_insurance="500.00",
        hsa_contribution="300.00",
        health_in_income_tax=True,
        hsa_in_income_tax=False,
        ytd_fica_wages="0.00",
        save_run=True,
        notes="Test run",
    )


class TestCalculate:
    def test_returns_payroll_result(self, service, base_input, owner_user):
        result = service.calculate(base_input, actor=owner_user)
        assert result.pay_period == "2026-03"
        assert result.gross_salary == D("8000.00")
        assert isinstance(result.tax_detail.net_pay, Decimal)

    def test_net_pay_is_positive(self, service, base_input, owner_user):
        result = service.calculate(base_input, actor=owner_user)
        assert result.tax_detail.net_pay > 0

    def test_journal_entries_are_balanced(self, service, base_input, owner_user):
        result = service.calculate(base_input, actor=owner_user)
        for entry in result.journal_entries:
            assert entry.is_balanced, f"Entry '{entry.title}' is not balanced"

    def test_zero_amount_lines_and_entries_are_omitted(self, service, owner_user):
        result = service.calculate(
            PayrollInput(
                pay_period="2026-09",
                gross_salary="300.00",
                health_insurance="566.00",
                hsa_contribution="0",
                health_in_income_tax=True,
                hsa_in_income_tax=False,
                ytd_fica_wages="0",
            ),
            actor=owner_user,
        )
        assert result.tax_detail.ga_withholding == 0
        assert result.tax_detail.federal_withholding == 0
        titles = [e.title for e in result.journal_entries]
        assert not any("Georgia" in t for t in titles)
        assert [t.split(":")[0] for t in titles] == [
            f"Entry {i}" for i in range(1, len(titles) + 1)
        ]
        for entry in result.journal_entries:
            assert all(ln.debit or ln.credit for ln in entry.lines)
            assert entry.is_balanced

    def test_journal_entries_use_configured_accounts(self, service, base_input, owner_user):
        config = service.config_service.get_config()
        updated = PayrollConfigUpdate.model_validate(
            config.model_dump() | {"acct_checking": "Operating Bank"}
        )
        service.config_service.update_config(updated, actor=owner_user)

        base_input.save_run = False
        result = service.calculate(base_input, actor=owner_user)

        accounts = [line.account for entry in result.journal_entries for line in entry.lines]
        assert "Operating Bank" in accounts

    def test_tax_payment_entries_clear_payables_to_checking(self, service, base_input, owner_user):
        base_input.save_run = False
        result = service.calculate(base_input, actor=owner_user)
        entries = {e.title.split(": ", 1)[1]: e for e in result.journal_entries}

        federal = entries["Federal Tax Payment (EFTPS)"]
        georgia = entries["Georgia Tax Payment"]
        td = result.tax_detail

        expected_federal = (
            td.federal_withholding + td.ss_ee + td.ss_er + td.total_medicare_ee + td.medicare_er
        )
        assert sum(line.credit for line in federal.lines) == expected_federal
        assert sum(line.credit for line in georgia.lines) == td.ga_withholding
        assert federal.is_balanced
        assert georgia.is_balanced

    @pytest.mark.parametrize(
        ("health", "hsa", "expected_count"),
        [(D("500"), D("300"), 6), (D("0"), D("300"), 5), (D("500"), D("0"), 5), (D(0), D(0), 4)],
    )
    def test_entry_numbers_are_consecutive(
        self, service, base_input, health, hsa, expected_count, owner_user
    ):
        base_input.save_run = False
        base_input.health_insurance = health
        base_input.hsa_contribution = hsa
        result = service.calculate(base_input, actor=owner_user)

        titles = [e.title for e in result.journal_entries]
        assert len(titles) == expected_count
        for i, title in enumerate(titles, start=1):
            assert title.startswith(f"Entry {i}:"), titles
        for entry in result.journal_entries:
            assert entry.is_balanced

    def test_saved_run_has_id(self, service, base_input, owner_user):
        result = service.calculate(base_input, actor=owner_user)
        assert result.saved is True
        assert result.id is not None

    def test_unsaved_run_has_no_id(self, service, base_input, owner_user):
        base_input.save_run = False
        result = service.calculate(base_input, actor=owner_user)
        assert result.saved is False
        assert result.id is None

    def test_duplicate_period_raises_error(self, service, base_input, owner_user):
        service.calculate(base_input, actor=owner_user)
        with pytest.raises(PayrollRunAlreadyExistsError):
            service.calculate(base_input, actor=owner_user)

    def test_invalid_pay_period_raises_error(self, service, base_input, owner_user):
        base_input.pay_period = "January-2026"
        with pytest.raises(InvalidPayPeriodError):
            service.calculate(base_input, actor=owner_user)

    def test_invalid_pay_period_bad_month(self, service, base_input, owner_user):
        base_input.pay_period = "2026-13"
        with pytest.raises(InvalidPayPeriodError):
            service.calculate(base_input, actor=owner_user)

    def test_tax_payment_summary_totals(self, service, base_input, owner_user):
        result = service.calculate(base_input, actor=owner_user)
        summary = result.tax_payment_summary
        expected_eftps = (
            summary.federal_income_tax + summary.social_security_total + summary.medicare_total
        )
        assert summary.total_federal_eftps == expected_eftps

    def test_half_cent_tie_rounds_half_up(self, service, owner_user):
        # 1122.50 * 6.2% = 69.595 -> 69.60 (the old float round() gave 69.59).
        result = service.calculate(
            PayrollInput(pay_period="2026-05", gross_salary="1122.50", save_run=False),
            actor=owner_user,
        )
        assert result.tax_detail.ss_ee == Decimal("69.60")
        assert result.tax_detail.ss_er == Decimal("69.60")

    @pytest.mark.parametrize(
        "inputs",
        [i for i in build_inputs() if i["gross_salary"] > 0],
        ids=lambda i: "-".join(str(v) for v in i.values()),
    )
    def test_journal_debits_equal_credits_exactly_for_golden_grid(
        self, service, inputs, owner_user
    ):
        payload = PayrollInput(
            pay_period="2026-06",
            save_run=False,
            **{k: v if isinstance(v, bool) else str(v) for k, v in inputs.items()},
        )
        result = service.calculate(payload, actor=owner_user)
        for entry in result.journal_entries:
            debits = sum((ln.debit for ln in entry.lines), Decimal(0))
            credits = sum((ln.credit for ln in entry.lines), Decimal(0))
            assert debits == credits, entry.title
            assert entry.dr_total == entry.cr_total == debits
            assert entry.is_balanced
            for line in entry.lines:
                assert isinstance(line.debit, Decimal) and isinstance(line.credit, Decimal)


class TestGetRun:
    def test_get_saved_run(self, service, base_input, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        retrieved = service.get_run(created.id)
        assert retrieved.id == created.id
        assert retrieved.pay_period == created.pay_period

    def test_get_nonexistent_run_raises_error(self, service):
        with pytest.raises(PayrollRunNotFoundError):
            service.get_run(99999)

    def test_get_run_returns_stored_values(self, service, session, base_input, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        stored = session.get(PayrollRun, created.id)

        retrieved = service.get_run(created.id)

        td = retrieved.tax_detail
        for field in (
            "fica_wages",
            "income_tax_wages",
            "ss_taxable_wages",
            "ss_ee",
            "ss_er",
            "medicare_ee",
            "additional_medicare",
            "total_medicare_ee",
            "medicare_er",
            "federal_withholding",
            "ga_withholding",
            "total_employee_deductions",
            "net_pay",
            "total_employer_taxes",
            "total_employer_cost",
            "ytd_fica_after",
        ):
            assert getattr(td, field) == getattr(stored, field), field
            assert getattr(td, field) == getattr(created.tax_detail, field), field
        assert td.ytd_fica_before == stored.ytd_fica_before
        assert retrieved.gross_salary == stored.gross_salary
        assert retrieved.journal_entries == created.journal_entries
        assert retrieved.tax_payment_summary == created.tax_payment_summary

    def test_get_run_never_recalculates(self, service, session, owner_user):
        """A run booked under the old float rounding must come back exactly as stored.

        gross 1122.50 recalculates to ss_ee 69.60 today, but a run saved by the
        float engine stored 69.59 — and that is what was booked in QuickBooks.
        """
        created = service.calculate(
            PayrollInput(pay_period="2026-07", gross_salary="1122.50", save_run=True),
            actor=owner_user,
        )
        stored = session.get(PayrollRun, created.id)
        assert stored.ss_ee == D("69.60")
        # Simulate the legacy row: one cent less SS withheld, one cent more net pay.
        stored.ss_ee = D("69.59")
        stored.total_employee_deductions -= D("0.01")
        stored.net_pay += D("0.01")
        session.add(stored)
        session.commit()

        retrieved = service.get_run(created.id)

        assert retrieved.tax_detail.ss_ee == D("69.59")
        assert retrieved.tax_detail.net_pay == stored.net_pay
        entry1 = retrieved.journal_entries[0]
        ss_line = next(ln for ln in entry1.lines if ln.memo.startswith("Employee Social Security"))
        assert ss_line.credit == D("69.59")
        assert entry1.is_balanced
        assert retrieved.tax_payment_summary.social_security_total == D("69.59") + stored.ss_er

    def test_get_run_rebuilds_journal_entries(self, service, base_input, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        retrieved = service.get_run(created.id)
        assert len(retrieved.journal_entries) > 0
        for entry in retrieved.journal_entries:
            assert entry.is_balanced


class TestListRuns:
    def test_list_returns_empty_initially(self, service):
        runs = service.list_runs()
        assert runs == []

    def test_list_returns_saved_runs(self, service, base_input, owner_user):
        service.calculate(base_input, actor=owner_user)
        runs = service.list_runs()
        assert len(runs) == 1

    def test_list_respects_limit(self, service, base_input, owner_user):
        # Save two runs
        service.calculate(base_input, actor=owner_user)
        second = base_input.model_copy()
        second.pay_period = "2026-04"
        service.calculate(second, actor=owner_user)

        runs = service.list_runs(limit=1)
        assert len(runs) == 1

    def test_list_ordered_newest_first(self, service, base_input, owner_user):
        service.calculate(base_input, actor=owner_user)
        second = base_input.model_copy()
        second.pay_period = "2026-04"
        service.calculate(second, actor=owner_user)

        runs = service.list_runs()
        assert runs[0].pay_period >= runs[-1].pay_period


class TestDeleteRun:
    def test_delete_removes_run(self, service, base_input, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        service.delete_run(created.id, actor=owner_user)
        assert service.list_runs() == []

    def test_delete_nonexistent_raises_error(self, service, owner_user):
        with pytest.raises(PayrollRunNotFoundError):
            service.delete_run(99999, actor=owner_user)

    def test_can_recalculate_after_delete(self, service, base_input, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        service.delete_run(created.id, actor=owner_user)
        # Should not raise PayrollRunAlreadyExistsError
        new_result = service.calculate(base_input, actor=owner_user)
        assert new_result.id is not None


class TestUsePreviousYtdFica:
    """Tests for auto-pulling YTD FICA wages from the previous saved run."""

    def test_uses_previous_run_ytd_fica_after(self, service, owner_user):
        """Second run should pick up ytd_fica_after from the first."""
        jan = PayrollInput(
            pay_period="2026-01",
            gross_salary="8000.00",
            save_run=True,
        )
        jan_result = service.calculate(jan, actor=owner_user)
        jan_ytd_after = jan_result.tax_detail.ytd_fica_after

        feb = PayrollInput(
            pay_period="2026-02",
            gross_salary="8000.00",
            use_previous_ytd_fica=True,
            save_run=True,
        )
        feb_result = service.calculate(feb, actor=owner_user)

        # The YTD before Feb should equal the YTD after Jan
        assert feb_result.tax_detail.ytd_fica_after == jan_ytd_after + D("8000.00")

    def test_defaults_to_zero_when_no_prior_run(self, service, owner_user):
        """If no saved run exists, ytd_fica_wages should default to 0."""
        result = service.calculate(
            PayrollInput(
                pay_period="2026-01",
                gross_salary="8000.00",
                use_previous_ytd_fica=True,
                save_run=False,
            ),
            actor=owner_user,
        )
        assert result.tax_detail.ytd_fica_after == D("8000.00")  # 0 + 8000

    def test_chains_across_multiple_months(self, service, owner_user):
        """Three consecutive runs should chain YTD correctly."""
        for month in ["01", "02", "03"]:
            service.calculate(
                PayrollInput(
                    pay_period=f"2026-{month}",
                    gross_salary="10000.00",
                    use_previous_ytd_fica=True,
                    save_run=True,
                ),
                actor=owner_user,
            )
        march = service.list_runs(limit=1)[0]  # newest first
        assert march.pay_period == "2026-03"
        # After 3 months at $10k gross: YTD after = $30,000
        run = service.get_run(march.id)
        assert run.tax_detail.ytd_fica_after == D("30000.00")

    def test_does_not_cross_year_boundary(self, service, owner_user):
        """A run in 2027-01 should NOT pull YTD from a 2026 run."""
        service.calculate(
            PayrollInput(
                pay_period="2026-12",
                gross_salary="8000.00",
                save_run=True,
            ),
            actor=owner_user,
        )
        jan_2027 = service.calculate(
            PayrollInput(
                pay_period="2027-01",
                gross_salary="8000.00",
                use_previous_ytd_fica=True,
                save_run=False,
            ),
            actor=owner_user,
        )
        # Should start from 0, not carry over from 2026-12
        assert jan_2027.tax_detail.ytd_fica_after == D("8000.00")

    def test_overrides_manual_ytd_fica_wages(self, service, owner_user):
        """When use_previous_ytd_fica=True, the manual ytd_fica_wages is ignored."""
        service.calculate(
            PayrollInput(
                pay_period="2026-01",
                gross_salary="8000.00",
                save_run=True,
            ),
            actor=owner_user,
        )
        result = service.calculate(
            PayrollInput(
                pay_period="2026-02",
                gross_salary="8000.00",
                ytd_fica_wages="99999.00",  # should be ignored
                use_previous_ytd_fica=True,
                save_run=False,
            ),
            actor=owner_user,
        )
        # ytd_fica_after = previous ytd_fica_after (8000) + current gross (8000)
        assert result.tax_detail.ytd_fica_after == D("16000.00")


def _audit_count(session) -> int:
    """Rows in audit_log (append-only and shared: compare deltas, never assert empty)."""
    return session.exec(select(func.count()).select_from(AuditLog)).one()


class TestAuditTrail:
    def test_save_records_one_audit_row(self, service, base_input, session, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        rows = session.exec(select(AuditLog).where(AuditLog.action == "payroll_run.save")).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.entity == "payroll_run"
        assert row.entity_id == str(created.id)
        assert row.before is None
        assert row.after["pay_period"] == "2026-03"
        assert row.after["gross_salary"] == "8000.00"
        assert row.after["net_pay"] == str(created.tax_detail.net_pay)
        assert row.user_id == owner_user.id

    def test_unsaved_calculation_records_nothing(self, service, base_input, session, owner_user):
        base_input.save_run = False
        before = _audit_count(session)
        service.calculate(base_input, actor=owner_user)
        assert _audit_count(session) == before

    def test_delete_records_one_audit_row(self, service, base_input, session, owner_user):
        created = service.calculate(base_input, actor=owner_user)
        service.delete_run(created.id, actor=owner_user)
        rows = session.exec(select(AuditLog).where(AuditLog.action == "payroll_run.delete")).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.entity == "payroll_run"
        assert row.entity_id == str(created.id)
        assert row.after is None
        assert row.before["pay_period"] == "2026-03"
        assert row.before["gross_salary"] == "8000.00"
        assert row.user_id == owner_user.id


def _audit(session, action: str) -> list[AuditLog]:
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


class TestActorEnforcement:
    """The services re-check the role, independent of the routes (defense in depth)."""

    @pytest.fixture(params=["accountant_user", "viewer_user"])
    def reader(self, request):
        return request.getfixturevalue(request.param)

    def test_reader_cannot_save_a_run(self, service, base_input, session, reader):
        audit_before = _audit_count(session)
        with pytest.raises(PermissionDeniedError) as exc_info:
            service.calculate(base_input, actor=reader)
        assert exc_info.value.http_code == 403
        assert session.exec(select(PayrollRun)).all() == []
        assert _audit_count(session) == audit_before

    def test_reader_may_calculate_without_saving(self, service, base_input, session, reader):
        base_input.save_run = False
        result = service.calculate(base_input, actor=reader)
        assert result.id is None
        assert session.exec(select(PayrollRun)).all() == []

    def test_reader_cannot_delete_a_run(self, service, base_input, session, owner_user, reader):
        created = service.calculate(base_input, actor=owner_user)
        with pytest.raises(PermissionDeniedError):
            service.delete_run(created.id, actor=reader)
        assert session.get(PayrollRun, created.id) is not None
        assert _audit(session, "payroll_run.delete") == []

    def test_reader_cannot_probe_missing_runs(self, service, reader):
        # The role is checked before the lookup: no 404-vs-403 existence oracle.
        with pytest.raises(PermissionDeniedError):
            service.delete_run(99999, actor=reader)

    def test_reader_cannot_update_config(self, session, reader):
        config_service = PayrollConfigService(session)
        current = config_service.get_config()
        update = PayrollConfigUpdate.model_validate(
            {**current.model_dump(), "default_gross_salary": "1.00"}
        )
        with pytest.raises(PermissionDeniedError):
            config_service.update_config(update, actor=reader)
        assert config_service.get_config().default_gross_salary == current.default_gross_salary
        assert _audit(session, "payroll_config.update") == []

    def test_reader_cannot_increment_journal_no(self, session, reader):
        config_service = PayrollConfigService(session)
        before = config_service.get_config().next_journal_no
        with pytest.raises(PermissionDeniedError):
            config_service.increment_journal_no(2, actor=reader)
        assert config_service.get_config().next_journal_no == before
        assert _audit(session, "payroll_config.increment_journal_no") == []


class TestConfigAudit:
    def test_update_records_changed_fields_only(self, session, owner_user):
        config_service = PayrollConfigService(session)
        current = config_service.get_config()
        update = PayrollConfigUpdate.model_validate(
            {
                **current.model_dump(),
                "default_gross_salary": "9123.45",
                "acct_checking": "Checking (new)",
            }
        )
        config_service.update_config(update, actor=owner_user)

        (row,) = _audit(session, "payroll_config.update")
        assert row.user_id == owner_user.id
        assert row.entity == "payroll_config"
        assert row.entity_id == str(current.id)
        assert row.before == {
            "default_gross_salary": str(current.default_gross_salary),
            "acct_checking": current.acct_checking,
        }
        # Money is stored in the audit row as an exact JSON string.
        assert row.after == {"default_gross_salary": "9123.45", "acct_checking": "Checking (new)"}

    def test_increment_journal_no_is_audited(self, session, owner_user):
        config_service = PayrollConfigService(session)
        before = config_service.get_config().next_journal_no
        config_service.increment_journal_no(3, actor=owner_user)

        (row,) = _audit(session, "payroll_config.increment_journal_no")
        assert row.user_id == owner_user.id
        assert row.before == {"next_journal_no": before}
        assert row.after == {"next_journal_no": before + 3}
