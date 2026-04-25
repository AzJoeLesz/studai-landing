"""Eval lab CLI.

Usage (from backend/ with venv active and OPENAI_API_KEY set):

    python -m evals.run                                    # prompt v1 vs all cases
    python -m evals.run --cases evals/cases.yaml
    python -m evals.run --prompt-file app/prompts/tutor_v2.txt --label v2
    python -m evals.run --html out/eval-v1.html
    python -m evals.run --concurrency 10 --model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# Allow `python -m evals.run` from backend/ regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.lab import (  # noqa: E402
    load_cases,
    print_terminal_report,
    run_eval,
    write_html_report,
)


DEFAULT_CASES = Path(__file__).parent / "cases.yaml"
DEFAULT_PROMPT = (
    Path(__file__).resolve().parent.parent / "app" / "prompts" / "tutor_v1.txt"
)
REPORTS_DIR = Path(__file__).parent / "reports"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StudAI eval lab")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p.add_argument(
        "--prompt-file",
        type=Path,
        default=DEFAULT_PROMPT,
        help="Path to a .txt file containing the system prompt to evaluate.",
    )
    p.add_argument(
        "--label",
        default=None,
        help="Human-readable label for this run (default: prompt-file stem).",
    )
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument(
        "--judge-model",
        default="gpt-4o-mini",
        help="Model used as the LLM-as-judge for rubric scoring.",
    )
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument(
        "--html",
        type=Path,
        default=None,
        help="Write a detailed HTML report here (default: evals/reports/<timestamp>.html).",
    )
    p.add_argument(
        "--filter-tag",
        default=None,
        help="Only run cases with this tag.",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    if not args.prompt_file.is_file():
        print(f"ERROR: prompt file not found: {args.prompt_file}", file=sys.stderr)
        return 2

    if not args.cases.is_file():
        print(f"ERROR: cases file not found: {args.cases}", file=sys.stderr)
        return 2

    system_prompt = args.prompt_file.read_text(encoding="utf-8").strip()
    label = args.label or args.prompt_file.stem
    cases = load_cases(args.cases)

    if args.filter_tag:
        before = len(cases)
        cases = [c for c in cases if args.filter_tag in c.tags]
        print(f"Filtered to tag {args.filter_tag!r}: {len(cases)} / {before} cases")

    if not cases:
        print("No cases to run.", file=sys.stderr)
        return 1

    print(
        f"Running {len(cases)} cases against prompt {label!r} with model {args.model}..."
    )
    result = await run_eval(
        cases,
        system_prompt=system_prompt,
        prompt_label=label,
        model=args.model,
        judge_model=args.judge_model,
        concurrency=args.concurrency,
    )

    print_terminal_report(result)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.html or (
        REPORTS_DIR
        / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_{label}.html"
    )
    write_html_report(result, out)
    print(f"HTML report: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
