"""Pydantic models for database rows + API payloads.

Kept deliberately small. If/when we start needing form validation etc., we
can split these into `db/` models and `api/` models.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


Role = Literal["user", "assistant", "system", "tool"]

# Tutoring mode the post-turn extractor labels each session with. Mirrors
# the four classes the v2/v3 system prompt classifies on its own. `lesson`
# is reserved for Phase 11 but added now so the column constraint covers it.
TutorMode = Literal[
    "problem", "concept", "verification", "conversational", "lesson"
]

# Source of evidence for a student_progress update. Used to weight noisy
# vs. clean signals in the BKT-IDEM update (see agents/mastery.py).
EvidenceSource = Literal[
    "prior", "placement", "extractor", "rating", "step_check"
]


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
# Student profile
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    """A student profile row.

    All personalization fields are optional. The tutor can work with
    nothing -- it just becomes more personal as the student fills more in.

    Phase 9 added:
      * `share_progress_with_parents` -- consent flag the parent dashboard
        in Phase 13 will read before showing any session content.
      * `preferences` -- jsonb container for the personality micro-survey
        (9C). Documented shape:
            {
              "hint_style": "fast_hints" | "figure_out" | "worked_example",
              "math_affect": "curious" | "neutral" | "anxious",
              "example_flavor": "story" | "pure" | "visual"
            }
        Missing keys are treated as "no opinion" by style_policy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str | None = None
    age: int | None = None
    grade_level: str | None = None
    interests: str | None = None
    learning_goals: str | None = None
    notes: str | None = None
    share_progress_with_parents: bool = False
    preferences: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Phase 9: session state + student progress
# ---------------------------------------------------------------------------


class SessionState(BaseModel):
    """Per-session structured snapshot.

    Written by the post-turn extractor (`agents/state_updater.py`) after
    every assistant reply. Read by the tutor on each turn and injected
    into the system prompt as a private block.
    """

    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    current_topic: str | None = None
    mode: TutorMode | None = None
    attempts_count: int = 0
    struggling_on: str | None = None
    mood_signals: dict = Field(default_factory=dict)
    summary: str | None = None
    updated_at: datetime | None = None


class SessionStateUpdate(BaseModel):
    """The shape the post-turn extractor's LLM is asked to produce."""

    current_topic: str | None = None
    mode: TutorMode | None = None
    struggling_on: str | None = None
    mood_signals: dict = Field(default_factory=dict)
    summary_delta: str | None = None
    mastery_signals: list["MasterySignal"] = Field(default_factory=list)


class MasterySignal(BaseModel):
    """One signal from the post-turn extractor about how a topic went."""

    topic: str
    # +1.0 = very confident in correct understanding
    # -1.0 = clear misconception or wrong answer
    delta: float


SessionStateUpdate.model_rebuild()


class StudentProgress(BaseModel):
    """A row in `student_progress` -- per-(user, topic) mastery."""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    topic: str
    mastery_score: float = 0.5
    evidence_count: int = 0
    evidence_source: EvidenceSource = "prior"
    last_seen_at: datetime | None = None


class PlacementAttempt(BaseModel):
    """A single answer in the optional onboarding placement quiz."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    user_id: UUID
    problem_id: UUID
    topic: str
    difficulty: str
    correct: bool
    created_at: datetime | None = None


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


class TeachingMaterialHit(BaseModel):
    """One OpenStax chunk from `match_teaching_material()`."""

    id: UUID
    source: str
    book_slug: str
    page_start: int
    page_end: int
    body: str
    similarity: float


class ProblemAnnotationRecord(BaseModel):
    """A row in `problem_annotations` (pedagogy JSON for one problem)."""

    model_config = ConfigDict(from_attributes=True)

    problem_id: UUID
    payload: dict
    model: str | None = None
