"""
Regenerate tests/tax/golden_cases.json from the current ``calc_payroll``.

Run: ``uv run python tests/tax/make_golden.py``

The snapshot captures every PayrollCalculation field for a grid of inputs so
that later refactors (e.g. float -> Decimal) can be compared against it.
Floats are serialized with ``repr`` precision (json default) and booleans stay
booleans.
Integers stay integers in the JSON (e.g. gross_salary 0, 1, 5000); Task 3 must
convert inputs with Decimal(str(v)), never Decimal(float).

The committed snapshot is the float engine's output and is the baseline the
Decimal engine is compared against. Regenerating it now would replace that
baseline with Decimal output (serialized as floats), so only do so deliberately.
"""

import itertools
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from payroll.tax.engine import calc_payroll

GROSS = [0, 1, 1234.56, 5000, 8000, 16666.67, 30000, 55000]
HEALTH = [0, 500]
HSA = [0, 300]
TOGGLES = [True, False]
YTD_FICA = [0, 100_000, 184_000, 184_500, 250_000]

OUT = Path(__file__).parent / "golden_cases.json"


def build_inputs() -> list[dict]:
    """The input grid, as JSON-native values (ints, floats, bools)."""
    return [
        {
            "gross_salary": gross,
            "health_insurance": health,
            "hsa_contribution": hsa,
            "health_in_income_tax": h_tax,
            "hsa_in_income_tax": hsa_tax,
            "ytd_fica_wages": ytd,
        }
        for gross, health, hsa, h_tax, hsa_tax, ytd in itertools.product(
            GROSS, HEALTH, HSA, TOGGLES, TOGGLES, YTD_FICA
        )
    ]


def _json_value(value):
    return float(value) if isinstance(value, Decimal) else value


def build_cases() -> list[dict]:
    cases = []
    for inputs in build_inputs():
        engine_inputs = {
            k: v if isinstance(v, bool) else Decimal(str(v)) for k, v in inputs.items()
        }
        expected = {k: _json_value(v) for k, v in asdict(calc_payroll(**engine_inputs)).items()}
        cases.append({"inputs": inputs, "expected": expected})
    return cases


if __name__ == "__main__":
    cases = build_cases()
    # One compact case per line keeps the file small (pre-commit size limit).
    body = ",\n".join(json.dumps(c, separators=(",", ":")) for c in cases)
    OUT.write_text("[\n" + body + "\n]\n")
    print(f"wrote {len(cases)} cases to {OUT}")
