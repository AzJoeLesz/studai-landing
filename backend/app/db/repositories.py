"""Data access layer.

Every function takes a `user_id` where relevant and filters by it. We
cannot rely on RLS because we connect with the service_role key.

All functions are sync. FastAPI runs sync endpoints in a threadpool, so
they don't block the event loop. Async call sites (streaming) wrap calls
with `asyncio.to_thread`.
"""

from uuid import UUID

from app.db.schemas import Message, Role, TutorSession
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
