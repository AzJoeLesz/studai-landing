"""Pydantic models for database rows + API payloads.

Kept deliberately small. If/when we start needing form validation etc., we
can split these into `db/` models and `api/` models.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


Role = Literal["user", "assistant", "system", "tool"]


class TutorSession(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class Message(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    role: Role
    content: str
    created_at: datetime


class MessageInput(BaseModel):
    """A message on its way INTO the LLM (no id/timestamp yet)."""

    role: Role
    content: str
