"""Phase 10E — band-balanced solution-path generation orchestrator.

Drives `scripts/generate_solution_paths::process_problem` per band per
topic so the verified-paths corpus has even coverage across:
  * grade bands  (K-2, 3-5, 6-8, 9-10, 11-12, university)
  * topics within each band  (read from `data/grade_priors.json`)

Why this exists: the founder's first run was `--from-annotations`,
which used the 205-problem L3 set. That set turned out to be heavily
gsm8k/asdiv/svamp (grade-school word problems), so the verified-paths
corpus ended up unusable for 9-12 students. This orchestrator fixes
the coverage gap by selecting problems via topic-aware semantic search
(same machinery as the placement quiz) AND filtering by per-band
source profiles (same `agents.grade_priors.placement_profile_for_band`
table the quiz uses, so K-8 gets gsm8k, 9-12 gets hendrycks, etc.).

Usage (from `backend/`, venv active):

    # Generate ~100 paths for one band, evenly spaced across that band's
    # topics. Idempotent -- skips problems that already have paths.
    python -m scripts.generate_band_corpus --band 9-10 --per-band 100

    # Same for all six bands. ~600 paths total at ~$30 LLM cost.
    # Recommended to run band-by-band so verification can stay caught up.
    python -m scripts.generate_band_corpus --all-bands --per-band 100

    # Cheap dev run (no DB writes, no critic): just verify wiring + see prompts.
    python -m scripts.generate_band_corpus --band 6-8 --per-band 5 --dry-run --no-critic

Cost (gpt-5-mini gen + gpt-5 critic): ~$0.025-0.035 per problem.
    100 problems -> ~$3
    600 problems -> ~$15-20
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.grade_priors import (  # noqa: E402
    PlacementProfile,
    all_band_names,
    placement_profile_for_band,
    topics_for_band,
)
from app.agents.mastery import corpus_difficulties_for  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.db import repositories as repo  # noqa: E402
from app.db.schemas import Problem  # noqa: E402
from app.embeddings.openai_embeddings import OpenAIEmbeddingsClient  # noqa: E402

# Reuse the path generator's actual processing function -- this file
# is just topic+band orchestration around it.
from scripts.generate_solution_paths import (  # noqa: E402
    PathGenOptions,
    load_system_prompt,
    process_problem,
)

logger = logging.getLogger("generate_band_corpus")

# Per-band difficulty mapping for path generation. Mirrors the placement-quiz
# mapping in `agents.grade_priors.placement_profile_for_band` because the
# same "Level 1 means very different things at 9-10 vs 11-12" reasoning
# applies. None means "any difficulty in this band's source list is OK".
_PATH_GEN_DIFFICULTY_BY_BAND: dict[str, list[str] | None] = {
    "K-2":        None,                              # gsm8k has no difficulty levels
    "3-5":        None,
    "6-8":        None,
    "9-10":       ["Level 1", "Level 2"],            # hendrycks easy/medium
    "11-12":      ["Level 2", "Level 3"],            # hendrycks medium-hard
    "university": ["Level 4", "Level 5"],            # hendrycks olympiad
}

# Conservative similarity floor for topic-based candidate selection.
# Below this we treat the match as too loose to be useful.
_TOPIC_SIMILARITY_FLOOR = 0.30
# Per topic, how wide a candidate net to cast before applying source/
# difficulty/length filters. Bigger pool = better filter survival rate
# at small extra DB cost.
_CANDIDATE_POOL_PER_TOPIC = 30
# Length sanity: don't pick problems that are too short (parse errors)
# or too long (multi-page proofs that blow the prompt budget).
_MIN_LEN = 30
_MAX_LEN = 800


async def _candidates_for_topic(
    *,
    emb: OpenAIEmbeddingsClient,
    topic: str,
    sources: list[str] | None,
    difficulties: list[str] | None,
    exclude_ids: list[UUID],
    target_count: int,
    language: str = "en",
) -> list[Problem]:
    """Pick up to `target_count` problems on `topic`, filtered to band sources.

    1. Embed the topic name (reusing the same model as the topic
       classifier and placement quiz).
    2. ANN-search the problem bank for the top ~30 closest matches.
    3. Apply source + difficulty + already-has-paths-in-language filters
       on top, preserving rank.

    Falls back to a non-semantic source-only filter if the topic
    centroid yields no usable candidates -- so a sparse-coverage topic
    still produces SOMETHING rather than a silent zero.
    """
    try:
        topic_vec = await emb.embed_one(topic)
    except Exception:
        logger.warning(
            "embed failed for topic=%s; falling back to source-only", topic
        )
        topic_vec = None

    candidate_ids: list[UUID] = []
    if topic_vec is not None:
        try:
            hits = await asyncio.to_thread(
                repo.search_problems, topic_vec, language, match_count=_CANDIDATE_POOL_PER_TOPIC
            )
            candidate_ids = [
                h.id for h in hits if h.similarity >= _TOPIC_SIMILARITY_FLOOR
            ]
        except Exception:
            logger.warning(
                "search_problems failed for topic=%s; falling back to source-only",
                topic,
            )
            candidate_ids = []

    if candidate_ids:
        filtered = await asyncio.to_thread(
            repo.fetch_problems_by_ids,
            candidate_ids,
            sources=sources,
            difficulties=difficulties,
            exclude_ids=exclude_ids,
            limit=target_count * 3,  # over-fetch for length-window filtering
        )
        # Length sanity.
        in_window = [
            p
            for p in filtered
            if p.problem_en and _MIN_LEN <= len(p.problem_en) <= _MAX_LEN
        ]
        # Drop already-pathed problems (separate query rather than threading
        # `only_without_paths_in_language` through the placement helper).
        in_window = await _drop_already_pathed(in_window, language=language)
        if in_window:
            return in_window[:target_count]

    # Fallback: source-only, no semantic. Better than nothing.
    fallback = await asyncio.to_thread(
        repo.list_problems_filtered,
        sources=sources,
        difficulties=difficulties,
        exclude_ids=exclude_ids,
        only_without_paths_in_language=language,
        limit=target_count * 3,
    )
    in_window = [
        p
        for p in fallback
        if p.problem_en and _MIN_LEN <= len(p.problem_en) <= _MAX_LEN
    ]
    return in_window[:target_count]


async def _drop_already_pathed(
    problems: list[Problem], *, language: str
) -> list[Problem]:
    """Remove problems that already have a `solution_paths` row in `language`.

    The orchestrator is idempotent -- re-running on a band that's
    partially generated picks up only the gaps. Otherwise we'd
    waste LLM calls on problems already in the verification queue.
    """
    if not problems:
        return []
    already = await asyncio.to_thread(
        repo.problem_ids_with_paths,
        [p.id for p in problems],
        language=language,
    )
    return [p for p in problems if str(p.id) not in already]


async def _generate_for_band(
    *,
    band: str,
    curriculum: str,
    per_band: int,
    options: PathGenOptions,
    settings,
    oa: AsyncOpenAI,
    emb: OpenAIEmbeddingsClient,
    system_prompt: str,
    concurrency: int,
) -> int:
    """Drive generation for one band. Returns the number of problems processed."""
    profile: PlacementProfile = placement_profile_for_band(band)
    sources = list(profile.sources) if profile.sources else None
    difficulties = _PATH_GEN_DIFFICULTY_BY_BAND.get(band)
    if difficulties is None and band in ("9-10", "11-12", "university"):
        # Fallback in case _PATH_GEN_DIFFICULTY_BY_BAND is out of sync.
        difficulties = corpus_difficulties_for("medium")

    topics = topics_for_band(curriculum, band)
    if not topics:
        logger.warning("no topics found for band=%s curriculum=%s", band, curriculum)
        return 0

    per_topic = max(1, per_band // len(topics))
    leftover = per_band - per_topic * len(topics)

    print(
        f"\n=== band={band} curriculum={curriculum} ===\n"
        f"  topics={len(topics)} per_topic={per_topic} leftover={leftover}\n"
        f"  sources={sources or 'any'} difficulties={difficulties or 'any'}",
        flush=True,
    )

    sem = asyncio.Semaphore(concurrency)
    processed = 0
    seen_ids: set[UUID] = set()

    for i, topic in enumerate(topics):
        # Distribute leftover across the first `leftover` topics.
        topic_target = per_topic + (1 if i < leftover else 0)
        if topic_target < 1:
            continue
        candidates = await _candidates_for_topic(
            emb=emb,
            topic=topic,
            sources=sources,
            difficulties=difficulties,
            exclude_ids=list(seen_ids),
            target_count=topic_target,
            language=options.language,
        )
        if not candidates:
            print(
                f"  [topic={topic!r}] no candidates after filtering; skipping",
                flush=True,
            )
            continue
        print(
            f"  [topic={topic!r}] generating {len(candidates)} paths",
            flush=True,
        )

        async def _one(p: Problem) -> bool:
            async with sem:
                return await process_problem(
                    client=oa,
                    emb=emb,
                    system_prompt=system_prompt,
                    problem=p,
                    options=options,
                    path_gen_model=settings.path_gen_model,
                    path_critic_model=settings.path_critic_model,
                )

        results = await asyncio.gather(*[_one(p) for p in candidates])
        for p, ok in zip(candidates, results):
            seen_ids.add(p.id)
            if ok:
                processed += 1

    print(f"=== band={band} done: processed={processed} ===\n", flush=True)
    return processed


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    emb = OpenAIEmbeddingsClient()
    oa = AsyncOpenAI(api_key=settings.openai_api_key)
    system_prompt = load_system_prompt()

    options = PathGenOptions(
        language=args.language,
        overwrite=args.overwrite,
        no_critic=args.no_critic,
        dry_run=args.dry_run,
        use_existing_annotation=False,
        persistence_source="band_orchestrator",
    )

    bands_to_run: list[str]
    if args.all_bands:
        bands_to_run = list(all_band_names())
    elif args.band:
        bands_to_run = [args.band]
    else:
        print("Specify --band <name> or --all-bands.", flush=True)
        return 2

    total = 0
    for band in bands_to_run:
        n = await _generate_for_band(
            band=band,
            curriculum=args.curriculum,
            per_band=args.per_band,
            options=options,
            settings=settings,
            oa=oa,
            emb=emb,
            system_prompt=system_prompt,
            concurrency=args.concurrency,
        )
        total += n

    print(
        f"\nALL DONE: bands={bands_to_run} total_processed={total}",
        flush=True,
    )
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(
        description=(
            "Topic-balanced solution-path generation per grade band. "
            "Wraps scripts/generate_solution_paths::process_problem with "
            "topic-aware candidate selection (semantic search) and "
            "per-band source profiles. See "
            "docs/phase10_solution_graphs.md for the full design."
        )
    )
    band_group = ap.add_mutually_exclusive_group(required=False)
    band_group.add_argument(
        "--band",
        choices=list(all_band_names()),
        default=None,
        help="Generate for one band (e.g. '9-10').",
    )
    band_group.add_argument(
        "--all-bands",
        action="store_true",
        help=(
            "Generate for every band, in order. ~$30 LLM cost at "
            "100/band. Idempotent -- safe to interrupt and re-run."
        ),
    )
    ap.add_argument(
        "--per-band",
        type=int,
        default=100,
        help=(
            "Target problems per band, evenly spaced across that "
            "band's topics. Default 100. With 8-18 topics per band "
            "this works out to ~5-12 paths per topic."
        ),
    )
    ap.add_argument(
        "--curriculum",
        default="us_ccss",
        choices=("us_ccss", "hu_nat"),
        help="Topic source. Default us_ccss; pass hu_nat for HU curriculum.",
    )
    ap.add_argument(
        "--language",
        default="en",
        choices=("en", "hu"),
        help="Generated paths language. Default en.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help=(
            "Concurrent generations per topic batch. Default 2 (matches "
            "annotate_problems.py and avoids OpenAI rate-limit churn)."
        ),
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Wipe + regenerate paths even when they already exist.",
    )
    ap.add_argument(
        "--no-critic",
        action="store_true",
        help="Skip the LLM-as-judge pre-filter (saves ~30%% cost).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate + critique but don't write to the DB.",
    )
    args = ap.parse_args()
    if not (args.band or args.all_bands):
        ap.print_help()
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
