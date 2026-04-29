"""Phase 10B — guided-mode orchestration.

Sits between `agents/tutor.py` (the chat-turn driver) and the
underlying tables (`solution_paths`, `solution_steps`, `step_hints`,
`common_mistakes`, `guided_problem_sessions`).

Responsibilities:
  1. **Activation gate**. Should this turn enter / continue guided
     mode? Reads the active `guided_problem_session` row, the top RAG
     hit, the configured similarity threshold, and the
     register-from-style-policy decision.
  2. **Path picker**. Decision L (lock): silent default uses
     `preferred=true`. Dynamic switch on `stuck_offer_alt_path` is
     handled here when the orchestrator sees the right signal.
  3. **State machine**. Translate an `EvaluatorOutcome` into the new
     counters / step index / status for `guided_problem_sessions`,
     and persist them.
  4. **Prompt block formatter**. Build the GUIDED PROBLEM PATH system
     message that the tutor injects between grounding and STYLE
     DIRECTIVES (see docs/phase10_solution_graphs.md "System message
     order").

This module deliberately does NOT import from `tutor.py` — `tutor.py`
imports from here. Keep the dependency direction clean so 10D's
mistake-handling additions don't accidentally reintroduce a cycle.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Literal
from uuid import UUID

from app.agents.step_evaluator import EvaluatorOutcome, evaluate as run_evaluator
from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import (
    CommonMistake,
    GuidedProblemSession,
    Problem,
    ProblemSearchResult,
    SolutionPath,
    SolutionStep,
    StepHint,
)

logger = logging.getLogger(__name__)


# Registers in which guided mode is allowed to run. The Phase 9 doc
# already locked this: above_level_exploration and below_level_warmup
# both kill guided mode (the student isn't being TUTORED on this topic,
# they're exploring or warming up). at_level and remedial both keep
# it on -- remedial means "student needs this topic, slow down", and
# guided mode is the slow-down mechanism.
GuidedRegister = Literal["at_level", "remedial"]
_GUIDED_REGISTERS: frozenset[str] = frozenset(["at_level", "remedial"])


@dataclass(frozen=True)
class GuidedTurnContext:
    """Everything the tutor needs to render a guided-mode turn.

    Returned by `prepare_turn(...)`. None means "no guided mode this
    turn — proceed as Phase 9 would have". The tutor just checks
    `is None` to decide whether to inject the GUIDED PATH block.

    `evaluator_outcome` is None on the FIRST turn after activation
    (no student attempt to evaluate yet).
    """

    guided: GuidedProblemSession  # the up-to-date row, post-state-machine
    problem: Problem
    path: SolutionPath
    steps: list[SolutionStep]
    current_step: SolutionStep
    next_step: SolutionStep | None
    hints: list[StepHint]               # for the CURRENT step only
    mistakes: list[CommonMistake]        # for the CURRENT step (step-scoped)
    alt_paths: list[SolutionPath]        # other verified paths on this problem
    evaluator_outcome: EvaluatorOutcome | None
    is_activation_turn: bool             # this row was just inserted this turn


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------
def _verified_path_for_problem(problem_id: UUID) -> SolutionPath | None:
    """Return the preferred verified path for a problem, or None.

    `repo.get_paths_for_problem` already sorts with `preferred=True`
    first, so taking element 0 is the silent picker (Decision L).
    """
    paths = repo.get_paths_for_problem(
        problem_id, language="en", verified_only=True
    )
    return paths[0] if paths else None


def _alt_paths_for_problem(
    problem_id: UUID, *, exclude_path_id: UUID
) -> list[SolutionPath]:
    """Other verified paths on the same problem, excluding the active one."""
    paths = repo.get_paths_for_problem(
        problem_id, language="en", verified_only=True
    )
    return [p for p in paths if p.id != exclude_path_id]


# Problem fetch by id is `repo.get_problem` (added in 10C alongside the
# /admin/paths endpoints). Earlier versions of this module inlined the
# query; that's now centralized.


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _StateChange:
    """Fields to patch on the guided_problem_session row."""

    current_step_index: int | None = None
    attempts_on_step: int | None = None
    hints_consumed_on_step: int | None = None
    off_path_count: int | None = None
    status: str | None = None


def _apply_evaluator_to_state(
    *,
    guided: GuidedProblemSession,
    outcome: EvaluatorOutcome,
    total_steps: int,
    has_alt_paths: bool,
) -> tuple[GuidedProblemSession, _StateChange]:
    """Pure state-machine: figure out what changes, return both the
    new in-memory row and the patch the repo should apply.

    * `on_path_correct` -> bump current_step_index by `step_advance`
      (clamped to total_steps); reset attempts + hints; mark complete
      if we passed the terminal step.
    * `on_path_partial` -> attempts_on_step += 1.
    * `off_path_valid` -> attempts_on_step += 1, off_path_count += 1.
      (Path switching itself is handled separately by `switch_to_alt_path`.)
    * `off_path_invalid` -> attempts_on_step += 1.
    * `matched_mistake` -> attempts_on_step += 1.
    * `stuck_offer_alt_path` -> attempts_on_step += 1. The orchestrator
      adds the offer to the GUIDED PATH block; the actual switch
      happens NEXT turn if the student says yes (a one-shot intent
      classifier is added in 10D — for 10B we already advance the
      counters here).
    * `no_step_yet` -> no change.

    Decision E (per-step step_check BKT writes) is handled in 10D, not
    here. This function only owns the guided_problem_session row.
    """
    # Downgrade stuck_offer_alt_path -> matched_mistake / off_path_invalid
    # if there are no alt paths to actually offer (the evaluator may
    # have hallucinated). Keeps the GUIDED PATH block honest.
    signal = outcome.signal
    if signal == "stuck_offer_alt_path" and not has_alt_paths:
        signal = "off_path_invalid"

    if signal == "on_path_correct":
        advance = max(1, outcome.step_advance)  # at least 1 if correct
        new_index = min(total_steps + 1, guided.current_step_index + advance)
        if new_index > total_steps:
            # Walked past the last step -> session is complete.
            change = _StateChange(
                current_step_index=total_steps,
                attempts_on_step=0,
                hints_consumed_on_step=0,
                status="completed",
            )
            new_row = replace(
                guided,
                current_step_index=total_steps,
                attempts_on_step=0,
                hints_consumed_on_step=0,
                status="completed",
            )
        else:
            change = _StateChange(
                current_step_index=new_index,
                attempts_on_step=0,
                hints_consumed_on_step=0,
            )
            new_row = replace(
                guided,
                current_step_index=new_index,
                attempts_on_step=0,
                hints_consumed_on_step=0,
            )
        return new_row, change

    if signal == "no_step_yet":
        return guided, _StateChange()

    # Everything else bumps attempts_on_step by 1; off_path_valid
    # also bumps off_path_count.
    new_attempts = (guided.attempts_on_step or 0) + 1
    new_off_path = guided.off_path_count
    if signal == "off_path_valid":
        new_off_path = (guided.off_path_count or 0) + 1
    change = _StateChange(
        attempts_on_step=new_attempts,
        off_path_count=new_off_path if signal == "off_path_valid" else None,
    )
    new_row = replace(
        guided,
        attempts_on_step=new_attempts,
        off_path_count=new_off_path,
    )
    return new_row, change


def _persist_state_change(guided_id: UUID, change: _StateChange) -> None:
    """Translate `_StateChange` into a repo.update_guided_session call."""
    if all(
        v is None
        for v in (
            change.current_step_index,
            change.attempts_on_step,
            change.hints_consumed_on_step,
            change.off_path_count,
            change.status,
        )
    ):
        return  # no-op
    repo.update_guided_session(
        guided_id,
        current_step_index=change.current_step_index,
        attempts_on_step=change.attempts_on_step,
        hints_consumed_on_step=change.hints_consumed_on_step,
        off_path_count=change.off_path_count,
        status=change.status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Public entry point: prepare_turn
# ---------------------------------------------------------------------------
async def prepare_turn(
    *,
    session_id: UUID,
    user_message: str,
    register: str,
    history_has_assistant_reply: bool,
    top_rag_hit: ProblemSearchResult | None,
) -> GuidedTurnContext | None:
    """One-stop call from `agents/tutor.py` to set up guided mode for this turn.

    Returns None when guided mode does not apply this turn — caller
    proceeds as Phase 9. Returns a fully-loaded `GuidedTurnContext`
    when guided mode is active (either freshly activated this turn or
    continuing from a prior turn).

    Key gates (in order):
      1. `Settings.guided_mode_enabled` master switch.
      2. `register` is `at_level` or `remedial` (Phase 9 lock-in).
      3. Either an active row exists OR a new activation can fire
         (top RAG hit clears `guided_mode_similarity_threshold` AND
         the hit problem has at least one verified path).
      4. The path actually has at least one step row (defensive).

    The blocking step-evaluator call (Decision D) happens here when
    appropriate. Hard timeout in `step_evaluator.evaluate` keeps the
    TTFT regression bounded.
    """
    settings = get_settings()
    if not settings.guided_mode_enabled:
        return None
    if register not in _GUIDED_REGISTERS:
        return None

    existing = await asyncio.to_thread(
        repo.get_active_guided_session, session_id
    )

    is_activation_turn = False

    if existing is None:
        # Try to activate.
        if top_rag_hit is None:
            return None
        if top_rag_hit.similarity < settings.guided_mode_similarity_threshold:
            return None
        path = await asyncio.to_thread(
            _verified_path_for_problem, top_rag_hit.id
        )
        if path is None:
            return None  # No verified path -> no guided mode.

        problem = await asyncio.to_thread(repo.get_problem, top_rag_hit.id)
        if problem is None:
            return None

        # Check the path has steps before we even bother creating the row.
        steps = await asyncio.to_thread(repo.get_steps_for_path, path.id)
        if not steps:
            logger.warning(
                "guided_mode: verified path %s has no steps; skipping",
                path.id,
            )
            return None

        existing = await asyncio.to_thread(
            repo.get_or_start_guided_session,
            session_id=session_id,
            problem_id=top_rag_hit.id,
            active_path_id=path.id,
        )
        is_activation_turn = True
    else:
        # Continuing. Load the active path; if it's been unverified
        # since (admin reverted), gracefully back out of guided mode.
        if existing.active_path_id is None:
            return None
        path = await asyncio.to_thread(
            repo.get_solution_path, existing.active_path_id
        )
        if path is None or not path.verified:
            logger.info(
                "guided_mode: active path %s vanished or was unverified; "
                "marking session abandoned",
                existing.active_path_id,
            )
            await asyncio.to_thread(
                repo.update_guided_session, existing.id, status="abandoned"
            )
            return None
        steps = await asyncio.to_thread(repo.get_steps_for_path, path.id)
        if not steps:
            return None
        problem = await asyncio.to_thread(
            repo.get_problem, existing.problem_id
        )
        if problem is None:
            return None

    # We have: existing (the row), path, steps, problem, is_activation_turn.
    total_steps = len(steps)
    current_index = max(1, min(total_steps, existing.current_step_index))
    current_step = steps[current_index - 1]
    next_step = steps[current_index] if current_index < total_steps else None

    # Load current-step hints + mistakes + alt paths in parallel.
    hints, step_mistakes, alt_paths = await asyncio.gather(
        asyncio.to_thread(repo.get_hints_for_step, current_step.id),
        asyncio.to_thread(repo.get_mistakes_for_step, current_step.id),
        asyncio.to_thread(
            _alt_paths_for_problem, problem.id, exclude_path_id=path.id
        ),
    )

    outcome: EvaluatorOutcome | None = None
    if not is_activation_turn and history_has_assistant_reply:
        # Run the evaluator. step_evaluator.evaluate has its own
        # timeout + fallback; it always returns an EvaluatorOutcome.
        outcome = await run_evaluator(
            problem=problem,
            path=path,
            current_step=current_step,
            next_step=next_step,
            hints=hints,
            mistakes=step_mistakes,
            alt_paths=alt_paths,
            guided=existing,
            student_message=user_message,
        )
        # Apply state machine + persist.
        new_existing, change = _apply_evaluator_to_state(
            guided=existing,
            outcome=outcome,
            total_steps=total_steps,
            has_alt_paths=bool(alt_paths),
        )
        if (
            new_existing.current_step_index != existing.current_step_index
            or new_existing.attempts_on_step != existing.attempts_on_step
            or new_existing.off_path_count != existing.off_path_count
            or new_existing.status != existing.status
        ):
            await asyncio.to_thread(
                _persist_state_change, existing.id, change
            )
            existing = new_existing
            # If we advanced the step pointer, refresh current/next.
            current_index = max(
                1, min(total_steps, existing.current_step_index)
            )
            current_step = steps[current_index - 1]
            next_step = (
                steps[current_index] if current_index < total_steps else None
            )
            if change.current_step_index is not None:
                # Reload hints + mistakes for the NEW current step.
                hints, step_mistakes = await asyncio.gather(
                    asyncio.to_thread(repo.get_hints_for_step, current_step.id),
                    asyncio.to_thread(repo.get_mistakes_for_step, current_step.id),
                )

    return GuidedTurnContext(
        guided=existing,
        problem=problem,
        path=path,
        steps=steps,
        current_step=current_step,
        next_step=next_step,
        hints=hints,
        mistakes=step_mistakes,
        alt_paths=alt_paths,
        evaluator_outcome=outcome,
        is_activation_turn=is_activation_turn,
    )


# ---------------------------------------------------------------------------
# GUIDED PROBLEM PATH system block
# ---------------------------------------------------------------------------
def _format_signal_instruction(
    *,
    outcome: EvaluatorOutcome | None,
    is_activation_turn: bool,
    has_alt_paths: bool,
    matched_mistake: CommonMistake | None,
) -> str:
    """The 'INSTRUCTIONS FOR THIS REPLY' bullets — vary by signal."""
    if is_activation_turn:
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - This is the activation turn (the student just typed the\n"
            "    problem). Do PHASE 1 — DIAGNOSTIC per the v3 prompt: a\n"
            "    warm acknowledgement plus ONE open question. Do NOT\n"
            "    name the operation, do NOT mention the numbers, do NOT\n"
            "    hint at the method. The path is loaded silently in the\n"
            "    background so the next turn's evaluator can check the\n"
            "    student's first attempt."
        )

    if outcome is None or outcome.signal == "no_step_yet":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student is asking a clarifying question or chatting,\n"
            "    not attempting the problem. Answer their question warmly\n"
            "    in a sentence or two, then gently re-orient: 'When you're\n"
            "    ready, what's your first move on this problem?'\n"
            "  - Do NOT take the next step on their behalf. Do NOT reveal\n"
            "    the expected_state. Do NOT name the operation."
        )

    if outcome.signal == "on_path_correct":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student JUST completed the previous step correctly.\n"
            "    Acknowledge SPECIFICALLY what they did right (name the\n"
            "    move, not generic praise). Then ask ONE Socratic question\n"
            "    that nudges toward the CURRENT STEP's expected_state.\n"
            "  - Do NOT reveal expected_state or the final answer.\n"
            "  - Use STYLE DIRECTIVES.step_size to decide granularity."
        )

    if outcome.signal == "on_path_partial":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student is heading the right direction but hasn't\n"
            "    finished this step. Encourage them and ask the smallest\n"
            "    next sub-question to close the gap.\n"
            "  - Do NOT compute the result for them."
        )

    if outcome.signal == "off_path_valid":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student took a different but mathematically valid\n"
            "    step. Validate their move warmly ('that works too!') and\n"
            "    follow them down their direction. Ask the next Socratic\n"
            "    question that progresses from their current state."
        )

    if outcome.signal == "matched_mistake" and matched_mistake is not None:
        # Surface the canonical pedagogical_hint for the model to use\n
        # *in spirit* (rephrased, never recited verbatim).
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student matched a known common mistake. Use the\n"
            "    PEDAGOGICAL HINT below in spirit (rephrase, do not\n"
            "    recite). Identify the mistake gently, ask ONE concrete\n"
            "    question that guides them to find the fix themselves.\n"
            "    Do NOT give the corrected answer.\n\n"
            f"  MISTAKE PATTERN: {matched_mistake.pattern}\n"
            f"  PEDAGOGICAL HINT (use in spirit, do not recite):\n"
            f"    {matched_mistake.pedagogical_hint}"
        )

    if outcome.signal == "stuck_offer_alt_path":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student has been stuck on this step for several tries.\n"
            "    You may OFFER an alternative path in your tutor's voice.\n"
            "    Suggested phrasing (adapt to the student's tone, do NOT\n"
            "    paste verbatim):\n"
            "      'Hmm, this approach is feeling tricky — want to try it\n"
            "       a different way? We could use [alternative_name]\n"
            "       instead, which works nicely when [alternative_rationale].'\n"
            "  - Do NOT force the switch. If they say yes next turn, the\n"
            "    system swaps active_path_id automatically.\n"
            "  - Do NOT also give a hint on the current path in the same\n"
            "    reply -- the offer IS your one move this turn."
        )

    if outcome.signal == "off_path_invalid":
        return (
            "INSTRUCTIONS FOR THIS REPLY:\n"
            "  - The student wrote something off-track. Identify the\n"
            "    problem precisely ('I see an error where you...'),\n"
            "    LOCATE it ('look at the step where you...'), then ask\n"
            "    ONE concrete question that guides them to find the fix.\n"
            "  - Do NOT give the correction. Do NOT give the answer.\n"
            "  - Use the STEP HINT at level (hints_consumed + 1) below if\n"
            "    you need a fallback question."
        )

    # Defensive fallback.
    return (
        "INSTRUCTIONS FOR THIS REPLY:\n"
        "  - Continue Socratic guidance toward CURRENT STEP's expected_state."
    )


def format_guided_path_block(ctx: GuidedTurnContext) -> str:
    """Build the GUIDED PROBLEM PATH system message.

    Slots between grounding (suppressed L1+L3 when guided active per
    Decision K) and the STYLE DIRECTIVES block. Stays under ~600
    tokens worst-case.
    """
    total_steps = len(ctx.steps)
    matched_mistake: CommonMistake | None = None
    if (
        ctx.evaluator_outcome is not None
        and ctx.evaluator_outcome.matched_mistake_id is not None
    ):
        matched_mistake = next(
            (
                m
                for m in ctx.mistakes
                if m.id == ctx.evaluator_outcome.matched_mistake_id
            ),
            None,
        )

    lines: list[str] = [
        "GUIDED PROBLEM PATH (private — strict guidance, do not recite):",
        f"problem_id: {ctx.problem.id}",
        f"path: {ctx.path.name}",
        f"rationale: {ctx.path.rationale or 'n/a'}",
        f"total_steps: {total_steps}",
        f"current_step: {ctx.guided.current_step_index} of {total_steps}",
        "",
        "CURRENT STEP:",
        f"  goal: {ctx.current_step.goal}",
    ]
    if ctx.current_step.expected_action:
        lines.append(f"  expected_action: {ctx.current_step.expected_action}")
    if ctx.current_step.expected_state:
        lines.append(f"  expected_state: {ctx.current_step.expected_state}")
    if ctx.current_step.is_terminal:
        lines.append("  is_terminal: true (this step's expected_state IS the answer)")

    if ctx.evaluator_outcome is not None:
        lines.append("")
        lines.append(
            f"EVALUATOR SIGNAL (this turn): {ctx.evaluator_outcome.signal}"
        )
        lines.append(
            f"evaluator_confidence: {ctx.evaluator_outcome.confidence:.2f}"
        )
        lines.append(f"evaluator_notes: {ctx.evaluator_outcome.notes}")
    elif ctx.is_activation_turn:
        lines.append("")
        lines.append("EVALUATOR SIGNAL: activation_turn (no evaluator yet)")

    lines.append(
        f"attempts_on_this_step: {ctx.guided.attempts_on_step}"
    )
    lines.append(
        f"hints_consumed_on_step: {ctx.guided.hints_consumed_on_step}"
    )

    # Step hints — show ALL three so the model can grade-up if needed,
    # but the evaluator-driven instructions tell it which to lean on.
    if ctx.hints:
        lines.append("")
        lines.append("STEP HINTS (graduated — pick by hints_consumed + 1):")
        for h in sorted(ctx.hints, key=lambda x: x.hint_index):
            lines.append(f"  hint #{h.hint_index}: {h.body}")

    # Next step — only as a teaser; the model doesn't act on it yet.
    if ctx.next_step is not None:
        lines.append("")
        lines.append("NEXT STEP (only after current step passes):")
        lines.append(f"  goal: {ctx.next_step.goal}")
        if ctx.next_step.expected_state:
            lines.append(
                f"  expected_state_after: {ctx.next_step.expected_state}"
            )

    # Alt paths — only printed when stuck_offer_alt_path is the signal.
    if (
        ctx.evaluator_outcome is not None
        and ctx.evaluator_outcome.signal == "stuck_offer_alt_path"
        and ctx.alt_paths
    ):
        lines.append("")
        lines.append("ALTERNATIVE VERIFIED PATHS AVAILABLE:")
        for ap in ctx.alt_paths:
            lines.append(
                f"  - {ap.name} (rationale: {ap.rationale or 'n/a'})"
            )

    lines.append("")
    lines.append(
        _format_signal_instruction(
            outcome=ctx.evaluator_outcome,
            is_activation_turn=ctx.is_activation_turn,
            has_alt_paths=bool(ctx.alt_paths),
            matched_mistake=matched_mistake,
        )
    )
    return "\n".join(lines)


__all__ = [
    "GuidedTurnContext",
    "prepare_turn",
    "format_guided_path_block",
]
