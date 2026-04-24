"""Session CRUD endpoints.

All endpoints here authenticate the caller and enforce ownership —
no user can touch another user's sessions, even by guessing the UUID.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.db import repositories as repo
from app.db.schemas import Message, TutorSession

router = APIRouter(prefix="/sessions", tags=["sessions"])


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionRenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class SessionWithMessages(BaseModel):
    session: TutorSession
    messages: list[Message]


@router.get("", response_model=list[TutorSession])
def list_sessions(user: CurrentUser) -> list[TutorSession]:
    return repo.list_user_sessions(user.user_id)


@router.post(
    "", response_model=TutorSession, status_code=status.HTTP_201_CREATED
)
def create_session(
    payload: SessionCreateRequest, user: CurrentUser
) -> TutorSession:
    return repo.create_session(user.user_id, title=payload.title)


@router.get("/{session_id}", response_model=SessionWithMessages)
def get_session(session_id: UUID, user: CurrentUser) -> SessionWithMessages:
    session = repo.get_session_for_user(session_id, user.user_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    messages = repo.list_messages(session_id)
    return SessionWithMessages(session=session, messages=messages)


@router.patch("/{session_id}", response_model=TutorSession)
def rename_session(
    session_id: UUID,
    payload: SessionRenameRequest,
    user: CurrentUser,
) -> TutorSession:
    existing = repo.get_session_for_user(session_id, user.user_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    repo.update_session_title(session_id, payload.title)
    updated = repo.get_session_for_user(session_id, user.user_id)
    assert updated is not None  # just confirmed ownership above
    return updated


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: UUID, user: CurrentUser) -> None:
    deleted = repo.delete_session(session_id, user.user_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
