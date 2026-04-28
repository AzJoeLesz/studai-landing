"""Onboarding endpoints (Phase 9C + 9E).

Three responsibilities:

  * `POST /onboarding/seed-priors`
        Seeds `student_progress` with grade-derived priors. Called once,
        right after the student fills in `grade_level`. Idempotent --
        it won't regress mastery built up via placement / extractor.

  * `POST /onboarding/placement/start`
        Begins an adaptive placement quiz. Returns the first problem +
        a quiz_id to thread through subsequent answer submissions.

  * `POST /onboarding/placement/answer`
        Records one answer, applies a BKT-IDEM update, and returns the
        next problem (or a final summary when 5 questions are done).

Auth: every route requires a Supabase JWT and writes only the
authenticated user's rows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agents import answer_judge
from app.agents import grade_priors as priors_mod
from app.agents import mastery as mastery_mod
from app.agents.retrieval import find_relevant_problems
from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import (
    MessageInput,
    PlacementAttempt,
    Problem,
    StudentProgress,
)
from app.llm import get_llm_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ---------------------------------------------------------------------------
# Seed grade priors (Phase 9C — the always-on cold start)
# ---------------------------------------------------------------------------
class SeedPriorsResponse(BaseModel):
    seeded: int
    curriculum: str | None
    band: str | None
    skipped_existing: bool


@router.post("/seed-priors", response_model=SeedPriorsResponse)
async def seed_priors(user: CurrentUser) -> SeedPriorsResponse:
    """Idempotent seed of grade-derived priors into `student_progress`.

    Resolution chain (so this endpoint is robust to whatever the user typed):
      1. Try `profile.grade_level` -> (curriculum, band) via the resolver.
      2. If that fails, fall back to `profile.age` -> band (us_ccss curriculum).
      3. If both are missing, return seeded=0 (no error -- tutor still works).

    Existing `student_progress` rows are never overwritten -- those rows
    have real evidence (placement / extractor / step_check / rating)
    behind them by the time anyone re-runs seed.
    """
    profile = await asyncio.to_thread(repo.get_profile, user.user_id)
    if profile is None:
        return SeedPriorsResponse(
            seeded=0, curriculum=None, band=None, skipped_existing=False
        )

    resolved = priors_mod.resolve_grade_band(profile.grade_level)
    curriculum: str | None
    band: str | None
    if resolved:
        curriculum, band = resolved
    else:
        band = priors_mod.band_for_age(profile.age)
        curriculum = "us_ccss" if band else None

    pairs = priors_mod.grade_priors_seed(
        profile.grade_level, age=profile.age
    )
    if not pairs:
        return SeedPriorsResponse(
            seeded=0,
            curriculum=curriculum,
            band=band,
            skipped_existing=False,
        )

    rows = ((user.user_id, topic, mastery) for topic, mastery in pairs)
    seeded = await asyncio.to_thread(
        repo.bulk_seed_progress, rows, evidence_source="prior"
    )
    return SeedPriorsResponse(
        seeded=seeded,
        curriculum=curriculum,
        band=band,
        skipped_existing=seeded < len(pairs),
    )


# ---------------------------------------------------------------------------
# Placement quiz (Phase 9E)
# ---------------------------------------------------------------------------
PLACEMENT_LENGTH = 5


class PlacementProblem(BaseModel):
    """The problem we serve to the student in a placement turn."""

    problem_id: UUID
    problem_text: str
    answer: str | None
    difficulty: str
    topic: str
    question_index: int  # 1-based: 1..PLACEMENT_LENGTH


class PlacementStartResponse(BaseModel):
    next: PlacementProblem | None
    completed: bool
    questions_total: int = PLACEMENT_LENGTH


class PlacementAnswerRequest(BaseModel):
    problem_id: UUID
    topic: str = Field(..., min_length=1, max_length=120)
    difficulty: str = Field(..., min_length=1, max_length=40)
    # Free-text answer the student typed in. Empty string is treated
    # as "I don't know" (an explicit incorrect answer).
    student_answer: str = Field(..., max_length=2000)
    # Echo back what we showed them, so the judge can compare without a
    # second DB lookup. Trusted because we control the frontend; an
    # adversarial caller can at worst confuse their own placement.
    problem_text: str = Field(..., max_length=8000)
    canonical_answer: str | None = Field(default=None, max_length=2000)


class PlacementAnswerResponse(BaseModel):
    next: PlacementProblem | None
    completed: bool
    was_correct: bool
    canonical_answer: str | None
    summary: list[StudentProgress] | None = None


def _next_difficulty_label(
    user_id: UUID, topic: str, prior_difficulty: str | None, last_correct: bool | None
) -> str:
    """Pick the difficulty for the next placement problem.

    First problem (no `prior_difficulty`): use the IRT bucket nearest to
    the existing mastery for the topic, or "medium" as default.

    Subsequent problems: simple staircase from the prior difficulty.
    """
    if prior_difficulty is None or last_correct is None:
        existing = repo.get_progress_for_topic(user_id, topic)
        if existing is None:
            return "medium"
        return mastery_mod.pick_difficulty_for(existing.mastery_score)
    return mastery_mod.next_difficulty_after_outcome(
        prior_difficulty, correct=last_correct
    )


def _problem_to_placement(
    problem: Problem, *, topic: str, question_index: int
) -> PlacementProblem:
    return PlacementProblem(
        problem_id=problem.id,
        problem_text=problem.problem_en,
        answer=problem.answer,
        difficulty=problem.difficulty or "medium",
        topic=topic,
        question_index=question_index,
    )


def _topic_for_placement_round(
    user_id: UUID, attempts_so_far: int
) -> str:
    """Pick which topic to probe next.

    Strategy: rotate through the user's seeded priors, with one twist:
    drop topics that already have evidence from this placement run, so
    the 5 questions cover up to 5 different topics instead of cycling
    back to the lowest-mastery row once it gains evidence.
    """
    progress = repo.get_top_progress(user_id, limit=30)
    if progress:
        # Topics with no evidence yet, sorted ascending by mastery so we
        # probe the shakier areas first.
        unseen = [p for p in progress if p.evidence_count == 0]
        unseen.sort(key=lambda p: p.mastery_score)
        if unseen:
            return unseen[attempts_so_far % len(unseen)].topic
        # All priors have been touched (rare on a 5-question quiz with
        # 11+ priors): rotate by lowest mastery overall.
        progress.sort(key=lambda p: p.mastery_score)
        return progress[attempts_so_far % len(progress)].topic
    # No priors seeded yet -- last-resort default.
    return "linear equations"


def _difficulties_for_request(
    profile: priors_mod.PlacementProfile, logical: str | None
) -> list[str]:
    """Translate a logical difficulty bucket using the band's override
    if one exists, else the global default in `mastery.corpus_difficulties_for`.

    Exists so callers can stay band-agnostic when computing the
    difficulty list to filter by.
    """
    if profile.difficulty_map and logical:
        return list(
            profile.difficulty_map.get(logical.strip().lower(), [])
        )
    return mastery_mod.corpus_difficulties_for(logical)


async def _load_placement_profile(
    user_id: UUID,
) -> tuple[priors_mod.PlacementProfile, str | None]:
    """Load the user's profile, return (PlacementProfile, band).

    Single source of truth for "what corpus subset is this student
    allowed to see in the placement quiz". Called once per
    /placement/start and once per /placement/answer.

    Returns the resolved band string too (e.g. `"9-10"`,
    `"university"`, or `None` if neither grade_level nor age was
    parseable). The reranker uses the band string for its prompt.
    """
    profile = await asyncio.to_thread(repo.get_profile, user_id)
    if profile is None:
        return (
            priors_mod.placement_profile_for_user(None, None),
            None,
        )
    resolved = priors_mod.resolve_grade_band(profile.grade_level)
    band = resolved[1] if resolved else priors_mod.band_for_age(profile.age)
    placement_profile = priors_mod.placement_profile_for_user(
        profile.grade_level, profile.age
    )
    return placement_profile, band


# ---------------------------------------------------------------------------
# Placement candidate reranker
# ---------------------------------------------------------------------------


# How many candidates to fetch from the repo for the reranker to choose
# from. Bigger = more chances to find a good fit; smaller = cheaper +
# faster. 12 is a reasonable middle.
_RERANK_POOL_SIZE = 12


async def _rerank_placement_candidates(
    candidates: list[Problem],
    *,
    topic: str,
    band: str | None,
) -> Problem | None:
    """LLM reranker: pick the single best problem for this topic + band.

    Why: semantic search returns problems that are *semantically near*
    the topic name. For a 10th grader on "rational expressions",
    candidate #1 might be a wallet/bills problem that mentions
    "expression" loosely; candidate #5 might be the actual rational-
    expression problem we want. A small LLM call ranks the pool by
    fitness for the band + topic.

    Falls back gracefully:
      * empty candidates -> None
      * single candidate -> return it (no rerank needed)
      * reranker disabled in config -> return first (semantic-top)
      * LLM call fails / unparseable -> return first
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    settings = get_settings()
    if not settings.placement_reranker_enabled:
        return candidates[0]

    # Compact each candidate for the prompt. Keep each under ~280 chars
    # so the whole prompt stays small (~3-5k chars for 12 candidates).
    summaries: list[str] = []
    for i, p in enumerate(candidates, start=1):
        snippet = (p.problem_en or "").strip().replace("\n", " ")[:280]
        summaries.append(
            f"{i}. [src={p.source} | "
            f"diff={p.difficulty or 'n/a'} | "
            f"type={p.type or 'n/a'}]\n   {snippet}"
        )

    rubric = (
        "You are picking ONE math problem from a placement-quiz "
        "candidate list.\n\n"
        f"STUDENT BAND: {band or 'unknown'}\n"
        f"TOPIC SLOT:   {topic}\n\n"
        "Pick the candidate that best satisfies all three:\n"
        "  1. Genuinely about the requested TOPIC (not just sharing\n"
        "     surface words).\n"
        "  2. Appropriately difficult for the BAND -- challenging but\n"
        "     solvable, not trivial review and not Olympiad-level.\n"
        "  3. Clear, unambiguous wording.\n\n"
        f"Output ONLY the integer (1 to {len(candidates)}) of the best\n"
        "candidate. No words, no punctuation, no explanation."
    )
    user_payload = "CANDIDATES:\n" + "\n".join(summaries)

    try:
        llm = get_llm_client()
        response = await llm.complete(
            [
                MessageInput(role="system", content=rubric),
                MessageInput(role="user", content=user_payload),
            ],
            model=settings.placement_reranker_model,
            max_tokens=8,
        )
        first_token = (response or "").strip().split()[0:1]
        if first_token:
            digits = "".join(c for c in first_token[0] if c.isdigit())
            if digits:
                idx = int(digits) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx]
        logger.debug(
            "placement reranker: unparseable response %r; using semantic top",
            response,
        )
    except Exception:
        logger.warning(
            "placement reranker: LLM call failed; using semantic top",
            exc_info=True,
        )
    return candidates[0]


async def _pick_topic_relevant_problem(
    *,
    topic: str,
    difficulty: str | None,
    exclude_ids: list[UUID],
    placement_profile: priors_mod.PlacementProfile,
    band: str | None = None,
) -> Problem | None:
    """Pick a placement problem that matches the requested topic and the
    student's grade band.

    Two filters compose:

      1. **Topic relevance** via the same problem-bank semantic search
         the live tutor uses (`find_relevant_problems`). Embed the
         topic name -> ANN against `problem_embeddings` -> keep the
         top 30 candidate IDs.

      2. **Grade-band appropriateness** via `placement_profile`:
         `placement_profile.sources` is the curated source list for
         the band (e.g. `("gsm8k",)` for grade school, never Hendrycks
         which is too advanced even at "Level 1"); `placement_profile.
         difficulty_map`, when set, overrides the default bucket -> 
         corpus-strings mapping so "easy/medium/hard" mean different
         things inside vs outside Hendrycks.

    Five-step fallback chain (each step is strictly looser than the
    previous one), so a sparse / unembedded corpus never kills the
    quiz mid-stream:

      1. Semantic hits + band sources + band-difficulty bucket
      2. Semantic hits + band sources, no difficulty
      3. Band sources + difficulty bucket, no semantic
      4. Band sources only (no semantic, no difficulty)
      5. None -- caller treats as "corpus exhausted, finish quiz"
    """
    diffs = _difficulties_for_request(placement_profile, difficulty)
    sources = list(placement_profile.sources)

    # Placement-specific similarity floor. We want problems that are
    # actually about the requested topic, not loose semantic neighbors.
    # Lower floor (~0.0) lets gsm8k word problems mentioning vaguely
    # related words match topic centroids like "rational expressions"
    # (the wallet/bills problem incident from a 10th grader's
    # placement quiz). 0.35 is the floor where false matches drop out
    # while genuine on-topic problems still come through.
    hits = await find_relevant_problems(
        topic,
        "en",
        top_k=30,
        similarity_threshold=0.35,
    )
    candidate_ids = [h.id for h in hits]

    if candidate_ids:
        # Get the top-N pool, then let the LLM reranker pick the best
        # for this topic + band. Falls through to non-difficulty-filtered
        # pool if the strict pool is empty.
        pool = await asyncio.to_thread(
            repo.fetch_problems_for_placement_by_ids,
            candidate_ids,
            sources=sources,
            exclude_ids=exclude_ids,
            filter_difficulties=diffs,
            limit=_RERANK_POOL_SIZE,
        )
        if pool:
            chosen = await _rerank_placement_candidates(
                pool, topic=topic, band=band
            )
            if chosen is not None:
                return chosen
        # Mismatched difficulty beats an off-topic problem.
        pool = await asyncio.to_thread(
            repo.fetch_problems_for_placement_by_ids,
            candidate_ids,
            sources=sources,
            exclude_ids=exclude_ids,
            filter_difficulties=None,
            limit=_RERANK_POOL_SIZE,
        )
        if pool:
            chosen = await _rerank_placement_candidates(
                pool, topic=topic, band=band
            )
            if chosen is not None:
                return chosen

    # Semantic search came back empty (corpus may not be embedded yet, or
    # the topic name doesn't ANN-match anything in the band's sources).
    # Fall back to source list + difficulty bucket.
    chosen = await asyncio.to_thread(
        repo.fetch_problem_for_placement,
        sources=sources,
        exclude_ids=exclude_ids,
        filter_difficulties=diffs,
    )
    if chosen is not None:
        return chosen

    # Final safety net: any band-allowed problem at all. Better an
    # off-topic, off-difficulty placement question than the quiz dying
    # on its second turn.
    return await asyncio.to_thread(
        repo.fetch_problem_for_placement,
        sources=sources,
        exclude_ids=exclude_ids,
        filter_difficulties=None,
    )


@router.post("/placement/start", response_model=PlacementStartResponse)
async def placement_start(user: CurrentUser) -> PlacementStartResponse:
    """Start a placement quiz. Returns the first problem.

    If the user already completed PLACEMENT_LENGTH attempts, returns
    `completed=True, next=None` -- the frontend handles that as "you've
    already done this". Re-runs are allowed but additive (we always
    record new attempts).
    """
    attempts = await asyncio.to_thread(
        repo.list_placement_attempts, user.user_id, limit=PLACEMENT_LENGTH
    )
    if len(attempts) >= PLACEMENT_LENGTH:
        return PlacementStartResponse(next=None, completed=True)

    placement_profile, band = await _load_placement_profile(user.user_id)
    topic = await asyncio.to_thread(
        _topic_for_placement_round, user.user_id, len(attempts)
    )
    difficulty = await asyncio.to_thread(
        _next_difficulty_label,
        user.user_id,
        topic,
        None,
        None,
    )
    problem = await _pick_topic_relevant_problem(
        topic=topic,
        difficulty=difficulty,
        exclude_ids=[a.problem_id for a in attempts],
        placement_profile=placement_profile,
        band=band,
    )
    if problem is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No problems available for placement.",
        )
    return PlacementStartResponse(
        next=_problem_to_placement(
            problem, topic=topic, question_index=len(attempts) + 1
        ),
        completed=False,
    )


@router.post("/placement/answer", response_model=PlacementAnswerResponse)
async def placement_answer(
    payload: PlacementAnswerRequest,
    user: CurrentUser,
) -> PlacementAnswerResponse:
    """Judge a free-text answer, apply BKT-IDEM, return the next problem.

    1. LLM judge decides correctness (with a strict-string fallback if
       the LLM call fails). Empty string / "I don't know" short-circuits
       to incorrect without a paid call.
    2. Record `placement_attempts` row.
    3. BKT-IDEM update on `student_progress` with evidence_source='placement'.
    4. Pick + return the next problem, or completion summary.
    """
    was_correct = await answer_judge.judge_answer(
        problem_text=payload.problem_text,
        canonical_answer=payload.canonical_answer,
        student_answer=payload.student_answer,
    )

    await asyncio.to_thread(
        repo.record_placement_attempt,
        PlacementAttempt(
            user_id=user.user_id,
            problem_id=payload.problem_id,
            topic=payload.topic,
            difficulty=payload.difficulty,
            correct=was_correct,
        ),
    )
    await asyncio.to_thread(
        mastery_mod.apply_graded_update,
        user_id=user.user_id,
        topic=payload.topic,
        correct=was_correct,
        difficulty=payload.difficulty,
        evidence_source="placement",
    )

    attempts = await asyncio.to_thread(
        repo.list_placement_attempts, user.user_id, limit=PLACEMENT_LENGTH
    )
    if len(attempts) >= PLACEMENT_LENGTH:
        summary = await asyncio.to_thread(
            repo.get_top_progress, user.user_id, limit=10
        )
        return PlacementAnswerResponse(
            next=None,
            completed=True,
            was_correct=was_correct,
            canonical_answer=payload.canonical_answer,
            summary=summary,
        )

    placement_profile, band = await _load_placement_profile(user.user_id)
    next_topic = await asyncio.to_thread(
        _topic_for_placement_round, user.user_id, len(attempts)
    )
    next_difficulty = await asyncio.to_thread(
        _next_difficulty_label,
        user.user_id,
        next_topic,
        payload.difficulty,
        was_correct,
    )
    next_problem = await _pick_topic_relevant_problem(
        topic=next_topic,
        difficulty=next_difficulty,
        exclude_ids=[a.problem_id for a in attempts],
        placement_profile=placement_profile,
        band=band,
    )
    if next_problem is None:
        # Corpus exhausted -- treat as completed early.
        summary = await asyncio.to_thread(
            repo.get_top_progress, user.user_id, limit=10
        )
        return PlacementAnswerResponse(
            next=None,
            completed=True,
            was_correct=was_correct,
            canonical_answer=payload.canonical_answer,
            summary=summary,
        )
    return PlacementAnswerResponse(
        next=_problem_to_placement(
            next_problem,
            topic=next_topic,
            question_index=len(attempts) + 1,
        ),
        completed=False,
        was_correct=was_correct,
        canonical_answer=payload.canonical_answer,
    )


# ---------------------------------------------------------------------------
# Placement status (so the frontend can decide whether to show the "take
# the placement quiz" CTA without a round-trip elsewhere)
# ---------------------------------------------------------------------------
class PlacementStatusResponse(BaseModel):
    completed: bool
    attempts_so_far: int
    questions_total: int = PLACEMENT_LENGTH


@router.get("/placement/status", response_model=PlacementStatusResponse)
async def placement_status(user: CurrentUser) -> PlacementStatusResponse:
    attempts = await asyncio.to_thread(
        repo.list_placement_attempts, user.user_id, limit=PLACEMENT_LENGTH
    )
    return PlacementStatusResponse(
        completed=len(attempts) >= PLACEMENT_LENGTH,
        attempts_so_far=len(attempts),
    )
