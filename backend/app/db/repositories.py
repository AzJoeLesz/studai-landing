"""Data access layer.

Every function takes a `user_id` where relevant and filters by it. We
cannot rely on RLS because we connect with the service_role key.

All functions are sync. FastAPI runs sync endpoints in a threadpool, so
they don't block the event loop. Async call sites (streaming) wrap calls
with `asyncio.to_thread`.
"""

import logging
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from pydantic import ValidationError

from app.db.schemas import (
    CommonMistake,
    CommonMistakeInsert,
    EvidenceSource,
    GuidedProblemSession,
    GuidedSessionStatus,
    Language,
    Message,
    PlacementAttempt,
    Problem,
    ProblemInsert,
    ProblemSearchResult,
    Profile,
    Role,
    SessionState,
    SolutionPath,
    SolutionPathInsert,
    SolutionStep,
    SolutionStepInsert,
    StepHint,
    StepHintInsert,
    StudentProgress,
    TeachingMaterialHit,
    TutorSession,
)
from app.db.supabase import get_supabase_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
_PROFILE_COLUMNS = (
    "id,display_name,age,grade_level,interests,learning_goals,notes,"
    "share_progress_with_parents,preferences,role"
)


def get_profile(user_id: UUID) -> Profile | None:
    """Read the current student profile. Missing rows return None.

    Cheap to call on every chat turn -- it's a single indexed lookup.
    We don't cache because profiles can change at any time and the cost of
    a stale prompt context is real.
    """
    sb = get_supabase_client()
    res = (
        sb.table("profiles")
        .select(_PROFILE_COLUMNS)
        .eq("id", str(user_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return Profile.model_validate(rows[0]) if rows else None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def list_user_sessions(user_id: UUID, limit: int = 100) -> list[TutorSession]:
    sb = get_supabase_client()
    res = (
        sb.table("tutor_sessions")
        .select("*")
        .eq("user_id", str(user_id))
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [TutorSession.model_validate(row) for row in (res.data or [])]


def create_session(user_id: UUID, title: str | None = None) -> TutorSession:
    sb = get_supabase_client()
    res = (
        sb.table("tutor_sessions")
        .insert({"user_id": str(user_id), "title": title})
        .execute()
    )
    if not res.data:
        raise RuntimeError("Failed to create session")
    return TutorSession.model_validate(res.data[0])


def get_session_for_user(
    session_id: UUID, user_id: UUID
) -> TutorSession | None:
    sb = get_supabase_client()
    res = (
        sb.table("tutor_sessions")
        .select("*")
        .eq("id", str(session_id))
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return TutorSession.model_validate(rows[0]) if rows else None


def delete_session(session_id: UUID, user_id: UUID) -> bool:
    sb = get_supabase_client()
    res = (
        sb.table("tutor_sessions")
        .delete()
        .eq("id", str(session_id))
        .eq("user_id", str(user_id))
        .execute()
    )
    return bool(res.data)


def update_session_title(session_id: UUID, title: str) -> None:
    sb = get_supabase_client()
    sb.table("tutor_sessions").update({"title": title}).eq(
        "id", str(session_id)
    ).execute()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def list_messages(session_id: UUID) -> list[Message]:
    sb = get_supabase_client()
    res = (
        sb.table("messages")
        .select("*")
        .eq("session_id", str(session_id))
        .order("created_at")
        .execute()
    )
    return [Message.model_validate(row) for row in (res.data or [])]


def append_message(session_id: UUID, role: Role, content: str) -> Message:
    sb = get_supabase_client()
    res = (
        sb.table("messages")
        .insert(
            {
                "session_id": str(session_id),
                "role": role,
                "content": content,
            }
        )
        .execute()
    )
    if not res.data:
        raise RuntimeError("Failed to insert message")
    return Message.model_validate(res.data[0])


# ---------------------------------------------------------------------------
# Problem bank
# ---------------------------------------------------------------------------
def upsert_problems(rows: Iterable[ProblemInsert]) -> list[Problem]:
    """Insert problems with ON CONFLICT (source, source_id) DO NOTHING semantics.

    Supabase's `upsert` matches on the primary key by default; we need it to
    match on the unique `(source, source_id)` constraint instead, which we
    achieve via the `on_conflict` parameter. Returns the inserted/updated rows.
    """
    payload = [r.model_dump(exclude_none=True) for r in rows]
    if not payload:
        return []
    sb = get_supabase_client()
    res = (
        sb.table("problems")
        .upsert(payload, on_conflict="source,source_id")
        .execute()
    )
    return [Problem.model_validate(row) for row in (res.data or [])]


def list_problems_missing_embedding(
    language: Language, limit: int = 1000
) -> list[Problem]:
    """Return problems that don't yet have an embedding for `language`.

    Calls the `problems_without_embedding()` Postgres function (added in
    migration 003a). Doing this as a server-side LEFT JOIN avoids
    sending tens of thousands of UUIDs back through the URL, which
    exploded with `NOT IN (...)` once we passed ~1000 embedded rows.
    """
    sb = get_supabase_client()
    res = sb.rpc(
        "problems_without_embedding",
        {"target_language": language, "max_count": limit},
    ).execute()
    return [Problem.model_validate(row) for row in (res.data or [])]


def insert_embeddings(
    pairs: Iterable[tuple[UUID, Language, list[float]]],
    *,
    chunk_size: int = 100,
) -> int:
    """Bulk upsert (problem_id, language, embedding) rows.

    Chunks the upsert into batches of `chunk_size` because each row is a
    1536-dim vector (~6 KB). Sending 1000 rows in one HTTP request was
    pushing Supabase past its 8s statement timeout (Postgres error 57014).
    100-row chunks finish well under 1s each.

    Existing rows for the same (problem_id, language) are kept (idempotent
    re-runs are common). Returns the count of rows attempted -- supabase-py's
    upsert response does not always include rows that were UPDATEd on
    conflict (vs INSERTed), so counting `res.data` undercounts. Since each
    chunk either fully succeeds or raises, total-attempted is the right
    metric here.
    """
    sb = get_supabase_client()
    total_attempted = 0
    buffer: list[dict] = []
    for pid, lang, vec in pairs:
        buffer.append(
            {
                "problem_id": str(pid),
                "language": lang,
                "embedding": vec,
            }
        )
        if len(buffer) >= chunk_size:
            (
                sb.table("problem_embeddings")
                .upsert(buffer, on_conflict="problem_id,language")
                .execute()
            )
            total_attempted += len(buffer)
            buffer.clear()

    if buffer:
        (
            sb.table("problem_embeddings")
            .upsert(buffer, on_conflict="problem_id,language")
            .execute()
        )
        total_attempted += len(buffer)

    return total_attempted


def search_problems(
    query_embedding: list[float],
    language: Language,
    *,
    match_count: int = 10,
    filter_type: str | None = None,
    filter_difficulty: str | None = None,
) -> list[ProblemSearchResult]:
    """Top-k nearest problems by cosine similarity, with optional filters.

    Calls the `match_problems()` Postgres function defined in migration 003.
    The function handles language fallback: if no translation exists for the
    requested language, the English problem text is returned (and `language`
    in the result reflects what was actually served).
    """
    sb = get_supabase_client()
    res = sb.rpc(
        "match_problems",
        {
            "query_embedding": query_embedding,
            "match_language": language,
            "match_count": match_count,
            "filter_type": filter_type,
            "filter_difficulty": filter_difficulty,
        },
    ).execute()
    out: list[ProblemSearchResult] = []
    for row in res.data or []:
        if not row.get("problem"):
            continue
        try:
            out.append(ProblemSearchResult.model_validate(row))
        except ValidationError:
            logger.debug("search_problems: skip invalid row %s", row.get("id"))
    return out


def upsert_translations(
    rows: Iterable[tuple[UUID, Language, str, str]],
) -> int:
    """Bulk upsert (problem_id, language, problem_text, solution_text)."""
    payload = [
        {
            "problem_id": str(pid),
            "language": lang,
            "problem_text": problem_text,
            "solution_text": solution_text,
        }
        for pid, lang, problem_text, solution_text in rows
    ]
    if not payload:
        return 0
    sb = get_supabase_client()
    res = (
        sb.table("problem_translations")
        .upsert(payload, on_conflict="problem_id,language")
        .execute()
    )
    return len(res.data or [])


# ---------------------------------------------------------------------------
# Teaching material (OpenStax chunks) + problem annotations
# ---------------------------------------------------------------------------
def delete_teaching_chunks_for_book(source: str, book_slug: str) -> None:
    """Remove all chunks (and cascaded embeddings) for one book. Re-ingest use."""
    sb = get_supabase_client()
    (
        sb.table("teaching_material_chunks")
        .delete()
        .eq("source", source)
        .eq("book_slug", book_slug)
        .execute()
    )


def upsert_teaching_chunks(
    rows: list[dict],
) -> list[dict]:
    """Bulk upsert chunk rows. Each dict: source, book_slug, chunk_index, page_start, page_end, body."""
    if not rows:
        return []
    sb = get_supabase_client()
    res = (
        sb.table("teaching_material_chunks")
        .upsert(rows, on_conflict="source,book_slug,chunk_index")
        .execute()
    )
    return res.data or []


def list_teaching_chunks_missing_embedding(
    book_slug: str | None, limit: int = 2000
) -> list[dict]:
    """Chunks with no row in teaching_material_embeddings."""
    sb = get_supabase_client()
    if book_slug:
        res = sb.rpc(
            "teaching_chunks_without_embedding_for_book",
            {"p_book_slug": book_slug, "max_count": limit},
        ).execute()
    else:
        res = sb.rpc(
            "teaching_chunks_without_embedding",
            {"max_count": limit},
        ).execute()
    return res.data or []


def list_problems_without_annotations(limit: int = 500) -> list[Problem]:
    """Batch of problems with no `problem_annotations` row (RPC)."""
    sb = get_supabase_client()
    res = sb.rpc("problems_without_annotations", {"max_count": limit}).execute()
    return [Problem.model_validate(row) for row in (res.data or [])]


def insert_teaching_embeddings(
    pairs: Iterable[tuple[UUID, list[float]]],
    *,
    chunk_size: int = 100,
) -> int:
    """Upsert (chunk_id, embedding) for teaching material."""
    sb = get_supabase_client()
    total_attempted = 0
    buffer: list[dict] = []
    for cid, vec in pairs:
        buffer.append({"chunk_id": str(cid), "embedding": vec})
        if len(buffer) >= chunk_size:
            (
                sb.table("teaching_material_embeddings")
                .upsert(buffer, on_conflict="chunk_id")
                .execute()
            )
            total_attempted += len(buffer)
            buffer.clear()
    if buffer:
        (
            sb.table("teaching_material_embeddings")
            .upsert(buffer, on_conflict="chunk_id")
            .execute()
        )
        total_attempted += len(buffer)
    return total_attempted


def search_teaching_material(
    query_embedding: list[float],
    *,
    match_count: int = 8,
) -> list[TeachingMaterialHit]:
    """Top-k OpenStax chunks by cosine similarity."""
    sb = get_supabase_client()
    res = sb.rpc(
        "match_teaching_material",
        {
            "query_embedding": query_embedding,
            "match_count": match_count,
        },
    ).execute()
    return [TeachingMaterialHit.model_validate(row) for row in (res.data or [])]


def get_annotations_for_problem_ids(
    problem_ids: list[UUID],
) -> dict[UUID, dict]:
    """Return payload JSON keyed by problem_id. Missing rows omitted."""
    if not problem_ids:
        return {}
    sb = get_supabase_client()
    res = (
        sb.table("problem_annotations")
        .select("problem_id, payload, model")
        .in_("problem_id", [str(p) for p in problem_ids])
        .execute()
    )
    out: dict[UUID, dict] = {}
    for row in res.data or []:
        out[UUID(row["problem_id"])] = {
            "payload": row["payload"],
            "model": row.get("model"),
        }
    return out


def upsert_problem_annotation(
    problem_id: UUID, payload: dict, model: str | None
) -> None:
    sb = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("problem_annotations").upsert(
        {
            "problem_id": str(problem_id),
            "payload": payload,
            "model": model,
            "updated_at": now,
        },
        on_conflict="problem_id",
    ).execute()


# ---------------------------------------------------------------------------
# Session state (Phase 9A)
# ---------------------------------------------------------------------------
def get_session_state(session_id: UUID) -> SessionState | None:
    """Read the per-session structured snapshot, or None if not yet written."""
    sb = get_supabase_client()
    res = (
        sb.table("session_state")
        .select("*")
        .eq("session_id", str(session_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return SessionState.model_validate(rows[0]) if rows else None


def upsert_session_state(
    session_id: UUID,
    *,
    current_topic: str | None = None,
    mode: str | None = None,
    attempts_count: int | None = None,
    struggling_on: str | None = None,
    mood_signals: dict | None = None,
    summary: str | None = None,
) -> None:
    """Idempotent upsert. Pass only fields that should change.

    The post-turn extractor calls this with whatever it managed to derive;
    None values mean "leave whatever was there alone". We achieve that by
    fetching the existing row first and merging.
    """
    sb = get_supabase_client()
    existing = get_session_state(session_id)
    payload: dict = {
        "session_id": str(session_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    def _merge(field: str, new_val, existing_val):
        if new_val is not None:
            payload[field] = new_val
        elif existing_val is not None:
            payload[field] = existing_val

    _merge(
        "current_topic",
        current_topic,
        existing.current_topic if existing else None,
    )
    _merge("mode", mode, existing.mode if existing else None)
    _merge(
        "attempts_count",
        attempts_count,
        existing.attempts_count if existing else 0,
    )
    _merge(
        "struggling_on",
        struggling_on,
        existing.struggling_on if existing else None,
    )
    _merge(
        "mood_signals",
        mood_signals,
        existing.mood_signals if existing else None,
    )
    _merge("summary", summary, existing.summary if existing else None)

    sb.table("session_state").upsert(
        payload, on_conflict="session_id"
    ).execute()


def increment_session_attempts(session_id: UUID) -> None:
    """Atomic-ish bump for attempts_count.

    `supabase-py` doesn't expose a server-side increment, so we read-then-
    write. Acceptable here because session_state is a single-writer
    resource per session (one extractor task per turn).
    """
    existing = get_session_state(session_id)
    next_count = (existing.attempts_count if existing else 0) + 1
    upsert_session_state(session_id, attempts_count=next_count)


# ---------------------------------------------------------------------------
# Student progress (Phase 9A scaffolding; 9D installs the real BKT update)
# ---------------------------------------------------------------------------
def get_top_progress(
    user_id: UUID, *, limit: int = 8
) -> list[StudentProgress]:
    """Most-recent topics for the student, descending by `last_seen_at`.

    Used to inject a compact STUDENT PROGRESS block into the tutor prompt.
    """
    sb = get_supabase_client()
    res = (
        sb.table("student_progress")
        .select("*")
        .eq("user_id", str(user_id))
        .order("last_seen_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [StudentProgress.model_validate(r) for r in (res.data or [])]


def get_progress_for_topic(
    user_id: UUID, topic: str
) -> StudentProgress | None:
    sb = get_supabase_client()
    res = (
        sb.table("student_progress")
        .select("*")
        .eq("user_id", str(user_id))
        .eq("topic", topic)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return StudentProgress.model_validate(rows[0]) if rows else None


def upsert_progress(
    user_id: UUID,
    topic: str,
    *,
    mastery_score: float,
    evidence_source: EvidenceSource,
    evidence_count: int | None = None,
) -> None:
    """Write a (possibly new) progress row for one topic.

    Caller is responsible for computing `mastery_score` (Phase 9D's
    `agents/mastery.py` does this via BKT-IDEM). This repo just persists.
    """
    sb = get_supabase_client()
    payload: dict = {
        "user_id": str(user_id),
        "topic": topic,
        "mastery_score": max(0.0, min(1.0, float(mastery_score))),
        "evidence_source": evidence_source,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    if evidence_count is not None:
        payload["evidence_count"] = evidence_count
    sb.table("student_progress").upsert(
        payload, on_conflict="user_id,topic"
    ).execute()


def bulk_seed_progress(
    rows: Iterable[tuple[UUID, str, float]],
    *,
    evidence_source: EvidenceSource = "prior",
) -> int:
    """Bulk insert grade-priors-derived rows for a fresh user.

    Used by the `/onboarding/seed-priors` endpoint after the student
    submits their grade level for the first time. Won't overwrite rows
    that already exist (on-conflict do-nothing semantics: we send the
    same source key, which is fine to dedupe on later if needed).

    For now we do upsert with the same source, which means re-running
    seed will refresh `last_seen_at` but not clobber a higher mastery
    that came from a placement quiz or extractor since.
    """
    payload = []
    now = datetime.now(timezone.utc).isoformat()
    for user_id, topic, mastery in rows:
        payload.append(
            {
                "user_id": str(user_id),
                "topic": topic,
                "mastery_score": max(0.0, min(1.0, float(mastery))),
                "evidence_source": evidence_source,
                "last_seen_at": now,
            }
        )
    if not payload:
        return 0
    sb = get_supabase_client()
    # ignore_duplicates so a re-seed for the same user is a no-op rather
    # than a regression of mastery built up since.
    sb.table("student_progress").upsert(
        payload, on_conflict="user_id,topic", ignore_duplicates=True
    ).execute()
    return len(payload)


# ---------------------------------------------------------------------------
# Placement quiz attempts (Phase 9E)
# ---------------------------------------------------------------------------
def record_placement_attempt(attempt: PlacementAttempt) -> PlacementAttempt:
    sb = get_supabase_client()
    payload = {
        "user_id": str(attempt.user_id),
        "problem_id": str(attempt.problem_id),
        "topic": attempt.topic,
        "difficulty": attempt.difficulty,
        "correct": attempt.correct,
    }
    res = sb.table("placement_attempts").insert(payload).execute()
    if not res.data:
        raise RuntimeError("Failed to record placement attempt")
    return PlacementAttempt.model_validate(res.data[0])


def list_placement_attempts(
    user_id: UUID, *, limit: int = 20
) -> list[PlacementAttempt]:
    sb = get_supabase_client()
    res = (
        sb.table("placement_attempts")
        .select("*")
        .eq("user_id", str(user_id))
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return [PlacementAttempt.model_validate(r) for r in (res.data or [])]


# Source allowlist for the placement quiz.
#
# The full corpus is a mix: hendrycks (Olympiad/contest math, hard at
# every "Level"), gsm8k (clean grade-school word problems), asdiv +
# svamp (synthetic template-generated -- frequent nonsense). For the
# placement quiz the *active* source list is computed PER GRADE BAND
# (see `agents.grade_priors.placement_profile_for_band`) -- a 4th
# grader gets gsm8k only, a university student gets hendrycks. The
# constant below is just a reasonable default for callers that don't
# have a band yet.
#
# Live tutoring RAG (`agents/retrieval.find_relevant_problems`) keeps
# pulling from the full corpus -- there the problems are PRIVATE
# context, not shown verbatim to the student.
_PLACEMENT_DEFAULT_SOURCES = ("hendrycks", "gsm8k", "openstax")

# Length sanity: too-short problems are usually parse errors; too-long
# problems are usually long proofs that don't fit a 5-question quiz UX.
_PLACEMENT_MIN_LEN = 30
_PLACEMENT_MAX_LEN = 600


def fetch_problem_for_placement(
    *,
    sources: list[str] | None = None,
    exclude_ids: list[UUID],
    filter_difficulties: list[str] | None = None,
) -> Problem | None:
    """Pick any allowlisted problem matching one of `filter_difficulties`.

    Used as the last-resort fallback when topic-aware semantic search
    returns nothing.

    `sources` -- the source-name allowlist for THIS placement turn.
    Defaults to `_PLACEMENT_DEFAULT_SOURCES` when omitted. Per-band
    callers (the only realistic ones) should always pass an explicit
    list from `agents.grade_priors.placement_profile_for_band`.

    `filter_difficulties=None` (or `[]`) skips the difficulty filter.
    Pass a list -- e.g. ``["Level 1", "Level 2", "easy"]`` -- to allow
    any of them, since different datasets use different difficulty
    vocabularies. See `agents.mastery.corpus_difficulties_for`.
    """
    src_list = list(sources) if sources else list(_PLACEMENT_DEFAULT_SOURCES)
    if not src_list:
        return None  # caller passed an empty list explicitly -- nothing to fetch
    sb = get_supabase_client()
    q = sb.table("problems").select(
        "id,source,type,difficulty,problem_en,solution_en,answer,source_id,"
        "created_at"
    )
    q = q.in_("source", src_list)
    if filter_difficulties:
        q = q.in_("difficulty", filter_difficulties)
    if exclude_ids:
        q = q.not_.in_("id", [str(i) for i in exclude_ids])
    # Pull a few candidates so we can length-filter; first survivor wins.
    res = q.limit(40).execute()
    for row in res.data or []:
        body = row.get("problem_en") or ""
        if _PLACEMENT_MIN_LEN <= len(body) <= _PLACEMENT_MAX_LEN:
            return Problem.model_validate(row)
    # Nothing in length window -- relax the length filter rather than
    # show nothing at all.
    rows = res.data or []
    return Problem.model_validate(rows[0]) if rows else None


def fetch_problem_for_placement_by_ids(
    candidate_ids: list[UUID],
    *,
    sources: list[str] | None = None,
    exclude_ids: list[UUID],
    filter_difficulties: list[str] | None = None,
) -> Problem | None:
    """Pick the first acceptable problem from a ranked candidate ID list.

    The IDs come from semantic topic search
    (``agents/retrieval.find_relevant_problems`` against the topic name).
    We then enforce the source list + difficulty list + length sanity +
    the excluded-IDs set on top, *while preserving the semantic-search
    rank order*.

    Use `fetch_problems_for_placement_by_ids` (plural) when the caller
    wants the top-N list to feed into a downstream reranker.
    """
    candidates = fetch_problems_for_placement_by_ids(
        candidate_ids,
        sources=sources,
        exclude_ids=exclude_ids,
        filter_difficulties=filter_difficulties,
        limit=1,
    )
    return candidates[0] if candidates else None


def fetch_problems_for_placement_by_ids(
    candidate_ids: list[UUID],
    *,
    sources: list[str] | None = None,
    exclude_ids: list[UUID],
    filter_difficulties: list[str] | None = None,
    limit: int = 15,
) -> list[Problem]:
    """List variant of `fetch_problem_for_placement_by_ids`.

    Returns up to `limit` candidate problems that pass all filters,
    preserving semantic-search rank order. Used by the placement-quiz
    LLM reranker (`api/onboarding._rerank_placement_candidates`)
    which then picks the single best for the requested topic + band.

    A length-window outlier is included only as a last-resort fallback
    so the caller never gets back an empty list when there *is* a
    candidate (just possibly a too-short or too-long one).
    """
    if not candidate_ids:
        return []
    src_list = list(sources) if sources else list(_PLACEMENT_DEFAULT_SOURCES)
    if not src_list:
        return []
    exclude_str = {str(i) for i in exclude_ids}
    keep = [str(i) for i in candidate_ids if str(i) not in exclude_str]
    if not keep:
        return []
    sb = get_supabase_client()
    q = (
        sb.table("problems")
        .select(
            "id,source,type,difficulty,problem_en,solution_en,answer,"
            "source_id,created_at"
        )
        .in_("id", keep)
        .in_("source", src_list)
    )
    if filter_difficulties:
        q = q.in_("difficulty", filter_difficulties)
    rows = q.execute().data or []
    # Preserve the semantic-search ranking order.
    rank = {pid: i for i, pid in enumerate(keep)}
    rows.sort(key=lambda r: rank.get(str(r["id"]), 1_000_000))
    out: list[Problem] = []
    for row in rows:
        body = row.get("problem_en") or ""
        if _PLACEMENT_MIN_LEN <= len(body) <= _PLACEMENT_MAX_LEN:
            out.append(Problem.model_validate(row))
            if len(out) >= limit:
                return out
    if out:
        return out
    # Length window excluded everything -- relax it as a last resort
    # so the caller still has SOMETHING ranked over no result.
    if rows:
        return [Problem.model_validate(rows[0])]
    return []


# ---------------------------------------------------------------------------
# Phase 10: solution graphs
# ---------------------------------------------------------------------------
# All four content tables (`solution_paths`, `solution_steps`,
# `step_hints`, `common_mistakes`) are PUBLIC content -- read by all
# authenticated users, written only via service_role from the generation
# script and the /admin/paths endpoints.
#
# `guided_problem_sessions` is per-(session, problem) runtime state and
# is read on every chat turn while a guided session is active.
# Decision J: this row is the authoritative state-holder for
# session_state.mode and session_state.struggling_on while active.
#
# Topic FKs are deferred to Phase 11; for now `common_mistakes.remediation_topic`
# is free text. See docs/phase10_solution_graphs.md.
_SOLUTION_PATH_COLUMNS = (
    "id,problem_id,name,rationale,preferred,language,verified,"
    "verified_by,verified_at,model,critic_score,source,created_at"
)
_SOLUTION_STEP_COLUMNS = (
    "id,path_id,step_index,goal,expected_action,expected_state,"
    "is_terminal,created_at"
)
_STEP_HINT_COLUMNS = "id,step_id,hint_index,body,created_at"
_COMMON_MISTAKE_COLUMNS = (
    "id,problem_id,step_id,pattern,detection_hint,pedagogical_hint,"
    "remediation_topic,created_at"
)
_GUIDED_SESSION_COLUMNS = (
    "id,session_id,problem_id,active_path_id,current_step_index,"
    "attempts_on_step,hints_consumed_on_step,off_path_count,status,"
    "started_at,updated_at"
)


# --- Generator support: which problems still need paths --------------------
def list_problems_without_solution_paths(
    target_language: Language = "en", limit: int = 500
) -> list[Problem]:
    """Problems without ANY `solution_paths` row in `target_language`.

    Calls the `problems_without_solution_paths()` Postgres function
    from migration 007. Server-side LEFT JOIN; same pattern as
    `list_problems_missing_embedding`.
    """
    sb = get_supabase_client()
    res = sb.rpc(
        "problems_without_solution_paths",
        {"target_language": target_language, "max_count": limit},
    ).execute()
    return [Problem.model_validate(row) for row in (res.data or [])]


def list_annotated_problems_without_solution_paths(
    target_language: Language = "en", limit: int = 500
) -> list[Problem]:
    """Problems with an existing `problem_annotations` row but no
    `solution_paths` yet. Used by the generator's `--from-annotations`
    mode to backfill the 205 already-annotated problems with richer
    input scaffolding (Decision A in docs/phase10_solution_graphs.md).
    """
    sb = get_supabase_client()
    res = sb.rpc(
        "annotated_problems_without_solution_paths",
        {"target_language": target_language, "max_count": limit},
    ).execute()
    return [Problem.model_validate(row) for row in (res.data or [])]


def list_problems_filtered(
    *,
    sources: list[str] | None = None,
    difficulties: list[str] | None = None,
    types: list[str] | None = None,
    exclude_ids: list[UUID] | None = None,
    only_without_paths_in_language: Language | None = None,
    limit: int = 200,
) -> list[Problem]:
    """General problem-bank filter. Used by the band-corpus orchestrator
    (Phase 10E) when topic-search ANN doesn't have a centroid for a
    topic, or when a caller just wants `(source, difficulty, type)` slices.

    Differs from `fetch_problem_for_placement(...)` (which is
    placement-quiz-flavored: defaults to a placement-source allowlist
    + length window + 40-row cap). This function applies NO defaults
    -- pass `sources=None` for "any source", `difficulties=None` for
    "any difficulty", etc. Pass `only_without_paths_in_language='en'`
    to skip problems that already have a solution_paths row in that
    language (idempotent re-runs of the orchestrator).
    """
    sb = get_supabase_client()
    q = sb.table("problems").select(
        "id,source,type,difficulty,problem_en,solution_en,answer,"
        "source_id,created_at"
    )
    if sources:
        q = q.in_("source", list(sources))
    if difficulties:
        q = q.in_("difficulty", list(difficulties))
    if types:
        q = q.in_("type", list(types))
    if exclude_ids:
        q = q.not_.in_("id", [str(i) for i in exclude_ids])
    safe_limit = max(1, min(2000, limit))
    res = q.limit(safe_limit).execute()
    rows = res.data or []
    if not rows:
        return []
    out = [Problem.model_validate(r) for r in rows]
    if only_without_paths_in_language:
        # Filter out anything that already has a path in this language.
        # One IN-query is cheaper than per-row RTTs.
        ids = [str(p.id) for p in out]
        existing = (
            sb.table("solution_paths")
            .select("problem_id")
            .in_("problem_id", ids)
            .eq("language", only_without_paths_in_language)
            .execute()
        )
        already = {row["problem_id"] for row in (existing.data or [])}
        out = [p for p in out if str(p.id) not in already]
    return out


def problem_ids_with_paths(
    problem_ids: list[UUID] | list[str],
    *,
    language: Language = "en",
) -> set[str]:
    """Subset of `problem_ids` that already have a `solution_paths` row
    in `language`. Used by the band-corpus orchestrator to skip
    already-generated problems on re-runs (idempotent).
    """
    if not problem_ids:
        return set()
    ids = [str(i) for i in problem_ids]
    sb = get_supabase_client()
    res = (
        sb.table("solution_paths")
        .select("problem_id")
        .in_("problem_id", ids)
        .eq("language", language)
        .execute()
    )
    return {row["problem_id"] for row in (res.data or [])}


def fetch_problems_by_ids(
    candidate_ids: list[UUID],
    *,
    sources: list[str] | None = None,
    difficulties: list[str] | None = None,
    types: list[str] | None = None,
    exclude_ids: list[UUID] | None = None,
    limit: int = 50,
    preserve_rank: bool = True,
) -> list[Problem]:
    """Filter a ranked list of problem ids by source/difficulty/type.

    Used by the band-corpus orchestrator when it has a semantic-search
    ranking from `repo.search_problems(topic_embedding, ...)` and wants
    to keep the rank order while applying corpus filters. Mirrors
    `fetch_problems_for_placement_by_ids` but without placement
    defaults (no source allowlist, no length window).
    """
    if not candidate_ids:
        return []
    exclude_str = {str(i) for i in (exclude_ids or [])}
    keep = [str(i) for i in candidate_ids if str(i) not in exclude_str]
    if not keep:
        return []
    sb = get_supabase_client()
    q = (
        sb.table("problems")
        .select(
            "id,source,type,difficulty,problem_en,solution_en,answer,"
            "source_id,created_at"
        )
        .in_("id", keep)
    )
    if sources:
        q = q.in_("source", list(sources))
    if difficulties:
        q = q.in_("difficulty", list(difficulties))
    if types:
        q = q.in_("type", list(types))
    rows = q.execute().data or []
    if not rows:
        return []
    if preserve_rank:
        rank = {pid: i for i, pid in enumerate(keep)}
        rows.sort(key=lambda r: rank.get(str(r["id"]), 1_000_000))
    out: list[Problem] = []
    for row in rows:
        out.append(Problem.model_validate(row))
        if len(out) >= limit:
            break
    return out


# --- Solution paths --------------------------------------------------------
def insert_solution_path(row: SolutionPathInsert) -> SolutionPath:
    """Upsert a single solution path; conflicts on (problem_id, name, language).

    Re-running the generator with the same path name overwrites the
    existing row (and its `model`/`critic_score`). Cascades from the
    path delete all child rows (steps, hints, mistakes-via-step).
    Decision B: no path versioning yet.
    """
    sb = get_supabase_client()
    payload = row.model_dump(exclude_none=True, mode="json")
    res = (
        sb.table("solution_paths")
        .upsert(payload, on_conflict="problem_id,name,language")
        .execute()
    )
    if not res.data:
        raise RuntimeError("Failed to upsert solution_path")
    return SolutionPath.model_validate(res.data[0])


def get_paths_for_problem(
    problem_id: UUID,
    *,
    language: Language = "en",
    verified_only: bool = False,
) -> list[SolutionPath]:
    """All paths for a problem, ordered with `preferred=true` first.

    `verified_only=True` is the runtime gate (Decision F): only verified
    paths drive guided mode. The /admin/paths route uses
    `verified_only=False` to surface unverified content for review.
    """
    sb = get_supabase_client()
    q = (
        sb.table("solution_paths")
        .select(_SOLUTION_PATH_COLUMNS)
        .eq("problem_id", str(problem_id))
        .eq("language", language)
    )
    if verified_only:
        q = q.eq("verified", True)
    res = q.execute()
    rows = res.data or []
    rows.sort(
        key=lambda r: (
            0 if r.get("preferred") else 1,
            r.get("created_at") or "",
        )
    )
    return [SolutionPath.model_validate(r) for r in rows]


def get_solution_path(path_id: UUID) -> SolutionPath | None:
    sb = get_supabase_client()
    res = (
        sb.table("solution_paths")
        .select(_SOLUTION_PATH_COLUMNS)
        .eq("id", str(path_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return SolutionPath.model_validate(rows[0]) if rows else None


def get_problem(problem_id: UUID) -> Problem | None:
    """Read one problem row by id. Used by /admin/paths and by
    `agents/guided_mode` (which previously inlined this query).
    """
    sb = get_supabase_client()
    res = (
        sb.table("problems")
        .select(
            "id,source,type,difficulty,problem_en,solution_en,answer,"
            "source_id,created_at"
        )
        .eq("id", str(problem_id))
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return Problem.model_validate(rows[0]) if rows else None


def list_unverified_paths(limit: int = 50) -> list[SolutionPath]:
    """Verification queue for /admin/paths (legacy non-paginated form).

    Sorted by `critic_score desc nulls last` then `created_at desc` so
    the LLM-as-judge's high-confidence paths bubble to the top. Phase
    10C's UI uses `list_admin_paths` instead (paginated + filterable);
    this function is kept for any non-UI caller that just needs a
    quick batch.
    """
    return list_admin_paths(
        status_filter="unverified", limit=limit, offset=0
    )


def list_admin_paths(
    *,
    status_filter: str = "unverified",
    limit: int = 25,
    offset: int = 0,
) -> list[SolutionPath]:
    """Paginated + filterable path list for /admin/paths.

    `status_filter` -- "unverified" | "verified" | "all".
    Same sort as `list_unverified_paths`: critic_score desc nulls
    last, then created_at desc as a tie-breaker.
    """
    sb = get_supabase_client()
    q = sb.table("solution_paths").select(_SOLUTION_PATH_COLUMNS)
    if status_filter == "unverified":
        q = q.eq("verified", False)
    elif status_filter == "verified":
        q = q.eq("verified", True)
    # "all" applies no filter.
    safe_limit = max(1, min(100, limit))
    safe_offset = max(0, offset)
    res = (
        q.order("critic_score", desc=True, nullsfirst=False)
        .order("created_at", desc=True)
        .range(safe_offset, safe_offset + safe_limit - 1)
        .execute()
    )
    return [SolutionPath.model_validate(r) for r in (res.data or [])]


def mark_path_verified(
    path_id: UUID, verified_by: UUID, *, verified: bool = True
) -> None:
    """Set `verified` (and `verified_by`/`verified_at`) on a path.

    The /admin/paths route calls this through a backend endpoint; the
    backend uses the service_role key so RLS is bypassed -- the
    endpoint itself enforces `profiles.role == 'admin'`.
    """
    sb = get_supabase_client()
    payload: dict = {"verified": verified}
    if verified:
        payload["verified_by"] = str(verified_by)
        payload["verified_at"] = datetime.now(timezone.utc).isoformat()
    else:
        payload["verified_by"] = None
        payload["verified_at"] = None
    sb.table("solution_paths").update(payload).eq(
        "id", str(path_id)
    ).execute()


def delete_path(path_id: UUID) -> None:
    """Hard delete a path (cascades to steps, hints, mistakes-via-step).

    Used by the generator's `--overwrite` flag to wipe a stale path
    before re-inserting fresh rows. Verification UI uses
    `mark_path_verified(verified=False)` for soft-rejects instead.
    """
    sb = get_supabase_client()
    sb.table("solution_paths").delete().eq("id", str(path_id)).execute()


# --- Solution steps --------------------------------------------------------
def bulk_insert_steps(
    rows: Iterable[SolutionStepInsert],
) -> list[SolutionStep]:
    """Bulk insert ordered steps for one (or several) paths."""
    payload = [r.model_dump(exclude_none=True, mode="json") for r in rows]
    if not payload:
        return []
    sb = get_supabase_client()
    res = (
        sb.table("solution_steps")
        .upsert(payload, on_conflict="path_id,step_index")
        .execute()
    )
    return [SolutionStep.model_validate(r) for r in (res.data or [])]


def get_steps_for_path(path_id: UUID) -> list[SolutionStep]:
    sb = get_supabase_client()
    res = (
        sb.table("solution_steps")
        .select(_SOLUTION_STEP_COLUMNS)
        .eq("path_id", str(path_id))
        .order("step_index")
        .execute()
    )
    return [SolutionStep.model_validate(r) for r in (res.data or [])]


# --- Step hints ------------------------------------------------------------
def bulk_insert_hints(
    rows: Iterable[StepHintInsert],
) -> list[StepHint]:
    payload = [r.model_dump(exclude_none=True, mode="json") for r in rows]
    if not payload:
        return []
    sb = get_supabase_client()
    res = (
        sb.table("step_hints")
        .upsert(payload, on_conflict="step_id,hint_index")
        .execute()
    )
    return [StepHint.model_validate(r) for r in (res.data or [])]


def get_hints_for_step(step_id: UUID) -> list[StepHint]:
    sb = get_supabase_client()
    res = (
        sb.table("step_hints")
        .select(_STEP_HINT_COLUMNS)
        .eq("step_id", str(step_id))
        .order("hint_index")
        .execute()
    )
    return [StepHint.model_validate(r) for r in (res.data or [])]


def get_hints_for_path(path_id: UUID) -> dict[UUID, list[StepHint]]:
    """Hints grouped by step_id for an entire path. Single round-trip."""
    sb = get_supabase_client()
    step_ids = [str(s.id) for s in get_steps_for_path(path_id)]
    if not step_ids:
        return {}
    res = (
        sb.table("step_hints")
        .select(_STEP_HINT_COLUMNS)
        .in_("step_id", step_ids)
        .order("hint_index")
        .execute()
    )
    out: dict[UUID, list[StepHint]] = {}
    for r in res.data or []:
        h = StepHint.model_validate(r)
        out.setdefault(h.step_id, []).append(h)
    return out


# --- Common mistakes -------------------------------------------------------
def bulk_insert_mistakes(
    rows: Iterable[CommonMistakeInsert],
) -> list[CommonMistake]:
    """Bulk insert mistake rows.

    No upsert: `common_mistakes` has no natural unique key (the same
    pattern can plausibly attach to multiple steps). Re-running the
    generator with `--overwrite` first deletes the parent path, which
    cascades to step-scoped mistakes.
    """
    payload = [r.model_dump(exclude_none=True, mode="json") for r in rows]
    if not payload:
        return []
    sb = get_supabase_client()
    res = sb.table("common_mistakes").insert(payload).execute()
    return [CommonMistake.model_validate(r) for r in (res.data or [])]


def get_mistakes_for_step(step_id: UUID) -> list[CommonMistake]:
    sb = get_supabase_client()
    res = (
        sb.table("common_mistakes")
        .select(_COMMON_MISTAKE_COLUMNS)
        .eq("step_id", str(step_id))
        .execute()
    )
    return [CommonMistake.model_validate(r) for r in (res.data or [])]


def get_mistakes_for_problem_only(
    problem_id: UUID,
) -> list[CommonMistake]:
    """Just the problem-scoped mistake rows (NOT the union with
    step-scoped). Used by /admin/paths so the detail view can show
    "this mistake spans the whole problem" separately from
    "this mistake is tied to step N".
    """
    sb = get_supabase_client()
    res = (
        sb.table("common_mistakes")
        .select(_COMMON_MISTAKE_COLUMNS)
        .eq("problem_id", str(problem_id))
        .execute()
    )
    return [CommonMistake.model_validate(r) for r in (res.data or [])]


def get_mistakes_for_problem(problem_id: UUID) -> list[CommonMistake]:
    """Both step-scoped (via the problem's paths' steps) and problem-scoped
    mistake rows. The step evaluator (10B) needs the union -- the
    student may match a mistake that's attached to a step we haven't
    even reached yet (e.g. they jumped ahead).
    """
    sb = get_supabase_client()
    # Per-problem direct hits.
    direct = (
        sb.table("common_mistakes")
        .select(_COMMON_MISTAKE_COLUMNS)
        .eq("problem_id", str(problem_id))
        .execute()
        .data
        or []
    )
    # Per-step hits across all paths for this problem. Two queries beats
    # a complex join; supabase-py doesn't expose the latter cleanly.
    paths = (
        sb.table("solution_paths")
        .select("id")
        .eq("problem_id", str(problem_id))
        .execute()
        .data
        or []
    )
    if not paths:
        return [CommonMistake.model_validate(r) for r in direct]
    path_ids = [p["id"] for p in paths]
    steps = (
        sb.table("solution_steps")
        .select("id")
        .in_("path_id", path_ids)
        .execute()
        .data
        or []
    )
    if not steps:
        return [CommonMistake.model_validate(r) for r in direct]
    step_ids = [s["id"] for s in steps]
    via_steps = (
        sb.table("common_mistakes")
        .select(_COMMON_MISTAKE_COLUMNS)
        .in_("step_id", step_ids)
        .execute()
        .data
        or []
    )
    seen: set[str] = set()
    out: list[CommonMistake] = []
    for r in direct + via_steps:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(CommonMistake.model_validate(r))
    return out


# --- Guided problem sessions (runtime state) -------------------------------
class _Unset:
    """Sentinel: distinguishes 'leave alone' from 'set to None' in
    `update_guided_session.active_path_id`. The argument default is
    `_UNSET`; callers either omit the arg (leave alone), pass `None`
    (explicit clear), or pass a UUID (set).
    """


_UNSET = _Unset()


def get_active_guided_session(
    session_id: UUID,
) -> GuidedProblemSession | None:
    """The single active guided session for this tutor session, if any.

    Decision J: if this returns non-None, the post-turn extractor MUST
    skip writing `mode` and `struggling_on` to `session_state`. Read
    on every chat turn; the partial index `guided_problem_sessions_active_idx`
    keeps it cheap.
    """
    sb = get_supabase_client()
    res = (
        sb.table("guided_problem_sessions")
        .select(_GUIDED_SESSION_COLUMNS)
        .eq("session_id", str(session_id))
        .eq("status", "active")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return GuidedProblemSession.model_validate(rows[0]) if rows else None


def get_or_start_guided_session(
    *,
    session_id: UUID,
    problem_id: UUID,
    active_path_id: UUID | None,
) -> GuidedProblemSession:
    """Find-or-create the (session, problem) row and return it.

    Existing rows are returned untouched -- their counters keep their
    state across chat turns. The `unique(session_id, problem_id)`
    constraint makes this safe under retry. If the existing row is
    `completed` or `abandoned`, we reactivate it (set status='active'
    and reset the per-step counters) -- the student is coming back to
    a problem they earlier walked away from.
    """
    sb = get_supabase_client()
    existing_res = (
        sb.table("guided_problem_sessions")
        .select(_GUIDED_SESSION_COLUMNS)
        .eq("session_id", str(session_id))
        .eq("problem_id", str(problem_id))
        .limit(1)
        .execute()
    )
    rows = existing_res.data or []
    if rows:
        row = rows[0]
        if row["status"] == "active":
            return GuidedProblemSession.model_validate(row)
        # Reactivate.
        upd = (
            sb.table("guided_problem_sessions")
            .update(
                {
                    "status": "active",
                    "active_path_id": (
                        str(active_path_id) if active_path_id else row.get("active_path_id")
                    ),
                    "current_step_index": 1,
                    "attempts_on_step": 0,
                    "hints_consumed_on_step": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", row["id"])
            .execute()
        )
        return GuidedProblemSession.model_validate(
            (upd.data or [row])[0]
        )

    # Fresh insert.
    payload = {
        "session_id": str(session_id),
        "problem_id": str(problem_id),
        "active_path_id": str(active_path_id) if active_path_id else None,
        "current_step_index": 1,
        "attempts_on_step": 0,
        "hints_consumed_on_step": 0,
        "off_path_count": 0,
        "status": "active",
    }
    res = sb.table("guided_problem_sessions").insert(payload).execute()
    if not res.data:
        raise RuntimeError("Failed to start guided_problem_session")
    return GuidedProblemSession.model_validate(res.data[0])


def update_guided_session(
    guided_id: UUID,
    *,
    active_path_id: UUID | None | _Unset = _UNSET,
    current_step_index: int | None = None,
    attempts_on_step: int | None = None,
    hints_consumed_on_step: int | None = None,
    off_path_count: int | None = None,
    status: GuidedSessionStatus | None = None,
) -> None:
    """Patch any subset of guided-session counters.

    The 10B step-evaluator + state-machine layer does the math (decide
    whether to bump current_step_index, reset attempts, etc.); this
    function just persists the result. `active_path_id` uses `_UNSET`
    as its default so callers can distinguish "leave alone" (omit
    arg) from "explicitly clear" (pass None).
    """
    payload: dict = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if not isinstance(active_path_id, _Unset):
        payload["active_path_id"] = (
            str(active_path_id) if active_path_id is not None else None
        )
    if current_step_index is not None:
        payload["current_step_index"] = current_step_index
    if attempts_on_step is not None:
        payload["attempts_on_step"] = attempts_on_step
    if hints_consumed_on_step is not None:
        payload["hints_consumed_on_step"] = hints_consumed_on_step
    if off_path_count is not None:
        payload["off_path_count"] = off_path_count
    if status is not None:
        payload["status"] = status

    sb = get_supabase_client()
    sb.table("guided_problem_sessions").update(payload).eq(
        "id", str(guided_id)
    ).execute()
