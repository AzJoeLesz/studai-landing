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


# ---------------------------------------------------------------------------
# Problem bank
# ---------------------------------------------------------------------------

# Languages the corpus exists in. English is the source of truth; everything
# else is a translation stored in `problem_translations`.
Language = Literal["en", "hu"]


class Problem(BaseModel):
    """A canonical math problem row (English). Translations live elsewhere."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    type: str
    difficulty: str | None = None
    problem_en: str
    solution_en: str
    answer: str | None = None
    source_id: str | None = None
    created_at: datetime


class ProblemInsert(BaseModel):
    """The payload passed to `repositories.insert_problems`. No id, no timestamps."""

    source: str
    type: str
    difficulty: str | None = None
    problem_en: str
    solution_en: str
    answer: str | None = None
    source_id: str | None = None


class ProblemSearchResult(BaseModel):
    """One hit returned by the similarity-search RPC.

    `language` is what we *actually* served — it falls back to 'en' when no
    translation exists in the requested language.
    """

    id: UUID
    source: str
    type: str
    difficulty: str | None = None
    problem: str
    solution: str
    answer: str | None = None
    language: Language
    similarity: float
