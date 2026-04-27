"""Style-policy layer: profile + state + progress -> StyleDirectives.

This is the deterministic adaptation centerpiece (see
`docs/phase9_personalization.md`). The function `derive_directives` is
pure -- given the same inputs it returns the same outputs -- so we can
log, eval, and demo it cleanly.

Design rules:
  * NEVER look up the database here. Pull all reads from the agent layer.
  * NEVER make LLM calls here. This module must stay <1ms.
  * NEVER read environment variables. This is logic, not configuration.
  * The serialized `format_directives_block(...)` output is the system
    prompt block the model sees. The contract between this output and
    the v3 prompt's "How to read STYLE DIRECTIVES" section is what
    gives StudAI its visible adaptation; do not change one without the
    other.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from app.agents.grade_priors import (
    canonicalize_topic,
    expected_mastery,
    resolve_grade_band,
)
from app.db.schemas import Profile, SessionState, StudentProgress


VocabularyLevel = Literal["concrete-everyday", "concrete-mathy", "abstract-formal"]
StepSize = Literal["micro", "normal", "leap-allowed"]
PraiseFrequency = Literal["high", "medium", "sparse"]
HintTiming = Literal["early", "balanced", "late"]
ExampleFlavor = Literal["story-narrative", "visual", "pure-math", "mixed"]
Register = Literal[
    "at_level",
    "above_level_exploration",
    "below_level_warmup",
    "remedial",
]
Affect = Literal["curious-engaged", "neutral", "anxious-needs-reassurance"]


@dataclass(frozen=True)
class StyleDirectives:
    vocabulary_level: VocabularyLevel
    step_size: StepSize
    praise_frequency: PraiseFrequency
    hint_timing: HintTiming
    example_flavor: ExampleFlavor
    register: Register
    affect: Affect

    def to_prompt_lines(self) -> list[str]:
        return [
            f"- Vocabulary level: {self.vocabulary_level}",
            f"- Step size: {self.step_size}",
            f"- Praise frequency: {self.praise_frequency}",
            f"- Hint timing: {self.hint_timing}",
            f"- Example flavor: {self.example_flavor}",
            f"- Register: {self.register}",
            f"- Affect: {self.affect}",
        ]


# ---------------------------------------------------------------------------
# Defaults + helpers
# ---------------------------------------------------------------------------
_BANDS_ORDER = ["K-2", "3-5", "6-8", "9-10", "11-12", "university"]


def _band_index(band: str | None) -> int | None:
    if band is None:
        return None
    try:
        return _BANDS_ORDER.index(band)
    except ValueError:
        return None


def _vocabulary_for_band(band: str | None) -> VocabularyLevel:
    idx = _band_index(band)
    if idx is None:
        return "concrete-mathy"
    if idx <= 1:        # K-2, 3-5
        return "concrete-everyday"
    if idx <= 3:        # 6-8, 9-10
        return "concrete-mathy"
    return "abstract-formal"


def _baseline_step_size(band: str | None) -> StepSize:
    idx = _band_index(band)
    if idx is None:
        return "normal"
    if idx <= 0:        # K-2
        return "micro"
    if idx <= 2:        # 3-5, 6-8
        return "normal"
    return "leap-allowed"


# ---------------------------------------------------------------------------
# Personality survey (Phase 9C) -> partial directives
# ---------------------------------------------------------------------------
# preferences shape (documented in db/schemas.py::Profile):
#   {
#     "hint_style":     "fast_hints" | "figure_out" | "worked_example",
#     "math_affect":    "curious"    | "neutral"    | "anxious",
#     "example_flavor": "story"      | "pure"       | "visual"
#   }


def _hint_timing_from_pref(pref: str | None) -> HintTiming | None:
    if pref == "fast_hints":
        return "early"
    if pref == "figure_out":
        return "late"
    if pref == "worked_example":
        return "balanced"
    return None


def _affect_from_pref(pref: str | None) -> Affect | None:
    if pref == "curious":
        return "curious-engaged"
    if pref == "anxious":
        return "anxious-needs-reassurance"
    if pref == "neutral":
        return "neutral"
    return None


def _example_flavor_from_pref(pref: str | None) -> ExampleFlavor | None:
    if pref == "story":
        return "story-narrative"
    if pref == "visual":
        return "visual"
    if pref == "pure":
        return "pure-math"
    return None


# ---------------------------------------------------------------------------
# Register: topic-grade alignment
# ---------------------------------------------------------------------------
# Empirically-tuned thresholds; revisit once we have enough placement-quiz
# data to ground them.
_ABOVE_LEVEL_PRIOR_THRESHOLD = 0.05   # below this -> above-level
_BELOW_LEVEL_PRIOR_THRESHOLD = 0.95   # above this -> below-level
_REMEDIAL_MASTERY_THRESHOLD = 0.20    # mastery this low on a band-appropriate topic


def _register_for(
    *,
    current_topic: str | None,
    curriculum: str | None,
    band: str | None,
    progress_for_topic: StudentProgress | None,
) -> Register:
    """Decide the conversational register from topic vs grade vs mastery.

    The "3rd grader asks about quadratics" rule lives here.
    """
    if not current_topic:
        return "at_level"
    canon = canonicalize_topic(current_topic)
    if not canon:
        return "at_level"
    prior = expected_mastery(canon, curriculum, band)
    if prior <= _ABOVE_LEVEL_PRIOR_THRESHOLD:
        return "above_level_exploration"
    if prior >= _BELOW_LEVEL_PRIOR_THRESHOLD:
        return "below_level_warmup"
    if (
        progress_for_topic is not None
        and progress_for_topic.mastery_score < _REMEDIAL_MASTERY_THRESHOLD
    ):
        return "remedial"
    return "at_level"


# ---------------------------------------------------------------------------
# Main: derive_directives
# ---------------------------------------------------------------------------
def derive_directives(
    *,
    profile: Profile | None,
    session_state: SessionState | None,
    top_progress: list[StudentProgress] | None = None,
) -> StyleDirectives:
    """Pure function: profile + state + progress -> StyleDirectives.

    `top_progress` should be the student's most recently-touched topics.
    The function looks up the row for `session_state.current_topic`
    among them when deciding `register`.
    """
    grade_level = profile.grade_level if profile else None
    age = profile.age if profile else None
    preferences = (profile.preferences if profile else None) or {}

    resolved = resolve_grade_band(grade_level)
    curriculum = resolved[0] if resolved else None
    band = resolved[1] if resolved else None
    if band is None and age is not None:
        # Fallback: rough age->band table when the user typed a freeform
        # grade we couldn't match but did fill in age.
        if age <= 7:
            band = "K-2"
        elif age <= 10:
            band = "3-5"
        elif age <= 13:
            band = "6-8"
        elif age <= 15:
            band = "9-10"
        elif age <= 18:
            band = "11-12"
        else:
            band = "university"
        # Default to US curriculum when we only have age.
        curriculum = curriculum or "us_ccss"

    vocabulary_level: VocabularyLevel = _vocabulary_for_band(band)
    step_size: StepSize = _baseline_step_size(band)

    # ---- praise/hint/affect from personality -----------------------------
    praise_frequency: PraiseFrequency = "medium"
    hint_timing: HintTiming = (
        _hint_timing_from_pref(preferences.get("hint_style")) or "balanced"
    )
    affect: Affect = (
        _affect_from_pref(preferences.get("math_affect")) or "neutral"
    )
    example_flavor: ExampleFlavor = (
        _example_flavor_from_pref(preferences.get("example_flavor")) or "mixed"
    )

    if affect == "anxious-needs-reassurance":
        praise_frequency = "high"
        # Smaller steps for anxious students help confidence.
        if step_size == "leap-allowed":
            step_size = "normal"
        elif step_size == "normal":
            step_size = "micro"
    elif affect == "curious-engaged":
        praise_frequency = "medium"

    # Younger learners get more praise by default regardless of personality.
    if band in ("K-2", "3-5"):
        praise_frequency = "high"

    # ---- session-state adjustments ---------------------------------------
    if session_state is not None:
        if (session_state.attempts_count or 0) >= 4:
            # If they've attempted a lot in this session, they're in deep
            # work mode -- don't pad with micro-checks they don't need.
            if step_size == "micro":
                step_size = "normal"
        if session_state.struggling_on:
            # If they're stuck, reduce step size and surface hints sooner.
            if step_size == "leap-allowed":
                step_size = "normal"
            elif step_size == "normal":
                step_size = "micro"
            if hint_timing == "late":
                hint_timing = "balanced"
            elif hint_timing == "balanced":
                hint_timing = "early"

    # ---- register (topic-grade alignment) --------------------------------
    current_topic = (
        session_state.current_topic if session_state is not None else None
    )
    progress_for_topic: StudentProgress | None = None
    if current_topic and top_progress:
        canon = canonicalize_topic(current_topic)
        progress_for_topic = next(
            (p for p in top_progress if canonicalize_topic(p.topic) == canon),
            None,
        )

    register: Register = _register_for(
        current_topic=current_topic,
        curriculum=curriculum,
        band=band,
        progress_for_topic=progress_for_topic,
    )

    return StyleDirectives(
        vocabulary_level=vocabulary_level,
        step_size=step_size,
        praise_frequency=praise_frequency,
        hint_timing=hint_timing,
        example_flavor=example_flavor,
        register=register,
        affect=affect,
    )


# ---------------------------------------------------------------------------
# Prompt block formatting
# ---------------------------------------------------------------------------
def format_directives_block(directives: StyleDirectives) -> str:
    lines = [
        "STYLE DIRECTIVES (private — follow exactly, do not recite):",
    ]
    lines.extend(directives.to_prompt_lines())
    return "\n".join(lines)


def format_progress_block(
    rows: list[StudentProgress] | None, *, max_rows: int = 5
) -> str | None:
    """Compact STUDENT PROGRESS block for the system context.

    Shown topics: top `max_rows` by `last_seen_at`. Mastery is rounded
    to one decimal. Skipped entirely if there are no rows.
    """
    if not rows:
        return None
    rows_sorted = sorted(
        rows, key=lambda r: (r.last_seen_at or 0), reverse=True
    )[:max_rows]
    if not rows_sorted:
        return None
    body_lines = [
        "STUDENT PROGRESS (private — silently calibrate to these. Don't recite.):",
    ]
    for r in rows_sorted:
        body_lines.append(
            f"  - {r.topic} -> mastery {r.mastery_score:.1f} "
            f"(source={r.evidence_source}, n={r.evidence_count})"
        )
    return "\n".join(body_lines)


def format_session_state_block(state: SessionState | None) -> str | None:
    """SESSION STATE block. None if there's nothing useful to say yet."""
    if state is None:
        return None
    parts: list[str] = []
    if state.current_topic:
        parts.append(f"current_topic: {state.current_topic}")
    if state.mode:
        parts.append(f"mode: {state.mode}")
    if state.attempts_count:
        parts.append(f"attempts_so_far: {state.attempts_count}")
    if state.struggling_on:
        parts.append(f"struggling_on: {state.struggling_on}")
    if state.mood_signals:
        parts.append(f"mood_signals: {state.mood_signals}")
    if state.summary:
        parts.append(
            "summary_so_far: " + state.summary.strip()[:1500]
        )
    if not parts:
        return None
    return (
        "SESSION STATE (private — running snapshot. Use silently.):\n  "
        + "\n  ".join(parts)
    )


__all__ = [
    "StyleDirectives",
    "derive_directives",
    "format_directives_block",
    "format_progress_block",
    "format_session_state_block",
]


# Tiny convenience for logging / debugging eval fixtures.
def to_dict(d: StyleDirectives) -> dict:
    return asdict(d)
