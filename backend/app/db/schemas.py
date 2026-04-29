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
    # Phase 10: gates the /admin/* routes. Default 'student' so existing
    # rows are unaffected. First admin user is set manually in Supabase
    # after migration 007 runs.
    role: str = "student"


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


# ---------------------------------------------------------------------------
# Phase 10: solution graphs
# ---------------------------------------------------------------------------
# See docs/phase10_solution_graphs.md for the design rationale and the
# full Decisions table. Free-text topic strings still apply (Phase 11
# will introduce the canonical `topics` taxonomy and migrate FKs at
# that point).

# Provenance of a `solution_paths` row.
#   * `generator`           — produced by scripts/generate_solution_paths.py
#                              from problem + solution + OpenStax retrieval.
#   * `annotation_backfill` — synthesized in 10A from the existing
#                              `problem_annotations.payload` JSON. Decision
#                              A (lock): backfill the 205 already-annotated
#                              rows as path #1 with verified=false.
PathSource = Literal["generator", "annotation_backfill"]

# Mirrors `guided_problem_sessions.status` enum check.
GuidedSessionStatus = Literal["active", "completed", "abandoned"]

# Mirrors `profiles.role` enum check.
ProfileRole = Literal["student", "parent", "teacher", "admin"]


class SolutionPath(BaseModel):
    """A named approach to solving a specific problem.

    1-3 paths per problem (factoring vs quadratic_formula vs graphing).
    `verified=true` is the runtime gate — only verified paths drive the
    guided-mode loop. Decision B (lock): no path versioning yet —
    unique-on-(problem_id, name, language) and overwrite with bumped
    `model` on regeneration.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    problem_id: UUID
    name: str
    rationale: str | None = None
    preferred: bool = False
    language: Language = "en"
    verified: bool = False
    verified_by: UUID | None = None
    verified_at: datetime | None = None
    model: str | None = None
    critic_score: float | None = None
    source: PathSource | None = None
    created_at: datetime | None = None


class SolutionPathInsert(BaseModel):
    """Payload for inserting a new `solution_paths` row."""

    problem_id: UUID
    name: str
    rationale: str | None = None
    preferred: bool = False
    language: Language = "en"
    model: str | None = None
    critic_score: float | None = None
    source: PathSource | None = None


class SolutionStep(BaseModel):
    """An ordered step inside a path.

    The step evaluator (10B) reads `goal`, `expected_action`, and
    `expected_state` to classify the student's latest message against
    the expected next move.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    path_id: UUID
    step_index: int
    goal: str
    expected_action: str | None = None
    expected_state: str | None = None
    is_terminal: bool = False
    created_at: datetime | None = None


class SolutionStepInsert(BaseModel):
    """Payload for inserting a new `solution_steps` row.

    `step_index` is required and must be unique within a path; the
    generator script assigns it sequentially from 1.
    """

    path_id: UUID
    step_index: int
    goal: str
    expected_action: str | None = None
    expected_state: str | None = None
    is_terminal: bool = False


class StepHint(BaseModel):
    """A graduated hint for a step.

    `hint_index`: 1=gentle, 2=stronger, 3=last hint before the method.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    step_id: UUID
    hint_index: int
    body: str
    created_at: datetime | None = None


class StepHintInsert(BaseModel):
    """Payload for inserting a new `step_hints` row."""

    step_id: UUID
    hint_index: int
    body: str


class CommonMistake(BaseModel):
    """A pedagogically-actionable mistake pattern.

    Either step-scoped (preferred — more actionable) or problem-scoped.
    The step evaluator's `matched_mistake_<id>` output points at one of
    these rows. `pedagogical_hint` is the response the model uses in
    spirit (rephrased, never recited verbatim).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    problem_id: UUID | None = None
    step_id: UUID | None = None
    pattern: str
    detection_hint: str | None = None
    pedagogical_hint: str
    remediation_topic: str | None = None
    created_at: datetime | None = None


class CommonMistakeInsert(BaseModel):
    """Payload for inserting a new `common_mistakes` row.

    Exactly one of `problem_id` or `step_id` should be set in practice
    (the DB constraint allows both, but step-scoped is preferred).
    """

    problem_id: UUID | None = None
    step_id: UUID | None = None
    pattern: str
    detection_hint: str | None = None
    pedagogical_hint: str
    remediation_topic: str | None = None


class GuidedProblemSession(BaseModel):
    """Per-(session, problem) runtime state for guided mode.

    Decision J (lock): while `status == 'active'`, this row is the
    authoritative source for `session_state.mode = 'problem'` and for
    `session_state.struggling_on = current_step.goal`. The post-turn
    extractor MUST defer to the guided system on those two fields.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    problem_id: UUID
    active_path_id: UUID | None = None
    current_step_index: int = 1
    attempts_on_step: int = 0
    hints_consumed_on_step: int = 0
    off_path_count: int = 0
    status: GuidedSessionStatus = "active"
    started_at: datetime | None = None
    updated_at: datetime | None = None
