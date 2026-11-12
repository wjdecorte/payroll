from decimal import Decimal

import pytest
from sqlmodel import delete, select

from payroll.audit.models import AuditLog
from payroll.common.exceptions import PermissionDeniedError
from payroll.payroll_run.exceptions import InvalidPayrollSettingsError, PayrollSettingsMissingError
from payroll.payroll_run.ledger_models import PayrollDepositRules, PayrollSettings
from payroll.payroll_run.settings_service import PayrollSettingsService
from payroll.tax.unemployment import UnemploymentRates

D = Decimal


def year_args(**over):
    args = {
        "tax_year": 2026,
        "futa_rate": D("0.006"),
        "futa_wage_base": D("7000.00"),
        "suta_rate": D("0.027"),
        "suta_wage_base": D("9500.00"),
    }
    return {**args, **over}


@pytest.fixture
def service(session):
    return PayrollSettingsService(session)


def test_deposit_rules_default_row(service, session):
    session.exec(delete(PayrollDepositRules))
    rules = service.deposit_rules()
    assert (rules.irs_941_cadence, rules.irs_941_due_day) == ("monthly", 15)
    assert (rules.ga_dor_cadence, rules.ga_dor_due_day) == ("monthly", 15)
    assert (rules.futa_cadence, rules.futa_due_day) == ("quarterly", 0)
    assert (rules.ga_dol_cadence, rules.ga_dol_due_day) == ("quarterly", 0)
    assert service.deposit_rules().id == 1


def test_set_year_creates_then_updates_one_row(service, session, owner_user):
    assert service.get_year(2026) is None
    first = service.set_year(actor=owner_user, **year_args())
    again = service.set_year(actor=owner_user, **year_args(suta_rate=D("0.0305")))
    assert first.id == again.id
    rows = session.exec(select(PayrollSettings).where(PayrollSettings.tax_year == 2026)).all()
    assert len(rows) == 1
    assert rows[0].suta_rate == D("0.030500")
    assert service.get_year(2026).id == first.id


def test_rates_returns_unemployment_rates(service, owner_user):
    service.set_year(actor=owner_user, **year_args())
    assert service.rates(2026) == UnemploymentRates(
        futa_rate=D("0.006"),
        futa_wage_base=D("7000.00"),
        suta_rate=D("0.027"),
        suta_wage_base=D("9500.00"),
    )


def test_require_year_missing(service):
    with pytest.raises(PayrollSettingsMissingError) as exc:
        service.require_year(2031)
    assert "Settings > Payroll" in exc.value.message
    with pytest.raises(PayrollSettingsMissingError):
        service.rates(2031)


def test_set_year_audited(service, session, owner_user):
    service.set_year(actor=owner_user, **year_args())
    service.set_year(actor=owner_user, **year_args(futa_rate=D("0.01")))
    logs = session.exec(
        select(AuditLog).where(AuditLog.action == "payroll.settings_year").order_by(AuditLog.id)
    ).all()
    assert len(logs) == 2
    assert logs[0].before is None
    assert logs[0].after["futa_rate"] == "0.006000"
    assert logs[1].before["futa_rate"] == "0.006000"
    assert logs[1].after["futa_rate"] == "0.010000"
    assert logs[1].user_id == owner_user.id and logs[1].entity_id == "2026"


@pytest.mark.parametrize("field", ["futa_rate", "suta_rate"])
@pytest.mark.parametrize(
    "bad",
    [
        D("-0.000001"),
        D("0.200001"),
        D("0.0000001"),
        D("NaN"),
        D("Infinity"),
        D("1E+30"),
        0.01,
        "0.01",
        1,
        True,
        None,
    ],
)
def test_bad_rates_refused(service, owner_user, field, bad):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_year(actor=owner_user, **year_args(**{field: bad}))


@pytest.mark.parametrize("good", [D("0"), D("0.2"), D("0.123456")])
def test_rate_bounds_accepted(service, owner_user, good):
    row = service.set_year(actor=owner_user, **year_args(futa_rate=good))
    assert row.futa_rate == good


@pytest.mark.parametrize("field", ["futa_wage_base", "suta_wage_base"])
@pytest.mark.parametrize(
    "bad",
    [
        D("0"),
        D("-1"),
        D("1000000.01"),
        D("7000.001"),
        D("NaN"),
        D("Infinity"),
        D("1E+30"),
        7000,
        7000.0,
        True,
        None,
    ],
)
def test_bad_wage_bases_refused(service, owner_user, field, bad):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_year(actor=owner_user, **year_args(**{field: bad}))


def test_wage_base_upper_bound_accepted(service, owner_user):
    row = service.set_year(actor=owner_user, **year_args(futa_wage_base=D("1000000")))
    assert row.futa_wage_base == D("1000000.00")


@pytest.mark.parametrize("bad", [True, None, "2026", 2026.0, 1999, 2101])
def test_bad_tax_year_refused(service, owner_user, bad):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_year(actor=owner_user, **year_args(tax_year=bad))


def test_refused_set_year_writes_nothing(service, session, owner_user):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_year(actor=owner_user, **year_args(futa_rate=D("2")))
    assert service.get_year(2026) is None
    assert not session.exec(
        select(AuditLog).where(AuditLog.action == "payroll.settings_year")
    ).all()


def test_set_deposit_rules_updates_and_audits(service, session, owner_user):
    rules = service.set_deposit_rules(
        actor=owner_user,
        irs_941_cadence="quarterly",
        irs_941_due_day=31,
        futa_due_day=10,
    )
    assert (rules.irs_941_cadence, rules.irs_941_due_day) == ("quarterly", 31)
    assert rules.futa_due_day == 10
    assert (rules.ga_dor_cadence, rules.ga_dor_due_day) == ("monthly", 15)  # untouched
    log = session.exec(select(AuditLog).where(AuditLog.action == "payroll.deposit_rules")).one()
    assert log.before["irs_941_cadence"] == "monthly"
    assert log.after["irs_941_cadence"] == "quarterly"
    assert log.entity_id == "1"


def test_set_deposit_rules_day_zero_is_end_of_month(service, owner_user):
    rules = service.set_deposit_rules(actor=owner_user, ga_dor_due_day=0)
    assert rules.ga_dor_due_day == 0


@pytest.mark.parametrize("bad", ["weekly", "", "Monthly", None, 1])
def test_bad_cadence_refused(service, owner_user, bad):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_deposit_rules(actor=owner_user, ga_dol_cadence=bad)


@pytest.mark.parametrize("bad", [-1, 32, True, None, "15", 15.0])
def test_bad_due_day_refused(service, owner_user, bad):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_deposit_rules(actor=owner_user, ga_dor_due_day=bad)


def test_unknown_or_empty_deposit_fields_refused(service, owner_user):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_deposit_rules(actor=owner_user, bogus=1)
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_deposit_rules(actor=owner_user)


def test_bad_deposit_rules_change_nothing(service, owner_user):
    with pytest.raises(InvalidPayrollSettingsError):
        service.set_deposit_rules(actor=owner_user, irs_941_due_day=20, futa_due_day=99)
    assert service.deposit_rules().irs_941_due_day == 15


@pytest.mark.parametrize("role", ["viewer_user", "accountant_user"])
def test_non_owner_refused(service, session, request, role):
    actor = request.getfixturevalue(role)
    with pytest.raises(PermissionDeniedError):
        service.set_year(actor=actor, **year_args())
    with pytest.raises(PermissionDeniedError):
        service.set_deposit_rules(actor=actor, irs_941_due_day=20)
    assert service.get_year(2026) is None
    assert service.deposit_rules().irs_941_due_day == 15
