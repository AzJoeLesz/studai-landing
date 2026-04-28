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
    EvidenceSource,
    Language,
    Message,
    PlacementAttempt,
    Problem,
    ProblemInsert,
    ProblemSearchResult,
    Profile,
    Role,
    SessionState,
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
    "share_progress_with_parents,preferences"
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
