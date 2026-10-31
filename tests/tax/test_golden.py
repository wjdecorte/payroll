"""Characterization test: engine output must match the golden snapshot."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from payroll.tax.engine import calc_payroll

GOLDEN = json.loads((Path(__file__).parent / "golden_cases.json").read_text())


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: json.dumps(c["inputs"]))
def test_engine_matches_golden(case):
    actual = asdict(calc_payroll(**case["inputs"]))
    assert actual.keys() == case["expected"].keys()
    for field, expected in case["expected"].items():
        if isinstance(expected, bool):
            assert actual[field] is expected, field
        else:
            assert actual[field] == pytest.approx(expected, abs=0.005), field
