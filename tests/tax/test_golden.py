"""Characterization test: engine output must match the golden snapshot."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from payroll.tax.engine import calc_payroll
from tests.tax.make_golden import build_cases

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


def test_golden_file_has_full_grid():
    assert len(GOLDEN) == 640

    def key(inputs):
        return json.dumps(inputs, sort_keys=True)

    assert {key(c["inputs"]) for c in GOLDEN} == {key(c["inputs"]) for c in build_cases()}
