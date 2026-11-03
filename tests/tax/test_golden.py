"""Characterization test: engine output must match the golden snapshot.

The snapshot was captured from the original float engine. The Decimal engine
must match every amount to within one cent; the only expected differences are
half-cent ties, where Python's float ``round()`` and ``round_money`` (half-up)
can disagree.
"""

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from payroll.tax.engine import calc_payroll
from tests.tax.make_golden import build_inputs

GOLDEN = json.loads((Path(__file__).parent / "golden_cases.json").read_text())

TOLERANCE = Decimal("0.01")


def _to_decimal(value: int | float) -> Decimal:
    # Never Decimal(float): that captures binary noise (e.g. 1234.56 -> 1234.5599...).
    return Decimal(str(value))


def _decimal_inputs(inputs: dict) -> dict:
    return {k: v if isinstance(v, bool) else _to_decimal(v) for k, v in inputs.items()}


def _differences(case: dict) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """Return (field, expected, actual, diff) for every non-bool field that differs."""
    actual = asdict(calc_payroll(**_decimal_inputs(case["inputs"])))
    diffs = []
    for field, expected in case["expected"].items():
        if isinstance(expected, bool):
            continue
        exp = _to_decimal(expected)
        diff = abs(actual[field] - exp)
        if diff > 0:
            diffs.append((field, exp, actual[field], diff))
    return diffs


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: json.dumps(c["inputs"]))
def test_engine_matches_golden(case):
    actual = asdict(calc_payroll(**_decimal_inputs(case["inputs"])))
    assert actual.keys() == case["expected"].keys()
    for field, expected in case["expected"].items():
        if isinstance(expected, bool):
            assert actual[field] is expected, field
        else:
            assert isinstance(actual[field], Decimal), field
            assert abs(actual[field] - _to_decimal(expected)) <= TOLERANCE, field


def test_report_golden_differences(capsys):
    """Report (never fail on) every field that differs from the float snapshot at all.

    Run with ``-s`` to see the listing; a reviewer should confirm each is a
    half-cent tie or float noise, not a tax-rule change.
    """
    lines = []
    for case in GOLDEN:
        for field, exp, act, diff in _differences(case):
            lines.append(
                f"{json.dumps(case['inputs'])} {field}: expected={exp} actual={act} diff={diff}"
            )
    with capsys.disabled():
        print(f"\n[golden] {len(lines)} field(s) differ from the float snapshot")
        for line in lines:
            print(f"[golden] {line}")


def test_golden_file_has_full_grid():
    assert len(GOLDEN) == 640

    def key(inputs):
        return json.dumps(inputs, sort_keys=True)

    assert {key(c["inputs"]) for c in GOLDEN} == {key(i) for i in build_inputs()}


def test_make_golden_refuses_to_overwrite_without_force():
    import subprocess
    import sys

    golden = Path(__file__).parent / "golden_cases.json"
    before = golden.read_bytes()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "make_golden.py")],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    assert proc.returncode != 0
    assert "--force" in proc.stderr
    assert golden.read_bytes() == before
