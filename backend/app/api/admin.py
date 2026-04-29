"""Phase 10C — admin endpoints for the verification queue.

All routes here are gated on `profiles.role == 'admin'` via
`AdminUser` in `app/api/deps.py`. The frontend at
`app/[locale]/admin/paths/page.tsx` reads from these.

What the verification queue is FOR:
  * The path generator (`scripts/generate_solution_paths.py`) writes
    solution_paths rows with `verified=false`.
  * The runtime guided mode (`agents/guided_mode.prepare_turn`) only
    consumes `verified=true` paths.
  * The admin (you) reviews the queue at /admin/paths, approving the
    good ones and rejecting (= keeping verified=false) the bad.

Throughput target: ~20 seconds per path with this UI = ~180/hour.
The 500-700-path MVP set is one focused weekend.

We deliberately do NOT expose:
  * Edit-in-place of generated content. Decision deferred until first
    100 verifications surface what kinds of edits are common.
  * Hard delete. `mark_path_verified(verified=False)` is a soft reject;
    the row stays for audit trail.
  * Bulk operations. One-at-a-time review forces a real read; bulk
    "approve all" defeats the verification purpose.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import AdminUser
from app.db import repositories as repo
from app.db.schemas import (
    CommonMistake,
    Problem,
    SolutionPath,
    SolutionStep,
    StepHint,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Response models — flat, JSON-friendly shapes the Next.js client consumes.
# ---------------------------------------------------------------------------
class AdminPathListItem(BaseModel):
    """Compact row shown in the verification queue list view.

    Sorted server-side: critic_score desc nulls last (high-confidence
    paths bubble to the top so the easy approves go first), then
    created_at desc as a tie-breaker.
    """

    id: UUID
    problem_id: UUID
    name: str
    rationale: str | None = None
    preferred: bool
    language: str
    verified: bool
    critic_score: float | None = None
    source: str | None = None
    model: str | None = None
    # Compact problem context so the list rows are scannable without
    # opening every detail view.
    problem_type: str
    problem_difficulty: str | None = None
    problem_preview: str  # first ~200 chars of problem_en


class AdminPathDetail(BaseModel):
    """Full payload for the detail view: path + all of its children."""

    path: SolutionPath
    problem: Problem
    steps: list[SolutionStep]
    hints_by_step: dict[UUID, list[StepHint]]
    mistakes_by_step: dict[UUID, list[CommonMistake]]
    problem_scoped_mistakes: list[CommonMistake]


class AdminPathListResponse(BaseModel):
    items: list[AdminPathListItem]
    next_offset: int | None = None  # None when there's nothing more


class AdminPathVerifyResponse(BaseModel):
    id: UUID
    verified: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
_LIST_LIMIT_DEFAULT = 25
_LIST_LIMIT_MAX = 100
_PROBLEM_PREVIEW_CHARS = 220


def _build_list_item(
    path: SolutionPath, problem: Problem | None
) -> AdminPathListItem | None:
    """Build a compact list row, dropping the path if its problem
    cascade-deleted out from under us (defensive — shouldn't happen
    given the FK with on delete cascade, but cheap to guard).
    """
    if problem is None:
        return None
    body = (problem.problem_en or "")[: _PROBLEM_PREVIEW_CHARS].strip()
    return AdminPathListItem(
        id=path.id,
        problem_id=path.problem_id,
        name=path.name,
        rationale=path.rationale,
        preferred=path.preferred,
        language=path.language,
        verified=path.verified,
        critic_score=path.critic_score,
        source=path.source,
        model=path.model,
        problem_type=problem.type,
        problem_difficulty=problem.difficulty,
        problem_preview=body,
    )


@router.get("/paths", response_model=AdminPathListResponse)
async def list_paths(
    user: AdminUser,
    status_filter: str = "unverified",
    limit: int = _LIST_LIMIT_DEFAULT,
    offset: int = 0,
) -> AdminPathListResponse:
    """List paths for the verification queue.

    Query params:
      * `status_filter`  - "unverified" (default) | "verified" | "all".
                          The MVP UI only uses unverified; the others
                          let admins audit accepted content later.
      * `limit`          - 1-100, default 25.
      * `offset`         - simple pagination; the frontend renders
                          25/page lists for now.

    Returns up to `limit` rows plus a `next_offset` (or null if the
    page was the last one).
    """
    capped_limit = max(1, min(_LIST_LIMIT_MAX, limit))
    capped_offset = max(0, offset)
    if status_filter not in ("unverified", "verified", "all"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="status_filter must be 'unverified', 'verified', or 'all'",
        )

    # Pull a window of paths in the right status. We fetch
    # `capped_limit + 1` so we can compute next_offset cheaply.
    paths: list[SolutionPath] = await asyncio.to_thread(
        repo.list_admin_paths,
        status_filter=status_filter,
        limit=capped_limit + 1,
        offset=capped_offset,
    )

    has_more = len(paths) > capped_limit
    visible = paths[:capped_limit]

    # Fetch the corresponding problems in parallel. Each is one
    # indexed query; for a 25-page that's 25 cheap queries -- fast
    # enough that we don't bother with a single batched IN(...) for
    # the MVP.
    problems = await asyncio.gather(
        *[asyncio.to_thread(repo.get_problem, p.problem_id) for p in visible]
    )

    items: list[AdminPathListItem] = []
    for path, problem in zip(visible, problems):
        item = _build_list_item(path, problem)
        if item is not None:
            items.append(item)

    next_offset = (capped_offset + capped_limit) if has_more else None
    return AdminPathListResponse(items=items, next_offset=next_offset)


@router.get("/paths/{path_id}", response_model=AdminPathDetail)
async def get_path_detail(path_id: UUID, user: AdminUser) -> AdminPathDetail:
    """Full detail for one path: path row + problem + all steps +
    per-step hints + per-step mistakes + problem-scoped mistakes.

    One round-trip from the frontend's perspective; we batch the DB
    reads in parallel via asyncio.to_thread + asyncio.gather.
    """
    path = await asyncio.to_thread(repo.get_solution_path, path_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "path not found")

    problem, steps, hints_by_step, problem_scoped_mistakes = await asyncio.gather(
        asyncio.to_thread(repo.get_problem, path.problem_id),
        asyncio.to_thread(repo.get_steps_for_path, path.id),
        asyncio.to_thread(repo.get_hints_for_path, path.id),
        asyncio.to_thread(repo.get_mistakes_for_problem_only, path.problem_id),
    )
    if problem is None:
        # Should not happen given the FK; defensive return.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "path's problem row is missing",
        )

    # Per-step mistakes: one query per step.
    mistake_lists = await asyncio.gather(
        *[asyncio.to_thread(repo.get_mistakes_for_step, s.id) for s in steps]
    )
    mistakes_by_step = {
        s.id: ml for s, ml in zip(steps, mistake_lists)
    }

    return AdminPathDetail(
        path=path,
        problem=problem,
        steps=steps,
        hints_by_step=hints_by_step,
        mistakes_by_step=mistakes_by_step,
        problem_scoped_mistakes=problem_scoped_mistakes,
    )


@router.post(
    "/paths/{path_id}/verify",
    response_model=AdminPathVerifyResponse,
)
async def verify_path(
    path_id: UUID, user: AdminUser
) -> AdminPathVerifyResponse:
    """Mark a path verified=true (and stamp verified_by + verified_at).

    Idempotent: re-verifying an already-verified path just refreshes
    the timestamp and updater. Returns the new state.
    """
    existing = await asyncio.to_thread(repo.get_solution_path, path_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "path not found")
    await asyncio.to_thread(
        repo.mark_path_verified, path_id, user.user_id, verified=True
    )
    return AdminPathVerifyResponse(id=path_id, verified=True)


@router.post(
    "/paths/{path_id}/reject",
    response_model=AdminPathVerifyResponse,
)
async def reject_path(
    path_id: UUID, user: AdminUser
) -> AdminPathVerifyResponse:
    """Mark verified=false (soft-reject; row stays for audit).

    Idempotent. Decision: we keep the row so the verification queue
    history is auditable. A separate hard-delete endpoint can be added
    later if cleanup ever becomes a problem.
    """
    existing = await asyncio.to_thread(repo.get_solution_path, path_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "path not found")
    await asyncio.to_thread(
        repo.mark_path_verified, path_id, user.user_id, verified=False
    )
    return AdminPathVerifyResponse(id=path_id, verified=False)
