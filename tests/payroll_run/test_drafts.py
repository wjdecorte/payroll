"""
Saved payroll runs are drafts: FUTA and Georgia SUTA are stored with the run, only a draft
can be deleted, and the year-to-date chain skips void runs.

Rows in other statuses are planted directly (the approve and void services come later).
"""

import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import select

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.payroll_run.exceptions import (
    PayrollRunAlreadyExistsError,
    PayrollRunStateError,
    PayrollSettingsMissingError,
)
from payroll.payroll_run.models import PayrollRun
from payroll.payroll_run.schemas import PayrollInput
from payroll.payroll_run.services import PayrollRunService
from payroll.tax.engine import calc_payroll
from payroll.tax.unemployment import UnemploymentRates, calc_unemployment
from tests.payroll_run.test_ledger_schema import _approved, _void

D = Decimal
PREFIX = "/payroll/api/v1"
RATES = UnemploymentRates(D("0.006"), D("7000.00"), D("0.0264"), D("9500.00"))
GOLDEN = json.loads((Path(__file__).parents[1] / "tax" / "golden_cases.json").read_text())

pytestmark = pytest.mark.usefixtures("year_settings")


@pytest.fixture
def service(session):
    return PayrollRunService(session)


def _payload(period="2026-03", gross="8000.00", ytd="0.00", **kw) -> PayrollInput:
    return PayrollInput(
        pay_period=period, gross_salary=gross, ytd_fica_wages=ytd, **{"save_run": True} | kw
    )


def _planted(session, status="imported", **kw) -> PayrollRun:
    kw.setdefault("gross_salary", D("8000.00"))
    kw.setdefault("fica_wages", D("8000.00"))
    row = PayrollRun(status=status, **kw)
    session.add(row)
    session.flush()
    return row


def _audit_count(session) -> int:
    return len(session.exec(select(AuditLog)).all())


# --------------------------------------------------------------------------- save: draft


def test_save_creates_a_draft_with_unemployment_from_stored_ytd(service, session, owner_user):
    result = service.calculate(_payload(ytd="5000.00"), actor=owner_user)

    run = session.get(PayrollRun, result.id)
    expected = calc_unemployment(run.ytd_fica_before, run.ytd_fica_after, RATES)
    assert run.status == "draft"
    assert (run.pay_date, run.entry_id, run.void_entry_id, run.void_date) == (None,) * 4
    assert (run.futa_wages, run.futa) == (expected.futa_wages, expected.futa)
    assert (run.suta_wages, run.suta) == (expected.suta_wages, expected.suta)
    assert run.suta_rate == D("0.0264")
    # 5,000 before, 13,000 after: 2,000 of FUTA wages and 4,500 of SUTA wages.
    assert (run.futa_wages, run.futa) == (D("2000.00"), D("12.00"))
    assert (run.suta_wages, run.suta) == (D("4500.00"), D("118.80"))


def test_result_reports_the_stored_state_and_unemployment(service, owner_user):
    saved = service.calculate(_payload(ytd="5000.00"), actor=owner_user)
    assert (saved.status, saved.pay_date, saved.entry_id) == ("draft", None, None)
    assert (saved.futa_wages, saved.futa) == (D("2000.00"), D("12.00"))
    assert (saved.suta_wages, saved.suta, saved.suta_rate) == (
        D("4500.00"),
        D("118.80"),
        D("0.0264"),
    )
    loaded = service.get_run(saved.id)  # rebuilt from the stored columns
    for field in ("status", "pay_date", "entry_id", "futa_wages", "futa", "suta_wages", "suta"):
        assert getattr(loaded, field) == getattr(saved, field), field
    assert loaded.suta_rate == saved.suta_rate


def test_summary_carries_status_and_unemployment(service, owner_user):
    service.calculate(_payload(ytd="5000.00"), actor=owner_user)
    (summary,) = service.list_runs()
    assert (summary.status, summary.pay_date, summary.entry_id) == ("draft", None, None)
    assert (summary.futa_wages, summary.futa) == (D("2000.00"), D("12.00"))
    assert (summary.suta_wages, summary.suta) == (D("4500.00"), D("118.80"))


def test_wages_over_the_base_attract_nothing(service, owner_user):
    result = service.calculate(_payload(ytd="20000.00"), actor=owner_user)
    assert (result.futa_wages, result.futa, result.suta_wages, result.suta) == (D("0.00"),) * 4


def test_unemployment_does_not_change_the_employee_side(service, session, owner_user):
    result = service.calculate(_payload(ytd="5000.00"), actor=owner_user)
    run = session.get(PayrollRun, result.id)
    calc = calc_payroll(
        gross_salary=D("8000.00"),
        health_insurance=D("0.00"),
        hsa_contribution=D("0.00"),
        health_in_income_tax=True,
        hsa_in_income_tax=False,
        ytd_fica_wages=D("5000.00"),
    )
    assert run.net_pay == calc.net_pay
    assert run.total_employer_taxes == calc.total_employer_taxes
    assert run.total_employer_cost == calc.total_employer_cost


def test_save_audit_row_names_the_status_and_unemployment(service, session, owner_user):
    created = service.calculate(_payload(ytd="5000.00"), actor=owner_user)
    (row,) = session.exec(select(AuditLog).where(AuditLog.action == "payroll_run.save")).all()
    assert row.entity_id == str(created.id)
    assert (row.after["status"], row.after["futa"], row.after["suta"]) == (
        "draft",
        "12.00",
        "118.80",
    )


def test_a_reader_cannot_save(service, session, viewer_user):
    before = _audit_count(session)
    with pytest.raises(PermissionDeniedError):
        service.calculate(_payload(), actor=viewer_user)
    assert session.exec(select(PayrollRun)).all() == []
    assert _audit_count(session) == before


# --------------------------------------------------------------------------- missing settings


def test_save_without_settings_is_refused_and_writes_nothing(
    service, session, owner_user, year_settings
):
    session.delete(year_settings)
    session.flush()
    before = _audit_count(session)

    with pytest.raises(PayrollSettingsMissingError) as exc:
        service.calculate(_payload(), actor=owner_user)

    assert exc.value.http_code == 422
    assert "Settings > Payroll" in exc.value.message
    assert "2026" in exc.value.message
    assert session.exec(select(PayrollRun)).all() == []
    assert _audit_count(session) == before


def test_save_without_settings_is_a_422_over_the_api(
    owner_client, session, year_settings, payroll_input
):
    session.delete(year_settings)
    session.flush()
    response = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input)
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["type"] == "PayrollSettingsMissingError"
    assert "Settings > Payroll" in error["message"]


@pytest.fixture(params=["owner_user", "accountant_user", "viewer_user"])
def any_role(request):
    return request.getfixturevalue(request.param)


def test_unsaved_calculation_without_settings_works_and_reports_none(
    service, session, year_settings, any_role
):
    session.delete(year_settings)
    session.flush()
    result = service.calculate(_payload(save_run=False), actor=any_role)
    assert result.saved is False
    assert result.tax_detail.net_pay > 0
    assert result.status is None
    assert (result.futa_wages, result.futa, result.suta_wages, result.suta) == (None,) * 4
    assert result.suta_rate is None
    assert session.exec(select(PayrollRun)).all() == []


def test_unsaved_calculation_with_settings_reports_unemployment_and_saves_nothing(
    service, session, any_role
):
    result = service.calculate(_payload(ytd="5000.00", save_run=False), actor=any_role)
    assert result.saved is False
    assert (result.futa_wages, result.futa) == (D("2000.00"), D("12.00"))
    assert (result.suta_wages, result.suta, result.suta_rate) == (
        D("4500.00"),
        D("118.80"),
        D("0.0264"),
    )
    assert result.status is None and result.id is None
    assert session.exec(select(PayrollRun)).all() == []


# --------------------------------------------------------------------------- figures unchanged

_GOLDEN_SAMPLE = [c for c in GOLDEN if c["inputs"]["gross_salary"] > 0][::37]


@pytest.mark.parametrize("case", _GOLDEN_SAMPLE, ids=lambda c: json.dumps(c["inputs"]))
def test_saved_draft_figures_match_the_engine_and_the_golden_snapshot(
    service, session, owner_user, case
):
    inputs = {k: v if isinstance(v, bool) else D(str(v)) for k, v in case["inputs"].items()}
    result = service.calculate(
        PayrollInput(
            pay_period="2026-04",
            save_run=True,
            **{k: v if isinstance(v, bool) else str(v) for k, v in inputs.items()},
        ),
        actor=owner_user,
    )
    run = session.get(PayrollRun, result.id)
    engine = asdict(calc_payroll(**inputs))

    compared = 0
    for field, expected in case["expected"].items():
        if not hasattr(run, field) or isinstance(expected, bool):
            continue
        compared += 1
        assert getattr(run, field) == engine[field], field  # exactly the engine's figure
        assert abs(getattr(run, field) - D(str(expected))) <= D("0.01"), field  # the snapshot
    assert compared >= 15


# QuickBooks entries for a saved run, captured from the code before FUTA / SUTA existed
# (account | memo | debit | credit).
_CHK = "Business Checking"
_FED = "Payroll Liabilities:Federal Taxes (941/943/944)"
_GA = "Payroll Liabilities:GA Income Tax"
_FULL_RUN = [
    (
        "Entry 1: Record Monthly Payroll — Wages & Withholdings",
        [
            "Payroll Expenses:Wages|Gross salary $8,000.00 + medical insurance W-2 addback $500.00"
            " + HSA contribution $300.00|8800.00|0.00",
            f"{_FED}|Federal income tax withheld|0.00|1134.17",
            f"{_GA}|Georgia income tax withheld|0.00|389.25",
            f"{_FED}|Employee Social Security 6.2% on $8,000.00|0.00|496.00",
            f"{_FED}|Employee Medicare 1.45%|0.00|116.00",
            "Payroll Liabilities:S-Corp Health Insurance|Medical insurance premium - clear when"
            " paid to insurer|0.00|500.00",
            "Payroll Liabilities:Co. HSA|HSA contribution payable - clear when transferred to"
            " HSA|0.00|300.00",
            f"{_CHK}|Net paycheck to officer|0.00|5864.58",
        ],
    ),
    (
        "Entry 2: Employer Payroll Taxes",
        [
            "Payroll Expenses:Taxes|Employer Social Security + Medicare|612.00|0.00",
            f"{_FED}|Employer Social Security 6.2%|0.00|496.00",
            f"{_FED}|Employer Medicare 1.45%|0.00|116.00",
        ],
    ),
    (
        "Entry 3: Pay Medical Insurance Premium to Insurer",
        [
            "Payroll Liabilities:S-Corp Health Insurance|Clear medical insurance payable|500.00|0.00",
            f"{_CHK}|Payment to insurance company|0.00|500.00",
        ],
    ),
    (
        "Entry 4: HSA Employer Contribution",
        [
            "Payroll Liabilities:Co. HSA|Clear HSA contribution payable|300.00|0.00",
            f"{_CHK}|Transfer to HSA account|0.00|300.00",
        ],
    ),
    (
        "Entry 5: Federal Tax Payment (EFTPS)",
        [
            f"{_FED}|Federal income tax withheld|1134.17|0.00",
            f"{_FED}|Employee Social Security 6.2%|496.00|0.00",
            f"{_FED}|Employer Social Security 6.2%|496.00|0.00",
            f"{_FED}|Employee Medicare 1.45%|116.00|0.00",
            f"{_FED}|Employer Medicare 1.45%|116.00|0.00",
            f"{_CHK}|EFTPS federal tax deposit|0.00|2358.17",
        ],
    ),
    (
        "Entry 6: Georgia Tax Payment",
        [
            f"{_GA}|Georgia income tax withheld|389.25|0.00",
            f"{_CHK}|Payment to Georgia Department of Revenue|0.00|389.25",
        ],
    ),
]
_CAPPED_RUN = [
    (
        "Entry 1: Record Monthly Payroll — Wages & Withholdings",
        [
            "Payroll Expenses:Wages|Gross salary $6,000.00|6000.00|0.00",
            f"{_FED}|Federal income tax withheld|0.00|584.17",
            f"{_GA}|Georgia income tax withheld|0.00|259.50",
            f"{_FED}|Employee Social Security 6.2% on $4,500.00|0.00|279.00",
            f"{_FED}|Employee Medicare 1.45%|0.00|87.00",
            f"{_CHK}|Net paycheck to officer|0.00|4790.33",
        ],
    ),
    (
        "Entry 2: Employer Payroll Taxes",
        [
            "Payroll Expenses:Taxes|Employer Social Security + Medicare|366.00|0.00",
            f"{_FED}|Employer Social Security 6.2%|0.00|279.00",
            f"{_FED}|Employer Medicare 1.45%|0.00|87.00",
        ],
    ),
    (
        "Entry 3: Federal Tax Payment (EFTPS)",
        [
            f"{_FED}|Federal income tax withheld|584.17|0.00",
            f"{_FED}|Employee Social Security 6.2%|279.00|0.00",
            f"{_FED}|Employer Social Security 6.2%|279.00|0.00",
            f"{_FED}|Employee Medicare 1.45%|87.00|0.00",
            f"{_FED}|Employer Medicare 1.45%|87.00|0.00",
            f"{_CHK}|EFTPS federal tax deposit|0.00|1316.17",
        ],
    ),
    (
        "Entry 4: Georgia Tax Payment",
        [
            f"{_GA}|Georgia income tax withheld|259.50|0.00",
            f"{_CHK}|Payment to Georgia Department of Revenue|0.00|259.50",
        ],
    ),
]


def _entry_text(result) -> list[tuple[str, list[str]]]:
    return [
        (e.title, [f"{ln.account}|{ln.memo}|{ln.debit}|{ln.credit}" for ln in e.lines])
        for e in result.journal_entries
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            _payload(
                gross="8000.00", health_insurance="500.00", hsa_contribution="300.00", ytd="0.00"
            ),
            _FULL_RUN,
        ),
        (_payload(gross="6000.00", ytd="180000.00"), _CAPPED_RUN),
    ],
    ids=["full", "ss-capped"],
)
def test_quickbooks_entries_for_a_saved_run_are_unchanged(service, owner_user, payload, expected):
    saved = service.calculate(payload, actor=owner_user)
    assert _entry_text(saved) == expected
    assert _entry_text(service.get_run(saved.id)) == expected


# --------------------------------------------------------------------------- one run per period


def test_second_saved_run_for_a_period_is_refused(service, owner_user):
    first = service.calculate(_payload(), actor=owner_user)
    with pytest.raises(PayrollRunAlreadyExistsError) as exc:
        service.calculate(_payload(), actor=owner_user)
    assert f"already exists (id={first.id}). Delete it first" in exc.value.message
    assert exc.value.http_code == 409


@pytest.mark.parametrize("status", ["imported", "approved"])
def test_second_saved_run_is_refused_for_any_non_void_run(
    service, session, owner_user, entries, status
):
    if status == "approved":
        _approved(session, entries, pay_period="2026-03")
    else:
        _planted(session, pay_period="2026-03")
    with pytest.raises(PayrollRunAlreadyExistsError):
        service.calculate(_payload(), actor=owner_user)


def test_a_void_run_frees_its_period(service, session, owner_user, entries):
    voided = _void(session, entries, pay_period="2026-03")
    result = service.calculate(_payload(), actor=owner_user)
    assert result.id != voided.id and result.status == "draft"


def test_a_lost_race_for_the_period_is_a_typed_conflict_not_a_raw_integrity_error(
    service, session, owner_user, monkeypatch
):
    service.calculate(_payload(), actor=owner_user)
    # Simulate the other save committing after our existence check.
    monkeypatch.setattr(service, "_find_by_period", lambda period: None)
    with pytest.raises(PayrollRunAlreadyExistsError) as exc:
        service.calculate(_payload(), actor=owner_user)
    assert exc.value.http_code == 409


# --------------------------------------------------------------------------- year-to-date chain


def test_ytd_chain_ignores_void_runs(service, session, owner_user, entries):
    _planted(session, pay_period="2026-01", ytd_fica_after=D("8000.00"))
    _void(session, entries, pay_period="2026-02", ytd_fica_after=D("99000.00"))
    result = service.calculate(
        _payload(period="2026-03", use_previous_ytd_fica=True, save_run=False), actor=owner_user
    )
    assert result.tax_detail.ytd_fica_before == D("8000.00")


@pytest.mark.parametrize("status", ["imported", "draft", "approved"])
def test_ytd_chain_includes_imported_draft_and_approved_runs(
    service, session, owner_user, entries, status
):
    if status == "approved":
        _approved(session, entries, pay_period="2026-02", ytd_fica_after=D("16000.00"))
    else:
        _planted(session, status, pay_period="2026-02", ytd_fica_after=D("16000.00"))
    result = service.calculate(
        _payload(period="2026-03", use_previous_ytd_fica=True, save_run=False), actor=owner_user
    )
    assert result.tax_detail.ytd_fica_before == D("16000.00")


def test_ytd_chain_is_zero_when_only_void_runs_precede(service, session, owner_user, entries):
    _void(session, entries, pay_period="2026-02", ytd_fica_after=D("16000.00"))
    result = service.calculate(
        _payload(period="2026-03", use_previous_ytd_fica=True, save_run=False), actor=owner_user
    )
    assert result.tax_detail.ytd_fica_before == D("0.00")


# --------------------------------------------------------------------------- delete rule


def test_a_draft_can_be_deleted(service, session, owner_user):
    created = service.calculate(_payload(), actor=owner_user)
    service.delete_run(created.id, actor=owner_user)
    assert session.get(PayrollRun, created.id) is None


@pytest.fixture
def frozen_run(request, session, entries):
    if request.param == "imported":
        run = _planted(session, pay_period="2026-02")
    elif request.param == "approved":
        run = _approved(session, entries, pay_period="2026-02")
    else:
        run = _void(session, entries, pay_period="2026-02")
    session.commit()  # a refused delete rolls back; the planted row must outlive that
    return run


@pytest.mark.parametrize("frozen_run", ["imported", "approved", "void"], indirect=True)
def test_a_run_that_is_not_a_draft_cannot_be_deleted(service, session, owner_user, frozen_run):
    status = frozen_run.status
    before = _audit_count(session)
    with pytest.raises(PayrollRunStateError) as exc:
        service.delete_run(frozen_run.id, actor=owner_user)
    assert exc.value.http_code == 409
    assert f"is {status}" in exc.value.message
    assert session.get(PayrollRun, frozen_run.id) is not None
    assert _audit_count(session) == before


def test_delete_over_the_api_is_204_for_a_draft_and_409_otherwise(
    owner_client, session, payroll_input
):
    created = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
    assert created["status"] == "draft"
    assert owner_client.delete(f"{PREFIX}/payroll/runs/{created['id']}").status_code == 204

    kept = _planted(session, pay_period="2026-02")
    session.commit()  # a refused delete rolls back; the planted row must outlive that
    response = owner_client.delete(f"{PREFIX}/payroll/runs/{kept.id}")
    assert response.status_code == 409
    (error,) = response.json()["errors"]
    assert error["type"] == "PayrollRunStateError"
    assert "is imported" in error["message"]
    assert session.get(PayrollRun, kept.id) is not None


def test_run_dates_are_json_dates_on_a_planted_approved_run(owner_client, session, entries):
    run = _approved(session, entries, pay_period="2026-02", pay_date=date(2026, 2, 28))
    data = owner_client.get(f"{PREFIX}/payroll/runs/{run.id}").json()
    assert (data["status"], data["payDate"], data["entryId"]) == (
        "approved",
        "2026-02-28",
        run.entry_id,
    )


def test_openapi_documents_the_new_run_fields(client):
    schemas = client.app.openapi()["components"]["schemas"]
    lifecycle = {"status", "payDate", "entryId", "futaWages", "futa", "sutaWages", "suta"}
    assert lifecycle <= set(schemas["PayrollResult"]["properties"])
    assert lifecycle <= set(schemas["PayrollRunSummary"]["properties"])
    assert "sutaRate" in schemas["PayrollResult"]["properties"]


def test_calculate_response_carries_money_strings_for_futa_and_suta(owner_client, payroll_input):
    payroll_input["ytdFicaWages"] = "5000.00"
    data = owner_client.post(f"{PREFIX}/payroll/runs/calculate", json=payroll_input).json()
    assert (data["status"], data["payDate"], data["entryId"]) == ("draft", None, None)
    assert (data["futaWages"], data["futa"]) == ("2000.00", "12.00")
    assert (data["sutaWages"], data["suta"]) == ("4500.00", "118.80")
    (summary,) = owner_client.get(f"{PREFIX}/payroll/runs").json()
    assert (summary["status"], summary["futa"], summary["suta"]) == ("draft", "12.00", "118.80")
