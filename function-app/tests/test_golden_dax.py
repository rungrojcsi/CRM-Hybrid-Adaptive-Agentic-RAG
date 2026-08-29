"""Golden Q→DAX regression tests.

Each row asserts that the DAX produced by generate_dax() contains the expected
patterns and AVOIDS forbidden ones (anti-patterns from cumulative pitfalls).

Skipped in CI when AZURE_OPENAI_API_KEY is unset (LLM call required).
Run locally: AZURE_OPENAI_API_KEY=... pytest tests/test_golden_dax.py -v
"""
import csv
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

GOLDEN_CSV = Path(__file__).parent / "fixtures" / "quant_golden.csv"

pytestmark = pytest.mark.skipif(
    not os.getenv("AZURE_OPENAI_API_KEY"),
    reason="AZURE_OPENAI_API_KEY not set — golden DAX requires live LLM",
)


def _load_golden() -> list[dict]:
    with open(GOLDEN_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize("row", _load_golden(), ids=lambda r: r["category"] + ":" + r["question"][:30])
def test_golden_dax(row):
    from transform.dax_generator import generate_dax, validate_dax

    dax = generate_dax(row["question"])
    must     = row["expected_must_contain"]
    must_not = row["expected_must_not_contain"]

    assert must in dax, f"DAX missing expected pattern {must!r}\nGenerated DAX:\n{dax}"
    assert must_not not in dax, f"DAX contains forbidden pattern {must_not!r}\nGenerated DAX:\n{dax}"

    # Pre-flight validator must not flag any structural issue
    issues = validate_dax(dax)
    assert issues == [], f"validate_dax flagged: {issues}\nDAX:\n{dax}"
