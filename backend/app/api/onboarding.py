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
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agents import grade_priors as priors_mod
from app.agents import mastery as mastery_mod
from app.api.deps import CurrentUser
from app.db import repositories as repo
from app.db.schemas import PlacementAttempt, Problem, StudentProgress

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

    Reads the current profile.grade_level, resolves to (curriculum, band),
    looks up the per-band priors, and writes rows for any topic the user
    doesn't already have. Topics with existing rows are NOT overwritten
    (those have real evidence behind them now).
    """
    profile = await asyncio.to_thread(repo.get_profile, user.user_id)
    if profile is None or not profile.grade_level:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Set grade_level on your profile before seeding priors.",
        )

    resolved = priors_mod.resolve_grade_band(profile.grade_level)
    if resolved is None:
        return SeedPriorsResponse(
            seeded=0,
            curriculum=None,
            band=None,
            skipped_existing=False,
        )
    curriculum, band = resolved

    pairs = priors_mod.grade_priors_seed(profile.grade_level)
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
    correct: bool


class PlacementAnswerResponse(BaseModel):
    next: PlacementProblem | None
    completed: bool
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

    Strategy: rotate through the user's seeded priors, preferring topics
    with fewer evidence rows. This keeps the 5 questions diverse rather
    than 5 in the same area.
    """
    progress = repo.get_top_progress(user_id, limit=20)
    if progress:
        # Sort by (evidence_count asc, mastery_score asc) -- least-known first.
        progress.sort(key=lambda p: (p.evidence_count, p.mastery_score))
        return progress[attempts_so_far % len(progress)].topic
    # No priors seeded yet -- fall back to a sensible default.
    return "linear equations"


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
    problem = await asyncio.to_thread(
        repo.fetch_problem_for_placement,
        exclude_ids=[a.problem_id for a in attempts],
        filter_difficulty=difficulty,
    )
    if problem is None:
        # Rare: empty or filtered-out corpus. Drop the difficulty filter.
        problem = await asyncio.to_thread(
            repo.fetch_problem_for_placement,
            exclude_ids=[a.problem_id for a in attempts],
            filter_difficulty=None,
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
    """Record an answer + apply BKT-IDEM + return the next problem (or finish)."""
    # Record attempt + progress update synchronously (the user is waiting
    # on the next problem and these are tiny writes).
    await asyncio.to_thread(
        repo.record_placement_attempt,
        PlacementAttempt(
            user_id=user.user_id,
            problem_id=payload.problem_id,
            topic=payload.topic,
            difficulty=payload.difficulty,
            correct=payload.correct,
        ),
    )
    await asyncio.to_thread(
        mastery_mod.apply_graded_update,
        user_id=user.user_id,
        topic=payload.topic,
        correct=payload.correct,
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
            next=None, completed=True, summary=summary
        )

    next_topic = await asyncio.to_thread(
        _topic_for_placement_round, user.user_id, len(attempts)
    )
    next_difficulty = await asyncio.to_thread(
        _next_difficulty_label,
        user.user_id,
        next_topic,
        payload.difficulty,
        payload.correct,
    )
    next_problem = await asyncio.to_thread(
        repo.fetch_problem_for_placement,
        exclude_ids=[a.problem_id for a in attempts],
        filter_difficulty=next_difficulty,
    )
    if next_problem is None:
        next_problem = await asyncio.to_thread(
            repo.fetch_problem_for_placement,
            exclude_ids=[a.problem_id for a in attempts],
            filter_difficulty=None,
        )
    if next_problem is None:
        # Corpus exhausted -- treat as completed early.
        summary = await asyncio.to_thread(
            repo.get_top_progress, user.user_id, limit=10
        )
        return PlacementAnswerResponse(
            next=None, completed=True, summary=summary
        )
    return PlacementAnswerResponse(
        next=_problem_to_placement(
            next_problem,
            topic=next_topic,
            question_index=len(attempts) + 1,
        ),
        completed=False,
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
