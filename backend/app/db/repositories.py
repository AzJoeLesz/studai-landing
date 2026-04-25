"""Data access layer.

Every function takes a `user_id` where relevant and filters by it. We
cannot rely on RLS because we connect with the service_role key.

All functions are sync. FastAPI runs sync endpoints in a threadpool, so
they don't block the event loop. Async call sites (streaming) wrap calls
with `asyncio.to_thread`.
"""

from typing import Iterable
from uuid import UUID

from app.db.schemas import (
    Language,
    Message,
    Problem,
    ProblemInsert,
    ProblemSearchResult,
    Role,
    TutorSession,
)
from app.db.supabase import get_supabase_client


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
) -> int:
    """Bulk insert (problem_id, language, embedding) rows.

    Existing rows for the same (problem_id, language) are kept (idempotent
    re-runs are common). Returns the count of newly inserted rows.
    """
    payload = [
        {
            "problem_id": str(pid),
            "language": lang,
            "embedding": vec,
        }
        for pid, lang, vec in pairs
    ]
    if not payload:
        return 0
    sb = get_supabase_client()
    res = (
        sb.table("problem_embeddings")
        .upsert(payload, on_conflict="problem_id,language")
        .execute()
    )
    return len(res.data or [])


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
