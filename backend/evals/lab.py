"""StudAI eval lab — a tiny LLM-as-judge framework for tutor prompts.

Why this exists:
  Every prompt change is a bet. Without measurement you can't tell if v3
  is actually better than v2 or just feels like it on the cases you tried.
  This module lets us define a corpus of test cases + pedagogical rubrics
  and score any prompt against them in seconds.

How it works:
  1. Load a YAML file of test cases (each is a fake conversation + tags +
     which rubrics apply).
  2. For each case, call the LLM with the system prompt under test +
     the case's conversation. Capture the assistant reply.
  3. For each rubric on the case, ask a *judge LLM* to score the reply
     against a structured rubric prompt. Get back {score, reason}.
  4. Aggregate: per-rubric averages, per-case scores, low scorers.

Conventions:
  - Scores are 0.0–1.0. `null` means N/A (rubric doesn't apply to this case).
  - Higher is always better.
  - Rubrics may have weights (default 1.0) used for the weighted aggregate.
  - Adding a rubric is a one-line `Rubric(...)` literal at the bottom of
    `RUBRICS`. Adding a test case is a YAML entry. No framework code edits.

Cost: with 30 cases × ~5 rubrics each × gpt-4o-mini judge ~= €0.03 per run.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template
from openai import AsyncOpenAI


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class Case:
    id: str
    description: str
    conversation: list[Turn]
    rubrics: list[str]
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Score:
    score: float | None  # None = N/A
    reason: str


@dataclass
class Rubric:
    """A single evaluator. Either LLM-as-judge or a pure pattern check."""

    name: str
    weight: float
    description: str
    # For LLM-as-judge rubrics: a Jinja template producing the judge prompt.
    # The template has access to: case, response.
    judge_template: str | None = None
    # For pattern rubrics: a function (response, case) -> Score
    pattern_fn: Any = None  # Callable[[str, Case], Score] | None


@dataclass
class CaseResult:
    case: Case
    response: str
    scores: dict[str, Score]  # rubric name -> Score
    elapsed_ms: int


@dataclass
class RunResult:
    prompt_label: str
    model: str
    cases: list[CaseResult]
    total_elapsed_ms: int


# ===========================================================================
# Rubrics — research-grounded set
# ===========================================================================
#
# Sources:
#   * Maurya et al. 2025 "Unifying AI Tutor Evaluation" (NAACL) — MRBench's
#     8-dimension taxonomy: Mistake Identification, Mistake Location,
#     Revealing of the Answer, Providing Guidance, Actionability, Coherence,
#     Tutor Tone, Humanlikeness.
#   * Macina et al. 2025 "MathTutorBench" (EMNLP) — 4 pedagogical principles:
#     (a) correctness, (b) scaffolding instead of revealing, (c) encourage
#     self-correction, (d) don't overload student.
#   * Vail et al. 2016 EDM — inference questions ("how would you...?")
#     correlate with learning gains; evaluative questions ("do you understand?")
#     don't help novices.
#   * VanLehn et al. 2007 — model, scaffold, fade: hints calibrated to
#     student's evident competence.
#   * Graesser et al. 1998 AutoTutor — discrete dialogue moves; ONE move
#     per turn principle.
#
# Rubric grouping (used in reports):
#   A. Mistake handling   1-2     conditional on student making a mistake
#   B. Solution disclosure  3      core anti-pattern: revealing the answer
#   C. Guidance quality    4-8    what good guidance looks like
#   D. Conversation       9        coherence with prior turns
#   E. Affective         10-12     tone, humanness, sycophancy
#   F. Style / form     13-16     language, age, topic, LaTeX
#   G. Correctness        17       no false math
#
# All judge prompts follow the MathTutorBench "extended" pattern:
# enumerate criteria explicitly + tell the judge to penalize specific
# anti-patterns. The paper showed this beats simple judge prompts by 10+pp.
#
# Output contract (every judge): strict JSON
#   {"score": <float 0..1 or null>, "reason": "<one short sentence>"}
# `null` means N/A (rubric doesn't apply to this turn — e.g. mistake_located
# when there's no mistake). The lab treats nulls as "not counted" in averages.
# ---------------------------------------------------------------------------


_JUDGE_OUTPUT = (
    'Output ONLY a JSON object: '
    '{"score": <float between 0 and 1, or null if not applicable>, '
    '"reason": "<one short sentence justifying the score>"}'
)


# ---------- A. Mistake handling -------------------------------------------
MISTAKE_IDENTIFIED_TEMPLATE = """You are evaluating whether a math tutor recognized that the student made a mistake.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

The student DID make a mistake in this turn. Evaluate whether the tutor recognized it.

Score:
  1.0  Tutor clearly recognized the student made a mistake (does not affirm the wrong answer).
  0.5  Tutor's reply hints there's a problem but is vague or evasive.
  0.0  Tutor confirmed / praised the wrong answer, or moved on as if it were correct.

The worst failure mode is empty praise of an incorrect answer, e.g. "Great job!" + restating the wrong number. Score 0.0 in that case.

""" + _JUDGE_OUTPUT


MISTAKE_LOCATED_TEMPLATE = """You are evaluating whether a math tutor pinpointed WHERE in the student's reasoning the mistake is.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

The student made a mistake at a specific step. Did the tutor target that exact step?

Score:
  1.0  Tutor pointed to the specific step / claim that was wrong, even indirectly through a question about it.
  0.5  Tutor noticed something is off but didn't isolate which step.
  0.0  Tutor is generic ("your answer is wrong, try again") or wrong about which step is the issue.

""" + _JUDGE_OUTPUT


# ---------- B. Solution disclosure ----------------------------------------
DOES_NOT_REVEAL_ANSWER_TEMPLATE = """You are evaluating whether a math tutor REVEALED the final answer to the student.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

A good tutor scaffolds the student toward the answer rather than computing or stating it. Revealing the answer (or the calculation that yields it directly) is the most common failure of LLM tutors.

Score:
  1.0  Reply did NOT state the final numeric answer or the complete decisive calculation. Used questions, hints, or partial scaffolds.
  0.7  Reply mostly scaffolded but leaked a non-trivial chunk of the calculation.
  0.3  Reply gave most of the solution; only a final arithmetic step was left to the student.
  0.0  Reply explicitly stated the final answer, or did the entire calculation.

Special cases:
  - If the student is NOT working on a problem (greeting, off-topic, conceptual lesson with no specific problem) → output {"score": null, "reason": "no answer to reveal in this turn"}.
  - If the student EXPLICITLY insists on the direct answer after the tutor already tried scaffolding (e.g. "just tell me, please"), do not penalize giving the answer.

""" + _JUDGE_OUTPUT


# ---------- C. Guidance quality -------------------------------------------
PROVIDES_GUIDANCE_TEMPLATE = """You are evaluating whether a math tutor's reply provides any substantive guidance.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

"Substantive guidance" means: a hint, a guiding question, an example, a definition, a strategy suggestion, or pointing at relevant prior knowledge. Vague reassurance ("good question, keep trying") does NOT count.

Score:
  1.0  Reply contains real guidance the student can use.
  0.5  Reply offers acknowledgement plus a token nudge, but mostly empty.
  0.0  Reply is acknowledgement only ("I see", "ok", "try again") with no actual help.

""" + _JUDGE_OUTPUT


ACTIONABLE_GUIDANCE_TEMPLATE = """You are evaluating whether a math tutor's guidance is actionable.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Actionable means: the student knows specifically what to do or think about next. "Think about this more carefully" is NOT actionable. "What happens to both sides if you subtract 5?" IS actionable.

Score:
  1.0  Student can plausibly take the next step from this reply alone.
  0.5  Reply gestures at a direction but the student would still need to guess what to actually do.
  0.0  Reply is too vague to act on, or contradicts itself.

If the reply provides no guidance at all, output {"score": null, "reason": "no guidance to assess actionability of"} — that's covered by the `provides_guidance` rubric.

""" + _JUDGE_OUTPUT


SINGLE_FOCUSED_MOVE_TEMPLATE = """You are evaluating whether a math tutor made a SINGLE focused pedagogical move per turn.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Cognitive-load research (and AutoTutor work) shows that one question or one hint per turn is more effective than dumping multiple. Three consecutive questions overwhelm; one targeted question scaffolds.

Score:
  1.0  Exactly one focused question OR one focused hint per turn.
  0.5  Two related moves (e.g. a brief acknowledgement + one question) — borderline acceptable.
  0.0  Three or more separate questions/hints in one reply, OR a wall of explanation that buries the move.

""" + _JUDGE_OUTPUT


INFERENCE_NOT_EVALUATIVE_QUESTION_TEMPLATE = """You are evaluating whether a tutor's question is an INFERENCE question vs an EVALUATIVE question.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

  * Inference questions require the student to construct or reason: "How would you simplify this?", "What changes if x is negative?", "Why does that step work?"
  * Evaluative questions just check meta-understanding: "Do you understand?", "Does that make sense?", "Are you with me?"

Educational research (Vail et al. 2016) shows inference questions correlate with learning gains; evaluative questions don't help novices.

Score:
  1.0  Reply contains an inference question (asks the student to think / construct / reason).
  0.5  Reply has both an inference question AND an evaluative one — partial credit.
  0.0  Reply contains only evaluative questions ("understand?", "ok?") OR no question at all.

If a question would feel forced (greeting, off-topic) output {"score": null, "reason": "no question expected"}.

""" + _JUDGE_OUTPUT


CALIBRATED_TO_STUDENT_TEMPLATE = """You are evaluating whether a math tutor's hint is calibrated to the student's evident level.

Student is {{ case.context.student_age|default('a teenager') }} years old, in grade {{ case.context.grade|default('unknown') }}.

Student's last message (and prior turns suggest their current understanding):
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

VanLehn's "model, scaffold, fade" principle: a hint should match the student's competence. Too easy = patronizing / wastes time. Too hard = student stuck. Just right = nudges them to the next step they can take.

Score:
  1.0  Hint is at the right level — addresses the gap the student showed without insulting their existing knowledge.
  0.5  Hint slightly mismatched (a bit too easy or a bit advanced).
  0.0  Hint is far off — either trivial relative to the student's evident level, or jumps over their actual gap.

If reply provides no hint, output {"score": null, "reason": "no hint to calibrate"}.

""" + _JUDGE_OUTPUT


# ---------- D. Conversation quality ---------------------------------------
COHERENT_WITH_PRIOR_TURNS_TEMPLATE = """You are evaluating whether a tutor's reply is coherent with the prior conversation.

Full conversation so far:
{% for turn in case.conversation %}
{{ turn.role }}: {{ turn.content }}
{% endfor %}

Tutor's new reply:
\"\"\"{{ response }}\"\"\"

Score:
  1.0  Reply makes sense given the conversation so far. References prior turns appropriately.
  0.5  Reply is on-topic but slightly disconnected (treats this as a fresh question when it shouldn't).
  0.0  Reply contradicts prior turns, ignores what was just said, or restarts the topic out of nowhere.

For single-turn cases (only one user message, no prior tutor turns), output {"score": null, "reason": "single-turn case"}.

""" + _JUDGE_OUTPUT


# ---------- E. Affective / relational -------------------------------------
ENCOURAGING_TONE_TEMPLATE = """You are evaluating the tone of a tutor's reply on a 3-way scale (per MRBench taxonomy).

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Score:
  1.0  Encouraging — warm, treats the student as capable, motivating without being saccharine.
  0.5  Neutral — businesslike, neither warm nor cold.
  0.0  Discouraging or offensive — cold, dismissive, lecturing, condescending ("come on, this is easy"), or in any way negative.

""" + _JUDGE_OUTPUT


HUMANLIKE_TEMPLATE = """You are evaluating whether a tutor's reply sounds humanlike (vs generic AI).

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Humanlike means: natural phrasing, occasional informal touches, doesn't sound templated. NOT humanlike: corporate AI cadence, every reply starts with "Great question!", over-formatted bullet lists everywhere, robotic structure.

Score:
  1.0  Reads like a real teacher talking to a student.
  0.5  Reads like a competent but slightly templated assistant.
  0.0  Generic AI cadence — formulaic openers, mechanical structure, no personality.

""" + _JUDGE_OUTPUT


AVOIDS_EMPTY_PRAISE_TEMPLATE = """You are evaluating whether a tutor avoided EMPTY PRAISE.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Empty praise = positive language used reflexively for trivial or wrong inputs. Examples:
  * "Great job!" when the student gave a one-word answer or made a mistake.
  * "Excellent reasoning!" right before correcting that very reasoning.
  * "Fantastic question!" for a generic question.

Honest acknowledgement is different — "good — you noticed the sign change" attached to a real observation is fine.

Score:
  1.0  Reply is honest. Praise (if any) is targeted and earned.
  0.5  Slight reflexive positivity but not damaging.
  0.0  Reflexive empty praise, especially praise of a wrong answer or trivial input.

""" + _JUDGE_OUTPUT


# ---------- F. Style / form -----------------------------------------------
LANGUAGE_MATCH_TEMPLATE = """You are evaluating whether a tutor reply is in the same language as the student.

Student wrote (declared language: {{ case.context.language|default('unknown') }}):
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

LaTeX, math symbols, and short proper nouns don't count as language switches.

Score:
  1.0  Reply is entirely in the same language as the student.
  0.5  Reply is mostly the same language but mixes in another (e.g. random English in a Hungarian reply).
  0.0  Reply is in a different language entirely.

""" + _JUDGE_OUTPUT


AGE_APPROPRIATE_TEMPLATE = """You are evaluating whether a tutor's response uses age-appropriate language.

Student is {{ case.context.student_age|default('a teenager') }} years old, in grade {{ case.context.grade|default('unknown') }}.

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Score:
  1.0  Vocabulary, sentence length, and concept density are appropriate for the age.
  0.5  Mostly fine but with a few too-advanced words or one over-complex sentence.
  0.0  Reads like a research paper for a teen, OR like talking to a small child for an older student — clearly mismatched.

""" + _JUDGE_OUTPUT


ON_TOPIC_TEMPLATE = """You are evaluating whether a math tutor stayed on topic.

Student's last message:
\"\"\"{{ case.last_user_message }}\"\"\"

Tutor's reply:
\"\"\"{{ response }}\"\"\"

A math tutor should stay focused on math (or on bringing the conversation back to math, gently and warmly). Off-topic chatter is undesirable.

Score:
  1.0  Reply is about math, OR gently redirects an off-topic question back to learning.
  0.5  Reply mixes math + significant off-topic content.
  0.0  Reply is entirely off-topic or refuses without redirecting.

""" + _JUDGE_OUTPUT


# ---------- G. Mathematical correctness -----------------------------------
MATHEMATICALLY_CORRECT_TEMPLATE = """You are evaluating whether a math tutor's reply contains any mathematically WRONG claims.

Conversation:
{% for turn in case.conversation %}
{{ turn.role }}: {{ turn.content }}
{% endfor %}

Tutor's reply:
\"\"\"{{ response }}\"\"\"

Check the math, definitions, and claims. Affirming a student's wrong answer counts as a wrong claim. So does stating an incorrect formula or fact.

Score:
  1.0  No false math, no false claims. (Asking questions without claiming anything also scores 1.0 — silence ≠ wrong.)
  0.5  Mostly correct but one minor inaccuracy or sloppy notation that could mislead.
  0.0  Contains a clearly wrong claim (incorrect formula, incorrect arithmetic, confirming a wrong student answer, etc).

""" + _JUDGE_OUTPUT


# ---------- Pattern rubric: LaTeX usage -----------------------------------
def uses_latex_pattern(response: str, case: Case) -> Score:
    """If the response discusses math, it should use LaTeX delimiters, not Unicode."""
    has_latex = bool(re.search(r"\$[^$\n]+\$|\$\$[\s\S]+?\$\$", response))
    has_unicode_math = bool(re.search(r"[²³⁴⁵√∫∑∏≈≠≤≥πθ∞]", response))
    last_user = case.last_user_message.lower()
    looks_like_math_question = any(
        kw in last_user
        for kw in [
            "egyenlet", "függvény", "derivált", "integrál", "deriv",
            "képlet", "számold",
            "equation", "function", "derivative", "integral", "solve",
            "compute", "calculate", "formula",
            "x²", "x^2", "²", "³", "√",
        ]
    )

    if not looks_like_math_question:
        return Score(score=None, reason="no math expected in reply")
    if has_unicode_math and not has_latex:
        return Score(score=0.0, reason="used Unicode math symbols instead of LaTeX")
    if has_latex:
        return Score(score=1.0, reason="uses LaTeX delimiters")
    return Score(score=0.5, reason="math context but no LaTeX (might be conceptual reply)")


# ===========================================================================
# Rubric registry
# ===========================================================================
RUBRICS: dict[str, Rubric] = {
    r.name: r
    for r in [
        # A. Mistake handling
        Rubric(
            name="mistake_identified",
            weight=2.5,
            description="Recognized that the student made a mistake (no false affirmation).",
            judge_template=MISTAKE_IDENTIFIED_TEMPLATE,
        ),
        Rubric(
            name="mistake_located",
            weight=2.0,
            description="Pinpointed WHERE in the student's reasoning the mistake is.",
            judge_template=MISTAKE_LOCATED_TEMPLATE,
        ),
        # B. Solution disclosure
        Rubric(
            name="does_not_reveal_answer",
            weight=3.0,
            description="Did NOT give away the final answer; scaffolded instead.",
            judge_template=DOES_NOT_REVEAL_ANSWER_TEMPLATE,
        ),
        # C. Guidance quality
        Rubric(
            name="provides_guidance",
            weight=2.0,
            description="Reply has substantive guidance, not just acknowledgement.",
            judge_template=PROVIDES_GUIDANCE_TEMPLATE,
        ),
        Rubric(
            name="actionable_guidance",
            weight=2.0,
            description="Hint is specific enough that the student knows what to do next.",
            judge_template=ACTIONABLE_GUIDANCE_TEMPLATE,
        ),
        Rubric(
            name="single_focused_move",
            weight=1.5,
            description="ONE question or hint per turn, not a wall.",
            judge_template=SINGLE_FOCUSED_MOVE_TEMPLATE,
        ),
        Rubric(
            name="inference_not_evaluative_question",
            weight=2.0,
            description="Asks 'how would you...?', not 'do you understand?'.",
            judge_template=INFERENCE_NOT_EVALUATIVE_QUESTION_TEMPLATE,
        ),
        Rubric(
            name="calibrated_to_student",
            weight=1.5,
            description="Hint matched to student's evident level (not babyish, not overwhelming).",
            judge_template=CALIBRATED_TO_STUDENT_TEMPLATE,
        ),
        # D. Conversation quality
        Rubric(
            name="coherent_with_prior_turns",
            weight=2.0,
            description="Reply makes sense given the prior conversation.",
            judge_template=COHERENT_WITH_PRIOR_TURNS_TEMPLATE,
        ),
        # E. Affective / relational
        Rubric(
            name="encouraging_tone",
            weight=1.5,
            description="Encouraging vs neutral vs offensive (3-way per MRBench).",
            judge_template=ENCOURAGING_TONE_TEMPLATE,
        ),
        Rubric(
            name="humanlike",
            weight=1.0,
            description="Sounds like a real teacher, not generic AI cadence.",
            judge_template=HUMANLIKE_TEMPLATE,
        ),
        Rubric(
            name="avoids_empty_praise",
            weight=2.0,
            description="No reflexive 'Great job!' for trivial/wrong inputs.",
            judge_template=AVOIDS_EMPTY_PRAISE_TEMPLATE,
        ),
        # F. Style / form
        Rubric(
            name="language_match",
            weight=3.0,
            description="Replied in the same language as the student.",
            judge_template=LANGUAGE_MATCH_TEMPLATE,
        ),
        Rubric(
            name="age_appropriate",
            weight=1.5,
            description="Vocabulary and complexity match the student's age.",
            judge_template=AGE_APPROPRIATE_TEMPLATE,
        ),
        Rubric(
            name="on_topic",
            weight=2.0,
            description="Stays on math (or redirects back gently).",
            judge_template=ON_TOPIC_TEMPLATE,
        ),
        Rubric(
            name="uses_latex",
            weight=1.0,
            description="Uses LaTeX delimiters when math is in the reply.",
            pattern_fn=uses_latex_pattern,
        ),
        # G. Mathematical correctness
        Rubric(
            name="mathematically_correct",
            weight=3.0,
            description="No false math, no affirmation of wrong student answers.",
            judge_template=MATHEMATICALLY_CORRECT_TEMPLATE,
        ),
    ]
}


# Helper used by Jinja templates.
def _attach_helpers(case: Case) -> Case:
    """Add the `last_user_message` convenience attribute used in templates."""
    last_user = next(
        (t.content for t in reversed(case.conversation) if t.role == "user"),
        "",
    )
    # mutate; Cases are short-lived per run
    case.last_user_message = last_user  # type: ignore[attr-defined]
    return case


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases: list[Case] = []
    for entry in raw:
        cases.append(
            Case(
                id=entry["id"],
                description=entry.get("description", ""),
                conversation=[
                    Turn(role=t["role"], content=t["content"])
                    for t in entry["conversation"]
                ],
                rubrics=entry.get("rubrics", []),
                tags=entry.get("tags", []),
                context=entry.get("context", {}),
            )
        )
    return cases


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def _run_one_case(
    case: Case,
    *,
    system_prompt: str,
    model: str,
    judge_model: str,
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
) -> CaseResult:
    case = _attach_helpers(case)
    started = time.monotonic()

    # 1. Get the tutor's response.
    messages = [
        {"role": "system", "content": system_prompt},
        *[{"role": t.role, "content": t.content} for t in case.conversation],
    ]
    async with sem:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
        )
    response = completion.choices[0].message.content or ""

    # 2. Score it against each requested rubric (in parallel).
    async def score_one(rubric_name: str) -> tuple[str, Score]:
        rubric = RUBRICS.get(rubric_name)
        if rubric is None:
            return rubric_name, Score(score=None, reason=f"unknown rubric: {rubric_name}")

        if rubric.pattern_fn is not None:
            return rubric_name, rubric.pattern_fn(response, case)

        assert rubric.judge_template is not None
        judge_prompt = Template(rubric.judge_template).render(
            case=case, response=response
        )
        async with sem:
            judge_completion = await client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": judge_prompt}],
                stream=False,
                response_format={"type": "json_object"},
            )
        raw = judge_completion.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
            return rubric_name, Score(
                score=parsed.get("score"),
                reason=str(parsed.get("reason", ""))[:300],
            )
        except json.JSONDecodeError:
            return rubric_name, Score(score=None, reason=f"judge returned non-JSON: {raw[:80]}")

    score_results = await asyncio.gather(
        *(score_one(name) for name in case.rubrics)
    )
    scores = dict(score_results)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return CaseResult(case=case, response=response, scores=scores, elapsed_ms=elapsed_ms)


async def run_eval(
    cases: list[Case],
    *,
    system_prompt: str,
    prompt_label: str,
    model: str = "gpt-4o-mini",
    judge_model: str = "gpt-4o-mini",
    concurrency: int = 6,
    api_key: str | None = None,
) -> RunResult:
    """Run all cases against `system_prompt` and score them. Returns aggregated results."""
    client = AsyncOpenAI(api_key=api_key)
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    case_results = await asyncio.gather(
        *(
            _run_one_case(
                c,
                system_prompt=system_prompt,
                model=model,
                judge_model=judge_model,
                client=client,
                sem=sem,
            )
            for c in cases
        )
    )

    return RunResult(
        prompt_label=prompt_label,
        model=model,
        cases=case_results,
        total_elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# Reporter — terminal-friendly summary + optional HTML
# ---------------------------------------------------------------------------
def aggregate(result: RunResult) -> dict[str, Any]:
    rubric_scores: dict[str, list[float]] = {}
    rubric_pass: dict[str, list[bool]] = {}
    for cr in result.cases:
        for name, score in cr.scores.items():
            if score.score is None:
                continue
            rubric_scores.setdefault(name, []).append(score.score)
            rubric_pass.setdefault(name, []).append(score.score >= 0.5)

    per_rubric = {
        name: {
            "avg": sum(scores) / len(scores) if scores else 0,
            "pass_rate": sum(rubric_pass[name]) / len(rubric_pass[name])
            if rubric_pass.get(name)
            else 0,
            "n": len(scores),
            "weight": RUBRICS[name].weight if name in RUBRICS else 1.0,
        }
        for name, scores in rubric_scores.items()
    }

    # weighted total = sum(avg * weight) / sum(weight)
    total_weight = sum(per_rubric[n]["weight"] for n in per_rubric) or 1
    weighted_total = (
        sum(per_rubric[n]["avg"] * per_rubric[n]["weight"] for n in per_rubric)
        / total_weight
    )

    return {
        "per_rubric": per_rubric,
        "weighted_total": weighted_total,
        "n_cases": len(result.cases),
    }


def print_terminal_report(result: RunResult) -> None:
    agg = aggregate(result)
    print()
    print(
        f"Eval results: {result.prompt_label} | {agg['n_cases']} cases | model={result.model} | {result.total_elapsed_ms / 1000:.1f}s"
    )
    print("─" * 72)
    print(f"{'Rubric':<26}{'Avg':>8}  {'Pass%':>6}   {'N':>4}   weight")
    print("─" * 72)
    for name, stats in sorted(
        agg["per_rubric"].items(),
        key=lambda kv: -kv[1]["weight"],
    ):
        print(
            f"{name:<26}"
            f"{stats['avg']:>8.2f}  "
            f"{stats['pass_rate'] * 100:>5.0f}%   "
            f"{stats['n']:>4}   {stats['weight']:>4.1f}"
        )
    print("─" * 72)
    print(f"Weighted total: {agg['weighted_total']:.2f}")
    print()

    failures = [
        cr
        for cr in result.cases
        if any(s.score is not None and s.score < 0.5 for s in cr.scores.values())
    ]
    if failures:
        print(f"Cases with at least one failing rubric ({len(failures)}):")
        for cr in failures[:10]:
            failed = [
                f"{n} ({s.score:.1f}: {s.reason})"
                for n, s in cr.scores.items()
                if s.score is not None and s.score < 0.5
            ]
            print(f"  • {cr.case.id}")
            for f in failed:
                print(f"      ↳ {f}")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more (see HTML report)")
        print()


def write_html_report(result: RunResult, out_path: Path) -> None:
    agg = aggregate(result)
    rows = []
    for cr in result.cases:
        score_cells = []
        for name in cr.case.rubrics:
            s = cr.scores.get(name)
            if s is None or s.score is None:
                score_cells.append(
                    f"<td style='color:#888'>n/a</td>"
                )
            else:
                color = "#16a34a" if s.score >= 0.7 else "#eab308" if s.score >= 0.4 else "#dc2626"
                score_cells.append(
                    f"<td style='color:{color}' title='{_html_escape(s.reason)}'>{s.score:.2f}</td>"
                )
        rows.append(
            f"<tr><td><code>{cr.case.id}</code><br><small>{_html_escape(cr.case.description)}</small></td>"
            f"<td><pre style='white-space:pre-wrap;max-width:560px'>{_html_escape(cr.response)}</pre></td>"
            + "".join(score_cells)
            + "</tr>"
        )

    rubric_headers = (
        "<th>" + "</th><th>".join(
            sorted(
                {n for cr in result.cases for n in cr.case.rubrics},
                key=lambda n: -RUBRICS[n].weight if n in RUBRICS else 0,
            )
        ) + "</th>"
    )
    summary = "".join(
        f"<li><strong>{n}</strong>: avg {s['avg']:.2f}, pass {s['pass_rate'] * 100:.0f}%</li>"
        for n, s in agg["per_rubric"].items()
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>StudAI eval — {result.prompt_label}</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui; padding: 24px; max-width: 1400px; margin: auto; color: #18181b }}
  h1 {{ font-weight: 500; margin-bottom: 4px }}
  .meta {{ color: #71717a; margin-bottom: 16px }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px }}
  th, td {{ border-bottom: 1px solid #e4e4e7; padding: 8px; text-align: left; vertical-align: top }}
  th {{ background: #f4f4f5; font-weight: 500; font-size: 12px; text-transform: uppercase; color: #52525b }}
  pre {{ font-family: ui-monospace, monospace; font-size: 12px; margin: 0 }}
  code {{ font-family: ui-monospace, monospace; font-size: 12px }}
  ul {{ line-height: 1.7 }}
</style></head>
<body>
  <h1>StudAI eval — {result.prompt_label}</h1>
  <div class="meta">{agg['n_cases']} cases · model={result.model} · {result.total_elapsed_ms / 1000:.1f}s · weighted total <strong>{agg['weighted_total']:.2f}</strong></div>
  <ul>{summary}</ul>
  <table>
    <thead><tr><th>Case</th><th>Response</th>{rubric_headers}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
