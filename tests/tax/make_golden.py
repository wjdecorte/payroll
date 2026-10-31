"""
Regenerate tests/tax/golden_cases.json from the current ``calc_payroll``.

Run: ``uv run python tests/tax/make_golden.py``

The snapshot captures every PayrollCalculation field for a grid of inputs so
that later refactors (e.g. float -> Decimal) can be compared against it.
Floats are serialized with ``repr`` precision (json default) and booleans stay
booleans.
Integers stay integers in the JSON (e.g. gross_salary 0, 1, 5000); Task 3 must
convert inputs with Decimal(str(v)), never Decimal(float).
"""

import itertools
import json
from dataclasses import asdict
from pathlib import Path

from payroll.tax.engine import calc_payroll

GROSS = [0, 1, 1234.56, 5000, 8000, 16666.67, 30000, 55000]
HEALTH = [0, 500]
HSA = [0, 300]
TOGGLES = [True, False]
YTD_FICA = [0, 100_000, 184_000, 184_500, 250_000]

OUT = Path(__file__).parent / "golden_cases.json"


def build_cases() -> list[dict]:
    cases = []
    for gross, health, hsa, h_tax, hsa_tax, ytd in itertools.product(
        GROSS, HEALTH, HSA, TOGGLES, TOGGLES, YTD_FICA
    ):
        inputs = {
            "gross_salary": gross,
            "health_insurance": health,
            "hsa_contribution": hsa,
            "health_in_income_tax": h_tax,
            "hsa_in_income_tax": hsa_tax,
            "ytd_fica_wages": ytd,
        }
        cases.append({"inputs": inputs, "expected": asdict(calc_payroll(**inputs))})
    return cases


if __name__ == "__main__":
    cases = build_cases()
    # One compact case per line keeps the file small (pre-commit size limit).
    body = ",\n".join(json.dumps(c, separators=(",", ":")) for c in cases)
    OUT.write_text("[\n" + body + "\n]\n")
    print(f"wrote {len(cases)} cases to {OUT}")
