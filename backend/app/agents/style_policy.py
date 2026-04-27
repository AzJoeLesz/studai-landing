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
    resolve_grade_band,
    topic_band_status,
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
# Empirically-tuned threshold; revisit once we have enough placement-quiz
# data to ground it.
_REMEDIAL_MASTERY_THRESHOLD = 0.20  # mastery this low on a band-appropriate topic


def _register_for(
    *,
    current_topic: str | None,
    curriculum: str | None,
    band: str | None,
    progress_for_topic: StudentProgress | None,
) -> Register:
    """Decide the conversational register from topic vs grade vs mastery.

    Uses `topic_band_status` to distinguish "above level" (topic
    appears only in higher bands) from "below level" (topic appears
    only in lower bands). The previous implementation used a prior
    threshold which conflated the two -- a 12th grader asking "what
    is 7 + 8?" was wrongly classified as `above_level_exploration`
    because addition isn't in the 11-12 priors. Now it correctly
    becomes `below_level_warmup`.
    """
    if not current_topic:
        return "at_level"
    canon = canonicalize_topic(current_topic)
    if not canon:
        return "at_level"
    status = topic_band_status(canon, curriculum, band)
    if status == "above_level":
        return "above_level_exploration"
    if status == "below_level":
        return "below_level_warmup"
    # at_level (or unknown) -- maybe remedial if mastery on this topic
    # is unusually low for someone at this band.
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
    live_topic: str | None = None,
) -> StyleDirectives:
    """Pure function: profile + state + progress -> StyleDirectives.

    `top_progress` should be the student's most recently-touched topics.
    The function looks up the row for the current topic among them when
    deciding `register`.

    `live_topic` is the topic classifier's call on the current user
    message. When provided it OVERRIDES `session_state.current_topic`
    for the register check -- the post-turn extractor only writes
    session_state *after* the previous turn, so on turn 1 of a fresh
    session `current_topic` is still None and the register would
    incorrectly default to `at_level`. The live classification fixes
    that.
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
    # Prefer the live classification: the post-turn extractor writes
    # session_state.current_topic AFTER the turn, so on turn 1 it's None.
    # Without `live_topic`, the register would default to at_level on
    # the first message of any new session -- exactly the moment a 4th
    # grader is most likely to ask "what are parabolas?" and most needs
    # the above_level_exploration register.
    state_topic = (
        session_state.current_topic if session_state is not None else None
    )
    current_topic = live_topic or state_topic
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

# When the register is anything other than `at_level`, we INLINE the
# strict recipe into the directives block instead of relying on the
# model to remember the corresponding section in the system prompt.
# Recency bias on long prompts (especially for gpt-4o-mini) means the
# model tends to follow instructions placed close to the user message
# more reliably than instructions buried earlier. Without the inline
# recipe, a 4th grader who explicitly asks "I want to do math about
# parabolas" gets served `y = ax^2 + bx + c` because the strict
# recipe is 6000+ tokens upstream and the immediate "do math" request
# wins.
#
# See `prompts/tutor_v3.txt` for the canonical, fuller version of these
# recipes -- the inline copies below are tightened versions of those
# rules, intentionally short so they fit at the top of attention.
# Vocabulary inline recipes. We only inline the strict variant
# (`concrete-everyday`) -- the others (`concrete-mathy`,
# `abstract-formal`) match the model's defaults and don't need
# extra reinforcement. Without the inline recipe, gpt-5-mini
# routinely smuggles letter variables ($n$, $x$) into K-5 chat
# replies because v3's definition of `concrete-everyday` is 13k
# characters upstream by the time it gets to the user message.
_VOCAB_RECIPES: dict[VocabularyLevel, str] = {
    "concrete-everyday": (
        "VOCABULARY RULES — concrete-everyday (this student is young, "
        "K-5). FOLLOW EXACTLY:\n"
        "  * NO letter variables. Never use `n`, `x`, `y`, etc. to "
        "stand for an unknown. Use the concrete numbers from the "
        "problem.\n"
        "  * NO algebraic framing. Avoid 'the largest n such that...', "
        "'let x be the number of...', 'if we call the unknown n...'. "
        "Speak in plain English about the actual things in the problem.\n"
        "  * Math jargon (factor, coefficient, equation, expression) "
        "only when explained in everyday words on first use.\n"
        "  * Reach for everyday objects: LEGOs, pizza slices, bags of "
        "candy, friends sharing snacks.\n"
        "  * Reply length: 2-3 short sentences MAX. NO monologue setup.\n"
        "  * If the student has just brought a NEW problem (this is your "
        "FIRST reply on it), do PHASE 1 — DIAGNOSTIC: warm "
        "acknowledgement + ONE open question to find out where they are. "
        "Examples: 'How would you start?', 'What do you already know "
        "that might help?', 'What do we know about the chocolate bars "
        "in this problem?'. DO NOT name the operation. DO NOT mention "
        "specific numbers. DO NOT hint at the method. Just ask and wait."
    ),
}


_INLINE_RECIPES: dict[Register, str] = {
    "above_level_exploration": (
        "REGISTER RULES — above_level_exploration (the topic is well\n"
        "above this student's grade). FOLLOW EXACTLY:\n"
        "  * You are a friendly explainer here, NOT a tutor solving "
        "problems.\n"
        "  * Validate the curiosity warmly and give ONE concrete\n"
        "    intuition in plain English (everyday analogies — a thrown\n"
        "    ball, a bridge, a slide). 3-5 sentences MAX for the whole\n"
        "    reply.\n"
        "  * NEVER write equations with variables (no `y = ax^2 + bx + c`,\n"
        "    no `h(t)`, no `f(x)`). NEVER use generic constants `a`, `b`,\n"
        "    `c`, `n`. Concrete numbers in plain English only, sparingly.\n"
        "  * NEVER pose a practice problem, even when the student\n"
        "    explicitly asks for one. If the student says 'let's solve\n"
        "    a problem', 'I want to do math', 'give me a problem', or\n"
        "    similar -- you MUST decline kindly and redirect:\n"
        "        'That kind of problem is something we usually start in\n"
        "         high school, because it needs a few pieces you'll\n"
        "         learn first. Want me to show you what those pieces\n"
        "         look like? Or we can do a problem from your grade\n"
        "         right now.'\n"
        "  * NEVER ask Socratic / 'your turn' questions. Offer-style\n"
        "    questions ('want to hear more about X?') are fine."
    ),
    "below_level_warmup": (
        "REGISTER RULES — below_level_warmup (the topic is well below\n"
        "this student's grade). Treat as quick review or playful\n"
        "warm-up. Do not dwell. Answer concisely without re-teaching\n"
        "from scratch."
    ),
    "remedial": (
        "REGISTER RULES — remedial (the student is missing\n"
        "prerequisite knowledge). Be patient and warm; no shame; fill\n"
        "in the gap before proceeding. Use micro steps."
    ),
}


def format_directives_block(directives: StyleDirectives) -> str:
    lines = [
        "STYLE DIRECTIVES (private — follow exactly, do not recite):",
    ]
    lines.extend(directives.to_prompt_lines())
    # Inline the vocab recipe BEFORE the register recipe so that the
    # register rules (the more specialized of the two) end up closer
    # to the user message and win attention ties.
    vocab_recipe = _VOCAB_RECIPES.get(directives.vocabulary_level)
    if vocab_recipe:
        lines.append("")
        lines.append(vocab_recipe)
    register_recipe = _INLINE_RECIPES.get(directives.register)
    if register_recipe:
        lines.append("")
        lines.append(register_recipe)
    return "\n".join(lines)


def should_suppress_grounding(directives: StyleDirectives) -> bool:
    """Whether to drop the RAG layers (problem bank / OpenStax / annotations)
    for this turn.

    When the register is non-default the student isn't being TUTORED
    on this topic -- they're just exploring (above), warming up
    (below), or being remediated. The retrieved problem-bank items
    and textbook excerpts are at the LEVEL OF THE TOPIC, not the
    student. Showing the model a Hendrycks Level-2 quadratic worked
    solution while telling it "talk to a 4th grader at intuition
    level only" puts the model in conflict -- it tends to mirror the
    private context.

    So: when register is above_level_exploration or below_level_warmup,
    skip the RAG injection. Remedial keeps it (the student SHOULD be
    learning this topic; we're just slowing down).
    """
    return directives.register in (
        "above_level_exploration",
        "below_level_warmup",
    )


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
    "should_suppress_grounding",
]


# Tiny convenience for logging / debugging eval fixtures.
def to_dict(d: StyleDirectives) -> dict:
    return asdict(d)
