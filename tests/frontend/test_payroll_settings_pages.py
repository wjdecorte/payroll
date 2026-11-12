"""Settings > Payroll: the year's FUTA/SUTA rates and wage bases, and the deposit rules."""

# ruff: noqa: F811  (fixtures re-exported below are used as arguments)

from decimal import Decimal

import pytest
from sqlmodel import select

from payroll.payroll_run.ledger_models import PayrollDepositRules, PayrollSettings
from payroll.reports.periods import company_today
from tests.frontend.test_csp_compat import _assert_csp_safe

D = Decimal


@pytest.fixture(autouse=True)
def _committed(session, owner_user):
    session.commit()


def year_form(**over):
    data = {
        "tax_year": "2027",
        "futa_rate": "0.6",
        "futa_wage_base": "7000.00",
        "suta_rate": "2.64",
        "suta_wage_base": "9,500.00",
    }
    data.update(over)
    return data


def rules_form(**over):
    data = {
        "irs_941_cadence": "monthly",
        "irs_941_due_day": "15",
        "ga_dor_cadence": "monthly",
        "ga_dor_due_day": "15",
        "futa_cadence": "quarterly",
        "futa_due_day": "0",
        "ga_dol_cadence": "quarterly",
        "ga_dol_due_day": "0",
    }
    data.update(over)
    return data


def test_page_defaults_to_the_current_tax_year(owner_client, year_settings, session):
    session.commit()
    page = owner_client.get(f"/settings/payroll?tax_year={year_settings.tax_year}")
    assert page.status_code == 200
    _assert_csp_safe(page.text)
    assert 'name="futa_rate" ' in page.text and 'value="0.6"' in page.text
    assert 'value="2.64"' in page.text and 'value="7000.00"' in page.text
    assert 'value="9500.00"' in page.text
    assert "%" in page.text
    default = owner_client.get("/settings/payroll")
    assert f'value="{company_today().year}"' in default.text


def test_save_year_converts_percent_to_fraction_and_shows_saved(owner_client, session):
    response = owner_client.post("/settings/payroll/year", data=year_form())
    assert response.status_code == 200
    row = session.exec(select(PayrollSettings).where(PayrollSettings.tax_year == 2027)).one()
    assert (row.futa_rate, row.suta_rate) == (D("0.006"), D("0.0264"))
    assert (row.futa_wage_base, row.suta_wage_base) == (D("7000.00"), D("9500.00"))
    assert 'role="status"' in response.text
    assert 'value="0.6"' in response.text and 'value="2.64"' in response.text
    again = owner_client.get("/settings/payroll?tax_year=2027")
    assert 'value="0.6"' in again.text and 'value="9500.00"' in again.text
    # saving again replaces the year's values
    owner_client.post("/settings/payroll/year", data=year_form(futa_rate="0.5"))
    session.expire_all()
    row = session.exec(select(PayrollSettings).where(PayrollSettings.tax_year == 2027)).one()
    assert row.futa_rate == D("0.005")


@pytest.mark.parametrize(
    "over",
    [
        {"tax_year": "abc"},
        {"tax_year": ""},
        {"tax_year": "1999"},
        {"tax_year": "2101"},
        {"tax_year": "99999999999"},
        {"futa_rate": ""},
        {"futa_rate": "abc"},
        {"futa_rate": "NaN"},
        {"futa_rate": "1E+30"},
        {"futa_rate": "-1"},
        {"futa_rate": "20.01"},
        {"futa_rate": "0.00001"},  # finer than the six places of the stored fraction
        {"suta_rate": "2.640001"},
        {"suta_rate": "99999999999999999999999999999999"},
        {"futa_wage_base": ""},
        {"futa_wage_base": "0"},
        {"futa_wage_base": "7000.123"},
        {"suta_wage_base": "1E+30"},
        {"suta_wage_base": "1000000.01"},
        {"suta_wage_base": "-5"},
    ],
)
def test_bad_year_values_are_422_and_keep_typed_values(owner_client, session, over):
    session.commit()  # the sign-in session must survive a refused service call's rollback
    response = owner_client.post("/settings/payroll/year", data=year_form(**over))
    assert response.status_code == 422, over
    assert 'role="alert"' in response.text
    assert session.exec(select(PayrollSettings)).first() is None
    for value in over.values():
        if value and value.isascii() and value.isprintable() and "&" not in value:
            assert f'value="{value}"' in response.text, over


def test_accepts_the_finest_percentage(owner_client, session):
    response = owner_client.post("/settings/payroll/year", data=year_form(futa_rate="0.0001"))
    assert response.status_code == 200
    row = session.exec(select(PayrollSettings)).one()
    assert row.futa_rate == D("0.000001")
    assert (
        owner_client.post(
            "/settings/payroll/year", data=year_form(tax_year="2028", futa_rate="0.60%")
        ).status_code
        == 200
    )


def test_save_deposit_rules_and_show_them(owner_client, session):
    page = owner_client.get("/settings/payroll")
    assert "last day of the month" in page.text
    for agency in ("irs_941", "ga_dor", "futa", "ga_dol"):
        assert f'name="{agency}_cadence"' in page.text
        assert f'name="{agency}_due_day"' in page.text
    response = owner_client.post(
        "/settings/payroll/deposit-rules",
        data=rules_form(irs_941_cadence="quarterly", irs_941_due_day="20", ga_dol_due_day="31"),
    )
    assert response.status_code == 200 and 'role="status"' in response.text
    session.expire_all()
    rules = session.get(PayrollDepositRules, 1)
    assert (rules.irs_941_cadence, rules.irs_941_due_day, rules.ga_dol_due_day) == (
        "quarterly",
        20,
        31,
    )
    shown = owner_client.get("/settings/payroll").text
    assert 'name="irs_941_due_day"' in shown and 'value="20"' in shown


@pytest.mark.parametrize(
    "over",
    [
        {"irs_941_cadence": "weekly"},
        {"irs_941_cadence": ""},
        {"ga_dor_due_day": "32"},
        {"ga_dor_due_day": "-1"},
        {"ga_dor_due_day": ""},
        {"futa_due_day": "abc"},
        {"futa_due_day": "1.5"},
        {"ga_dol_due_day": "99999999999"},
    ],
)
def test_bad_rules_are_422_and_nothing_changes(owner_client, session, over):
    session.commit()  # the sign-in session must survive a refused service call's rollback
    response = owner_client.post("/settings/payroll/deposit-rules", data=rules_form(**over))
    assert response.status_code == 422, over
    assert 'role="alert"' in response.text
    session.expire_all()
    rules = session.get(PayrollDepositRules, 1)
    assert rules is None or (rules.irs_941_due_day, rules.ga_dor_due_day) == (15, 15)
    value = next(iter(over.values()))
    if value and "cadence" not in next(iter(over)):  # a select cannot show an unknown choice
        assert f'value="{value}"' in response.text or f'value="{value}" selected' in response.text


def test_missing_rules_field_is_422(owner_client):
    data = rules_form()
    del data["futa_cadence"]
    assert owner_client.post("/settings/payroll/deposit-rules", data=data).status_code == 422


@pytest.mark.parametrize("role_client", ["viewer_client", "accountant_client"])
def test_non_owners_read_but_cannot_write(request, role_client, session):
    client = request.getfixturevalue(role_client)
    page = client.get("/settings/payroll")
    assert page.status_code == 200
    assert "<form" not in page.text.split("</nav>")[-1] or "csrf_token" in page.text
    assert 'action="/settings/payroll/year"' not in page.text
    assert 'action="/settings/payroll/deposit-rules"' not in page.text
    assert client.post("/settings/payroll/year", data=year_form()).status_code == 403
    assert client.post("/settings/payroll/deposit-rules", data=rules_form()).status_code == 403
    assert session.exec(select(PayrollSettings)).first() is None


def test_settings_navigation(owner_client):
    ledger = owner_client.get("/settings/ledger")
    assert 'href="/settings/payroll"' in ledger.text
    assert 'href="/settings/ledger"' in owner_client.get("/settings/payroll").text
