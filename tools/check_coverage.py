"""Enforce a per-module coverage floor on the money-critical code.

A single project-wide percentage hides exactly the wrong thing: a CLI with no tests
can be offset by a thoroughly tested enum, and the average looks fine while the
optimizer is uncovered. So the modules that decide whether to spend money carry
their own floor, checked here.

Usage::

    uv run pytest --cov --cov-report=json
    uv run python tools/check_coverage.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Modules whose branch coverage must meet CRITICAL_FLOOR. These are the ones that
#: compute floats, probabilities, fees, expected value, bundle selection and the
#: approval decision.
CRITICAL_MODULES = (
    "src/tradeup/domain/mathematics.py",
    "src/tradeup/domain/money.py",
    "src/tradeup/domain/conversion.py",
    "src/tradeup/domain/items.py",
    "src/tradeup/domain/rules.py",
    "src/tradeup/domain/fees.py",
    "src/tradeup/optimizer/bundle.py",
    "src/tradeup/optimizer/pareto.py",
    "src/tradeup/valuation/capital.py",
    "src/tradeup/valuation/exit_prices.py",
    "src/tradeup/valuation/expected_value.py",
    "src/tradeup/valuation/partial_fill.py",
    "src/tradeup/valuation/settlement.py",
    "src/tradeup/execution/policy.py",
)

CRITICAL_FLOOR = 90.0
OVERALL_FLOOR = 80.0
COVERAGE_JSON = Path("coverage.json")


def main() -> int:
    if not COVERAGE_JSON.exists():
        print(
            f"{COVERAGE_JSON} not found. Run: uv run pytest --cov --cov-report=json",
            file=sys.stderr,
        )
        return 2

    report = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    files = report.get("files", {})
    normalised = {key.replace("\\", "/"): value for key, value in files.items()}

    failures: list[str] = []
    print(f"{'module':<48}{'branch cov':>12}")
    print("-" * 60)

    for module in CRITICAL_MODULES:
        entry = normalised.get(module)
        if entry is None:
            failures.append(f"{module}: absent from the coverage report")
            print(f"{module:<48}{'MISSING':>12}")
            continue
        percent = float(entry["summary"]["percent_covered"])
        marker = "" if percent >= CRITICAL_FLOOR else "  <-- below floor"
        print(f"{module:<48}{percent:>11.1f}%{marker}")
        if percent < CRITICAL_FLOOR:
            failures.append(f"{module}: {percent:.1f}% < {CRITICAL_FLOOR}%")

    overall = float(report["totals"]["percent_covered"])
    print("-" * 60)
    print(f"{'critical floor':<48}{CRITICAL_FLOOR:>11.1f}%")
    print(f"{'overall':<48}{overall:>11.1f}%  (floor {OVERALL_FLOOR:.0f}%)")

    if overall < OVERALL_FLOOR:
        failures.append(f"overall: {overall:.1f}% < {OVERALL_FLOOR}%")

    if failures:
        print("\nCOVERAGE FAILURES", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\ncoverage floors met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
