"""One-shot downloader: pulls MathQA from the official math-qa.github.io
distribution and writes it as JSONL in the same shape
`scripts/ingest_problems.py` already understands.

Why not Hugging Face: `allenai/math_qa` on HF uses a legacy dataset
loading script that newer `datasets` versions refuse to run. The
original MathQA distribution (a ZIP of three JSON files) is hosted on
the project's GitHub Pages site under Apache 2.0, same content.

Output:  math_problem_example/word_problem/mathqa.jsonl

Run once:
    cd backend
    python -m scripts.download_mathqa

The output file is gitignored (`math_problem_example/` is in
.gitignore). After running this, the existing ingestion pipeline
picks it up:

    python -m scripts.ingest_problems --source mathqa
    python -m scripts.ingest_problems --source mathqa --embed

Volume + cost estimate at ingestion:
  * ~37,300 rows added to `problems`
  * Embedding cost ~$25 at text-embedding-3-small rates (~$0.02/1M tokens)
  * Wall-clock: a few minutes for inserts, ~10-15 min for embeddings
    (with default concurrency=4)
"""

from __future__ import annotations

import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Source URL -- official MathQA distribution
# ---------------------------------------------------------------------------
MATHQA_ZIP_URL = "https://math-qa.github.io/math-QA/data/MathQA.zip"

# scripts/download_mathqa.py -> scripts/ -> backend/ -> repo root
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent.parent / "math_problem_example" / "word_problem"
OUT_PATH = OUT_DIR / "mathqa.jsonl"


# MathQA's `category` field has these values; we use them as the `type`
# in our `problems` table so the placement reranker can filter sensibly.
# Capitalize for consistency with Hendrycks types ("Algebra", "Geometry").
CATEGORY_TO_TYPE: dict[str, str] = {
    "general": "General",
    "geometry": "Geometry",
    "physics": "Physics",
    "probability": "Counting & Probability",
    "gain": "General",      # rare; profit/loss arithmetic
    "other": "General",
}


# Map MathQA `correct` letter (a-e) to its numeric value from `options`.
# `options` looks like: "a ) 24 , b ) 120 , c ) 625 , d ) 720 , e ) 1024".
# Earlier I tried a lookahead-based regex; it had a bug where only
# the LAST option matched (the lookahead's interaction with `re.finditer`
# is fragile). This version is just "letter ) content (until comma or
# end)" which is unambiguous and tests as expected.
_OPTION_RE = re.compile(
    r"([a-e])\s*\)\s*([^,]+?)\s*(?:,|$)", re.IGNORECASE
)


def parse_options(raw: str) -> dict[str, str]:
    return {
        m.group(1).lower(): m.group(2).strip()
        for m in _OPTION_RE.finditer(raw or "")
    }


def extract_answer(correct_letter: str, options_str: str) -> str | None:
    if not correct_letter:
        return None
    opts = parse_options(options_str or "")
    return opts.get(correct_letter.strip().lower())


def transform(row: dict, idx: int, split: str) -> dict | None:
    problem = (row.get("Problem") or "").strip()
    rationale = (row.get("Rationale") or "").strip().strip('"')
    if not problem or not rationale:
        return None

    answer = extract_answer(row.get("correct") or "", row.get("options") or "")
    category = (row.get("category") or "").lower().strip()
    type_label = CATEGORY_TO_TYPE.get(category, "General")

    source_id = f"mathqa-{split}-{idx:06d}"

    return {
        "problem": problem,
        # Use the rationale as the solution -- MathQA rationales are
        # step-by-step, e.g. "5 choices for each of the 4 questions,
        # thus total of 5 * 5 * 5 * 5 = 5 ^ 4 = 625 ways..."
        "solution": rationale,
        "answer": answer,  # may be None if options-parsing failed
        "type": type_label,
        # MathQA doesn't ship per-problem difficulty. Treat it as
        # `easy_medium` -- this matches the corpus_difficulties_for()
        # mapping so the placement system can find them under both
        # "easy" and "medium" buckets.
        "difficulty": "easy_medium",
        "source": "mathqa",
        "source_id": source_id,
        # MathQA-only fields kept under namespaced keys for future use.
        # The ingest_problems.py parser ignores anything it doesn't
        # explicitly read, so these are safe.
        "_mathqa_options": row.get("options"),
        "_mathqa_correct_letter": row.get("correct"),
        "_mathqa_annotated_formula": row.get("annotated_formula"),
        "_mathqa_linear_formula": row.get("linear_formula"),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {MATHQA_ZIP_URL}...")
    with urllib.request.urlopen(MATHQA_ZIP_URL) as resp:
        zip_bytes = resp.read()
    print(f"  got {len(zip_bytes) / 1024 / 1024:.2f} MB")

    print("Extracting in-memory and converting to JSONL...")
    written = 0
    skipped = 0
    by_type: dict[str, int] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf, OUT_PATH.open(
        "w", encoding="utf-8"
    ) as out_f:
        # The zip layout is `MathQA/{train,dev,test}.json`. We accept any
        # casing or nesting depth in case the upstream rearranges.
        for name in zf.namelist():
            base = name.lower().rstrip("/").rsplit("/", 1)[-1]
            if base not in {"train.json", "dev.json", "test.json"}:
                continue
            split = base.replace(".json", "")
            if split == "dev":
                split = "validation"
            print(f"  reading {name} (split={split})...")
            with zf.open(name) as f:
                rows = json.load(f)
            for idx, row in enumerate(rows):
                out = transform(row, idx, split)
                if out is None:
                    skipped += 1
                    continue
                out_f.write(json.dumps(out, ensure_ascii=False) + "\n")
                written += 1
                by_type[out["type"]] = by_type.get(out["type"], 0) + 1

    print(f"\nWrote {written:,} problems to {OUT_PATH}")
    if skipped:
        print(f"Skipped {skipped:,} rows (missing problem or rationale)")
    print("\nBy type:")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {t:<24} {n:>6,}")
    print()
    print("Next steps (from backend/):")
    print("  python -m scripts.ingest_problems --source mathqa")
    print("  python -m scripts.ingest_problems --source mathqa --embed")


if __name__ == "__main__":
    main()
