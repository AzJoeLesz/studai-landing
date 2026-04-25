"""Quick sanity check — used during lab development. Safe to delete."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.lab import RUBRICS, load_cases  # noqa: E402


def main() -> None:
    cases = load_cases(Path(__file__).parent / "cases.yaml")
    print(f"cases  : {len(cases)}")
    print(f"rubrics: {len(RUBRICS)}")

    referenced = {r for c in cases for r in c.rubrics}
    unknown = referenced - set(RUBRICS)
    print(f"unknown rubric refs in cases: {sorted(unknown) if unknown else 'none'}")

    unused = set(RUBRICS) - referenced
    print(f"rubrics defined but never used: {sorted(unused) if unused else 'none'}")

    print("\nrubric usage across cases:")
    counter = Counter(r for c in cases for r in c.rubrics)
    for name in sorted(RUBRICS, key=lambda n: -counter.get(n, 0)):
        print(f"  {name:<40} {counter.get(name, 0):>3}  weight={RUBRICS[name].weight}")

    print("\ncase totals by rubric count:")
    case_rubric_counts = Counter(len(c.rubrics) for c in cases)
    for n, k in sorted(case_rubric_counts.items()):
        print(f"  {n} rubrics : {k} cases")

    total_judge_calls = sum(
        sum(1 for r in c.rubrics if r in RUBRICS and RUBRICS[r].judge_template is not None)
        for c in cases
    )
    print(f"\ntotal LLM-judge calls per full run: {total_judge_calls}")


if __name__ == "__main__":
    main()
