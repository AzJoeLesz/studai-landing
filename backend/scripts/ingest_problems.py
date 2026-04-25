"""Ingest math problems from JSONL files into the `problems` table.

Layout of the input directory (configurable via --input):

    math_problem_example/
      Algebra/hendrycks.jsonl
      Geometry/hendrycks.jsonl
      ...
      word_problem/gsm8k.jsonl
      word_problem/asdiv.jsonl
      word_problem/svamp.jsonl

Each line of each file is a JSON object with these fields:

    {
      "problem":    "...",
      "solution":   "...",
      "answer":     "..." | null,
      "type":       "Algebra" | "word_problem" | ...,
      "difficulty": "Level 3" | "easy_medium" | ...,
      "source":     "hendrycks" | "gsm8k" | ...
    }

Usage (from backend/, venv active):

    # 1) Validate parsing (no DB writes, no API calls)
    python -m scripts.ingest_problems --dry-run

    # 2) Ingest everything (no embeddings yet -- safe, costs nothing)
    python -m scripts.ingest_problems

    # 3) Ingest + embed in English (~EUR 10 for the full ~18k corpus)
    python -m scripts.ingest_problems --embed

    # 4) Ingest only one source/type (handy while iterating)
    python -m scripts.ingest_problems --source hendrycks --type Algebra

The script is idempotent. Re-running won't create duplicates because
`problems` has a unique constraint on `(source, source_id)` and we use
`upsert(on_conflict="source,source_id")`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Iterator

# Allow `python -m scripts.ingest_problems` from backend/ regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import repositories as repo  # noqa: E402
from app.db.schemas import ProblemInsert  # noqa: E402
from app.embeddings import get_embeddings_client  # noqa: E402

DEFAULT_INPUT = (
    Path(__file__).resolve().parent.parent.parent / "math_problem_example"
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def iter_jsonl_files(root: Path) -> Iterator[Path]:
    """Yield every .jsonl under `root`, deterministically ordered."""
    yield from sorted(root.rglob("*.jsonl"))


def parse_jsonl(path: Path) -> Iterator[ProblemInsert]:
    """Parse a single dataset file into ProblemInsert rows.

    `source_id` includes the parent directory (the math category) because
    multiple hendrycks category folders all contain a file literally
    named `hendrycks.jsonl`. Without the parent in the id, line 1 of
    `Algebra/hendrycks.jsonl` and line 1 of `Geometry/hendrycks.jsonl`
    would both serialize to "hendrycks:hendrycks:1" and one would
    silently overwrite the other on upsert. (We learned this the hard
    way -- the first ingestion only retained 1744 of the 7500
    hendrycks problems.)
    """
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            problem = (obj.get("problem") or "").strip()
            solution = (obj.get("solution") or "").strip()
            if not problem or not solution:
                # Skip obviously broken rows rather than crash mid-ingestion.
                continue
            source = (obj.get("source") or path.stem).strip()
            yield ProblemInsert(
                source=source,
                type=(obj.get("type") or path.parent.name).strip(),
                difficulty=(obj.get("difficulty") or None) and obj["difficulty"].strip(),
                problem_en=problem,
                solution_en=solution,
                answer=(obj.get("answer") or None) and str(obj["answer"]).strip(),
                source_id=f"{source}:{path.parent.name}/{path.stem}:{lineno}",
            )


def collect_rows(
    root: Path,
    source_filter: str | None,
    type_filter: str | None,
    limit: int | None,
) -> list[ProblemInsert]:
    rows: list[ProblemInsert] = []
    for jsonl in iter_jsonl_files(root):
        for row in parse_jsonl(jsonl):
            if source_filter and row.source != source_filter:
                continue
            if type_filter and row.type != type_filter:
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                return rows
    return rows


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------


def insert_in_batches(
    rows: list[ProblemInsert], batch_size: int = 200
) -> int:
    """Insert via repo.upsert_problems in chunks. Returns rows attempted."""
    total = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        repo.upsert_problems(chunk)
        total += len(chunk)
        print(f"  inserted/updated {total}/{len(rows)}")
    return total


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


async def embed_missing(
    language: str = "en", concurrency: int = 4, batch: int = 500
) -> int:
    """Embed every problem that doesn't yet have an `<language>` embedding.

    Pulls `batch` missing rows at a time from the DB, embeds them, writes
    them back. Loops until two consecutive empty responses confirm there's
    nothing left -- this defends against rare cases where Postgres returns
    an empty result on a transient slow query instead of a proper error.
    """
    embeddings = get_embeddings_client()
    total_embedded = 0
    consecutive_empty = 0

    while True:
        missing = repo.list_problems_missing_embedding(  # type: ignore[arg-type]
            language=language, limit=batch
        )
        if not missing:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                # Truly nothing left.
                break
            print("  no missing rows returned, retrying once...")
            continue
        consecutive_empty = 0

        # We embed the problem text + solution together so similarity
        # captures both "looks like" the question and "leads to" the same
        # technique. Trade-off: solutions can be long; we cap at ~6000 chars
        # which keeps us comfortably under the model's input limit.
        texts = [
            (m.problem_en + "\n\n" + m.solution_en)[:6000] for m in missing
        ]
        print(f"  embedding {len(texts)} problems (concurrency={concurrency})...")
        vectors = await embeddings.embed_concurrent(texts, concurrency=concurrency)
        pairs = [
            (m.id, language, v)  # type: ignore[arg-type]
            for m, v in zip(missing, vectors)
        ]
        n = repo.insert_embeddings(pairs)
        total_embedded += n
        print(f"  stored {n} embeddings (cumulative this run: {total_embedded})")
    return total_embedded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest StudAI math problems.")
    p.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Root folder containing JSONL files (default: {DEFAULT_INPUT}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate only. No DB writes, no API calls.",
    )
    p.add_argument(
        "--embed",
        action="store_true",
        help="Also generate English embeddings for any problem missing one.",
    )
    p.add_argument(
        "--source",
        default=None,
        help="Only ingest problems from this source (e.g. 'gsm8k').",
    )
    p.add_argument(
        "--type",
        default=None,
        help="Only ingest problems of this type (e.g. 'Algebra').",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of problems processed (handy for smoke tests).",
    )
    p.add_argument(
        "--embed-concurrency",
        type=int,
        default=4,
        help="Parallel embedding requests (default 4).",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    if not args.input.is_dir():
        print(f"ERROR: input dir not found: {args.input}", file=sys.stderr)
        return 2

    print(f"Scanning {args.input} ...")
    rows = collect_rows(
        args.input,
        source_filter=args.source,
        type_filter=args.type,
        limit=args.limit,
    )
    print(f"Parsed {len(rows)} valid rows.")

    if not rows:
        print("Nothing to ingest.")
        return 0

    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    print("Breakdown by source:")
    for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {src:<12} {n}")

    if args.dry_run:
        print("\n[dry-run] No DB writes performed.")
        return 0

    print(f"\nInserting/updating {len(rows)} rows in batches...")
    insert_in_batches(rows)

    if args.embed:
        print("\nEmbedding pass (English)...")
        n = await embed_missing(language="en", concurrency=args.embed_concurrency)
        print(f"Embedded {n} problems.")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
