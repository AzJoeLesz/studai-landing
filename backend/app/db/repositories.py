"""Data access layer.

Every function takes a `user_id` where relevant and filters by it. We
cannot rely on RLS because we connect with the service_role key.

All functions are sync. FastAPI runs sync endpoints in a threadpool, so
they don't block the event loop. Async call sites (streaming) wrap calls
with `asyncio.to_thread`.
"""

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from app.db.schemas import (
    Language,
    Message,
    Problem,
    ProblemInsert,
    ProblemSearchResult,
    Profile,
    Role,
    TeachingMaterialHit,
    TutorSession,
)
from app.db.supabase import get_supabase_client


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def get_profile(user_id: UUID) -> Profile | None:
    """Read the current student profile. Missing rows return None.

    Cheap to call on every chat turn -- it's a single indexed lookup.
    We don't cache because profiles can change at any time and the cost of
    a stale prompt context is real.
    """
    sb = get_supabase_client()
    res = (
        sb.table("profiles")
        .select(
            "id,display_name,age,grade_level,interests,learning_goals,notes"
        )
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
    return [ProblemSearchResult.model_validate(row) for row in (res.data or [])]


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
