"""Inspect what content is actually in the Supabase corpus.

Reports:
  * `problems`                 -- count by source, count by (source, difficulty)
  * `problem_embeddings`       -- count by language, plus how many problems
                                  have an English embedding vs not
  * `teaching_material_chunks` -- count by (source, book_slug), plus
                                  how many have an embedding
  * `problem_annotations`      -- total count

Use this when you want to know "have I actually ingested the OpenStax
prealgebra book?" or "do my placement problems have hendrycks Level 2
content for the 9-10 grader?".

Run:
    cd backend
    python -m scripts.check_corpus_state

Read-only. Doesn't change any data.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Allow `python -m scripts.check_corpus_state` from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.supabase import get_supabase_client  # noqa: E402


def _fmt_count(n: int) -> str:
    return f"{n:>7,}"


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _table_count(table: str) -> int:
    sb = get_supabase_client()
    res = (
        sb.table(table).select("*", count="exact", head=True).execute()
    )
    return int(res.count or 0)


def _all_rows(table: str, columns: str, page_size: int = 1000) -> list[dict]:
    """Paginated full-table scan. Used for the small-ish summary tables."""
    sb = get_supabase_client()
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            sb.table(table)
            .select(columns)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        chunk = res.data or []
        out.extend(chunk)
        if len(chunk) < page_size:
            return out
        offset += page_size


def report_problems() -> None:
    _section("public.problems  (the live problem bank)")
    rows = _all_rows("problems", "source,difficulty,type")
    if not rows:
        print("  (empty -- no problems ingested)")
        return

    by_source = Counter(r["source"] for r in rows)
    print(f"  Total: {_fmt_count(len(rows))}")
    print()
    print("  By source:")
    for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"    {src:<24} {_fmt_count(n)}")

    print()
    print("  By (source, difficulty):")
    by_sd: Counter[tuple[str, str]] = Counter(
        (r["source"], r.get("difficulty") or "(null)") for r in rows
    )
    for (src, diff), n in sorted(by_sd.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {src:<14} {diff:<14} {_fmt_count(n)}")

    print()
    print("  By (source, type)  -- top 30:")
    by_st: Counter[tuple[str, str]] = Counter(
        (r["source"], r.get("type") or "(null)") for r in rows
    )
    for (src, typ), n in by_st.most_common(30):
        print(f"    {src:<14} {typ:<32} {_fmt_count(n)}")


def report_problem_embeddings() -> None:
    _section("public.problem_embeddings  (used by RAG + placement)")
    total = _table_count("problem_embeddings")
    if total == 0:
        print("  (empty -- no problems are embedded)")
        return
    print(f"  Total embedding rows: {_fmt_count(total)}")

    rows = _all_rows("problem_embeddings", "language")
    by_lang = Counter(r["language"] for r in rows)
    print("  By language:")
    for lang, n in sorted(by_lang.items()):
        print(f"    {lang:<6}  {_fmt_count(n)}")

    problem_count = _table_count("problems")
    en_count = by_lang.get("en", 0)
    if problem_count:
        print()
        print(
            f"  English coverage: {en_count:,}/{problem_count:,} = "
            f"{en_count / problem_count:.1%}"
        )


def report_teaching_material() -> None:
    _section("public.teaching_material_chunks  (OpenStax L2 RAG)")
    rows = _all_rows("teaching_material_chunks", "source,book_slug")
    if not rows:
        print("  (empty -- no books extracted/ingested)")
        print("  To ingest, run from backend/:")
        print("    python -m scripts.extract_books")
        print("    python -m scripts.ingest_openstax_material")
        return

    by_book = Counter((r["source"], r["book_slug"]) for r in rows)
    print(f"  Total chunks: {_fmt_count(len(rows))}")
    print()
    print("  By (source, book_slug):")
    for (src, slug), n in sorted(by_book.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        print(f"    {src:<10} {slug:<40} {_fmt_count(n)}")

    embedded = _table_count("teaching_material_embeddings")
    if len(rows):
        print()
        print(
            f"  Embedded chunks: {embedded:,}/{len(rows):,} = "
            f"{embedded / len(rows):.1%}"
        )
        if embedded < len(rows):
            print(
                "  Some chunks aren't embedded -- run "
                "`python -m scripts.ingest_openstax_material` to "
                "complete embeddings."
            )


def report_annotations() -> None:
    _section("public.problem_annotations  (Phase 9 L3 RAG, optional)")
    n = _table_count("problem_annotations")
    if n == 0:
        print("  (empty -- annotation pass not run)")
        print("  Optional. To populate: `python -m scripts.annotate_problems`")
    else:
        print(f"  Annotated problems: {_fmt_count(n)}")


def main() -> None:
    print("Inspecting Supabase content state for StudAI...")
    report_problems()
    report_problem_embeddings()
    report_teaching_material()
    report_annotations()
    print()


if __name__ == "__main__":
    main()
