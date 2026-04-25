"""Translate ingested problems into Hungarian (or other languages later).

Reads from `public.problems` (English source of truth), translates the
problem + solution via gpt-4o-mini, writes the result to
`public.problem_translations`. Then optionally embeds the translation so
Hungarian queries can hit Hungarian text directly.

Cost (approximate, gpt-4o-mini, April 2026):
  * Translation: ~EUR 0.03 per problem -> EUR 540 for the full ~18k corpus
  * Embeddings:  ~EUR 0.0006 per problem -> EUR 11 for ~18k

Use --type or --source filters to translate only a slice while iterating:

    # Hungarian Algebra problems only (~700 rows, ~EUR 21)
    python -m scripts.translate_problems --language hu --type Algebra --embed

    # Smoke test on 5 rows (~EUR 0.15)
    python -m scripts.translate_problems --language hu --limit 5 --embed

The script is idempotent: it only translates problems that don't already
have a translation in the requested language.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import repositories as repo  # noqa: E402
from app.db.schemas import Problem  # noqa: E402
from app.db.supabase import get_supabase_client  # noqa: E402
from app.embeddings import get_embeddings_client  # noqa: E402
from app.llm import get_llm_client  # noqa: E402
from app.db.schemas import MessageInput  # noqa: E402


_TRANSLATE_SYSTEM = """\
You translate K-12 / undergraduate math problems and solutions from
English into the requested target language. Rules:

1. Preserve ALL LaTeX math expressions exactly: $...$, $$...$$, \\boxed{...},
   \\frac, etc. Do not translate or alter anything between $ signs.
2. Preserve numbers, variable names, and units exactly.
3. Translate names of people if culturally appropriate (e.g. "Natalia"
   stays "Natalia" in Hungarian; "John" -> "Janos" is acceptable).
4. The output must be natural, native-sounding mathematical prose, not a
   word-for-word transliteration.
5. If the source contains markup like `<<48/2=24>>`, keep it intact -- it
   is parsed downstream.
6. Reply with ONLY a JSON object: {"problem": "...", "solution": "..."}.
   No prose around it, no markdown fences.
""".strip()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Translate ingested problems into another language."
    )
    p.add_argument(
        "--language",
        required=True,
        choices=["hu"],
        help="Target language code.",
    )
    p.add_argument("--source", default=None, help="Filter by source.")
    p.add_argument("--type", default=None, help="Filter by problem type.")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of problems to translate this run.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Parallel LLM requests (default 8).",
    )
    p.add_argument(
        "--embed",
        action="store_true",
        help="After translating, also embed the translations.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be translated. No LLM calls, no DB writes.",
    )
    return p.parse_args()


def list_untranslated(
    language: str,
    source: str | None,
    type_: str | None,
    limit: int | None,
) -> list[Problem]:
    """Find problems that don't yet have a translation in `language`."""
    sb = get_supabase_client()
    have = (
        sb.table("problem_translations")
        .select("problem_id")
        .eq("language", language)
        .execute()
    )
    have_ids = {row["problem_id"] for row in (have.data or [])}

    q = sb.table("problems").select("*")
    if source:
        q = q.eq("source", source)
    if type_:
        q = q.eq("type", type_)
    if limit:
        q = q.limit(limit + len(have_ids))  # over-fetch, filter after
    res = q.execute()

    out: list[Problem] = []
    for row in res.data or []:
        if row["id"] in have_ids:
            continue
        out.append(Problem.model_validate(row))
        if limit and len(out) >= limit:
            break
    return out


async def translate_one(p: Problem, language: str) -> tuple[str, str] | None:
    """Returns (problem_text, solution_text) in `language`, or None on failure."""
    import json

    llm = get_llm_client()
    user_payload = json.dumps(
        {
            "target_language": language,
            "problem": p.problem_en,
            "solution": p.solution_en,
        },
        ensure_ascii=False,
    )
    raw = await llm.complete(
        [
            MessageInput(role="system", content=_TRANSLATE_SYSTEM),
            MessageInput(role="user", content=user_payload),
        ],
        max_tokens=2000,
    )

    text = raw.strip()
    if text.startswith("```"):
        # Strip stray code fences from misbehaving outputs.
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        obj = json.loads(text)
        return str(obj["problem"]).strip(), str(obj["solution"]).strip()
    except Exception as exc:
        print(f"  WARN: could not parse translation for {p.id}: {exc}")
        return None


async def main() -> int:
    args = parse_args()

    pending = list_untranslated(
        language=args.language,
        source=args.source,
        type_=args.type,
        limit=args.limit,
    )
    print(
        f"Found {len(pending)} problems missing '{args.language}' translation."
    )

    if not pending:
        return 0

    if args.dry_run:
        print("[dry-run] would translate these (first 5 shown):")
        for p in pending[:5]:
            print(f"  {p.source}/{p.type}: {p.problem_en[:80]}...")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    translated: list[tuple] = []  # (problem_id, language, problem_text, solution_text)

    async def _run(p: Problem) -> None:
        async with sem:
            res = await translate_one(p, args.language)
            if res is None:
                return
            translated.append((p.id, args.language, res[0], res[1]))

    print(f"Translating with concurrency={args.concurrency}...")
    await asyncio.gather(*(_run(p) for p in pending))
    print(f"Got {len(translated)} successful translations. Writing to DB...")

    n_inserted = repo.upsert_translations(translated)  # type: ignore[arg-type]
    print(f"Inserted/updated {n_inserted} translation rows.")

    if args.embed:
        print("\nEmbedding the new translations...")
        embeddings = get_embeddings_client()
        # Re-fetch what we just translated so we have the canonical text.
        sb = get_supabase_client()
        ids = [str(t[0]) for t in translated]
        if not ids:
            return 0
        rows = (
            sb.table("problem_translations")
            .select("problem_id,problem_text,solution_text")
            .eq("language", args.language)
            .in_("problem_id", ids)
            .execute()
        ).data or []
        texts = [
            (r["problem_text"] + "\n\n" + r["solution_text"])[:6000]
            for r in rows
        ]
        vectors = await embeddings.embed_concurrent(texts, concurrency=args.concurrency)
        pairs = [
            (r["problem_id"], args.language, v) for r, v in zip(rows, vectors)
        ]
        n_emb = repo.insert_embeddings(pairs)  # type: ignore[arg-type]
        print(f"Stored {n_emb} embeddings for '{args.language}'.")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
