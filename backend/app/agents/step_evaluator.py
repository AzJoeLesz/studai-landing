"""Phase 10B — per-turn step evaluator.

The student is in guided mode. This module's `evaluate(...)` runs ONE
small LLM call BEFORE the main tutor LLM call (Decision D in
docs/phase10_solution_graphs.md), classifying the student's latest
message against the current step's `expected_state`. The orchestrator
in `agents/guided_mode.py` reads the result and decides whether to
advance, hint, or offer an alternative path.

Latency contract:
  * Hard timeout = `Settings.step_evaluator_timeout_ms` (default 600ms).
  * On timeout / error / unparsable JSON, return a `no_step_yet`
    fallback so the main turn proceeds without an evaluator signal
    (graceful degradation).

Cost contract:
  * One small completion per turn while guided mode is active.
  * Default model: `gpt-4o-mini` (`Settings.step_evaluator_model`).
  * ~$0.0003/turn at gpt-4o-mini prices.

Cache:
  * In-process LRU keyed by `(step_id, sha256(student_message[:1000]))`.
  * Same retry of the same message on the same step is free. Cache size
    is capped so a chatty session doesn't grow it unboundedly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.core.config import get_settings
from app.db.schemas import (
    CommonMistake,
    GuidedProblemSession,
    MessageInput,
    Problem,
    SolutionPath,
    SolutionStep,
    StepHint,
)
from app.llm import get_llm_client

logger = logging.getLogger(__name__)


# Mirrors the categories in prompts/step_evaluator_v1.txt. Keep this
# enum and the prompt in sync; if you add a category to one, add it
# to the other or the orchestrator will silently treat it as
# "no_step_yet".
EvaluatorSignal = Literal[
    "on_path_correct",
    "on_path_partial",
    "off_path_valid",
    "off_path_invalid",
    "matched_mistake",
    "stuck_offer_alt_path",
    "no_step_yet",
]

# Hard upper bound on how many steps a single message can be credited
# with advancing. Keeps a confused / hallucinating evaluator from
# vaulting the student to the terminal step on a vague reply.
_MAX_STEP_ADVANCE = 5

# Cache size. ~1KB per entry; 256 entries = ~256KB per worker. Plenty
# for a single-process Railway worker handling concurrent sessions.
_CACHE_MAX = 256


@dataclass(frozen=True)
class EvaluatorOutcome:
    """The structured result of one evaluator call.

    `signal` is the bucket. `step_advance` is the number of CURRENT-
    PATH steps to advance (clamped to `_MAX_STEP_ADVANCE`). `notes` is
    a short string the orchestrator surfaces to the GUIDED PATH block
    so the main LLM has model-readable context for why the signal
    fired.
    """

    signal: EvaluatorSignal
    confidence: float
    matched_mistake_id: UUID | None
    step_advance: int
    notes: str
    used_fallback: bool = False


_FALLBACK = EvaluatorOutcome(
    signal="no_step_yet",
    confidence=0.0,
    matched_mistake_id=None,
    step_advance=0,
    notes="evaluator unavailable (timeout / error / parse fail)",
    used_fallback=True,
)


# Small in-process LRU. Async-safe: we never await between read +
# write inside `_cache_get` / `_cache_put`.
_cache: "OrderedDict[str, EvaluatorOutcome]" = OrderedDict()
_cache_lock = asyncio.Lock()


def _cache_key(step_id: UUID, student_message: str) -> str:
    body = student_message.strip()[:1000].encode("utf-8", errors="ignore")
    return f"{step_id}:{hashlib.sha256(body).hexdigest()}"


async def _cache_get(key: str) -> EvaluatorOutcome | None:
    async with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


async def _cache_put(key: str, outcome: EvaluatorOutcome) -> None:
    # Don't cache fallbacks -- a transient timeout shouldn't persist.
    if outcome.used_fallback:
        return
    async with _cache_lock:
        _cache[key] = outcome
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402  (kept after the constants for readability)

_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "step_evaluator_v1.txt"
)
_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _format_step(step: SolutionStep, label: str) -> str:
    parts = [
        f"{label}:",
        f"  step_index: {step.step_index}",
        f"  goal: {step.goal}",
    ]
    if step.expected_action:
        parts.append(f"  expected_action: {step.expected_action}")
    if step.expected_state:
        parts.append(f"  expected_state: {step.expected_state}")
    if step.is_terminal:
        parts.append("  is_terminal: true")
    return "\n".join(parts)


def _format_hints(hints: list[StepHint]) -> str:
    if not hints:
        return "(no hints stored for this step)"
    return "\n".join(
        f"  hint #{h.hint_index}: {h.body}"
        for h in sorted(hints, key=lambda x: x.hint_index)
    )


def _format_mistakes(mistakes: list[CommonMistake]) -> str:
    if not mistakes:
        return "(no common_mistakes stored for this step)"
    lines: list[str] = []
    for m in mistakes:
        lines.append(f"  - id: {m.id}")
        lines.append(f"    pattern: {m.pattern}")
        if m.detection_hint:
            lines.append(f"    detection_hint: {m.detection_hint}")
    return "\n".join(lines)


def _format_alt_paths(alt_paths: list[SolutionPath]) -> str:
    if not alt_paths:
        return "(no alternative verified paths)"
    return "\n".join(
        f"  - name: {p.name}  rationale: {p.rationale or ''}"
        for p in alt_paths
    )


def _build_user_payload(
    *,
    problem: Problem,
    path: SolutionPath,
    current_step: SolutionStep,
    next_step: SolutionStep | None,
    hints: list[StepHint],
    mistakes: list[CommonMistake],
    alt_paths: list[SolutionPath],
    guided: GuidedProblemSession,
    student_message: str,
) -> str:
    """Build the user-message payload for the evaluator call."""
    next_block = (
        _format_step(next_step, "NEXT STEP") if next_step is not None else "(none — current step is terminal)"
    )
    return (
        f"PROBLEM:\n{problem.problem_en[:2000]}\n\n"
        f"VERIFIED ANSWER (private — never reveal):\n"
        f"{(problem.answer or 'n/a')[:300]}\n\n"
        f"PATH: {path.name}  (rationale: {path.rationale or 'n/a'})\n"
        f"PATH PROGRESS: step {guided.current_step_index} of "
        f"{guided.current_step_index + (1 if next_step else 0)}+ "
        f"(attempts_on_step={guided.attempts_on_step}, "
        f"hints_consumed={guided.hints_consumed_on_step})\n\n"
        f"{_format_step(current_step, 'CURRENT STEP')}\n\n"
        f"{next_block}\n\n"
        f"STEP HINTS (graduated 1 -> 3):\n{_format_hints(hints)}\n\n"
        f"COMMON MISTAKES (uuids you may cite in matched_mistake_id):\n"
        f"{_format_mistakes(mistakes)}\n\n"
        f"ALTERNATIVE VERIFIED PATHS (for stuck_offer_alt_path):\n"
        f"{_format_alt_paths(alt_paths)}\n\n"
        f"STUDENT'S LATEST MESSAGE:\n{student_message[:2000]}"
    )


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------
_VALID_SIGNALS: frozenset[str] = frozenset(
    [
        "on_path_correct",
        "on_path_partial",
        "off_path_valid",
        "off_path_invalid",
        "matched_mistake",
        "stuck_offer_alt_path",
        "no_step_yet",
    ]
)


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _parse(
    raw: str, *, valid_mistake_ids: set[UUID]
) -> EvaluatorOutcome | None:
    parsed = _extract_json(raw)
    if not parsed:
        return None
    signal_str = (parsed.get("signal") or "").strip().lower()
    if signal_str not in _VALID_SIGNALS:
        return None

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    try:
        step_advance_raw = int(parsed.get("step_advance", 0))
    except (TypeError, ValueError):
        step_advance_raw = 0
    step_advance = max(0, min(_MAX_STEP_ADVANCE, step_advance_raw))

    matched_id_raw = parsed.get("matched_mistake_id")
    matched_id: UUID | None = None
    if matched_id_raw and isinstance(matched_id_raw, str):
        try:
            candidate = UUID(matched_id_raw.strip())
            if candidate in valid_mistake_ids:
                matched_id = candidate
        except ValueError:
            matched_id = None

    notes_raw = parsed.get("notes") or ""
    notes = str(notes_raw).strip()[:200]

    # Sanity: a matched_mistake signal without an id is downgraded to
    # off_path_invalid. The orchestrator can't surface a pedagogical
    # hint for an unknown mistake row.
    if signal_str == "matched_mistake" and matched_id is None:
        signal_str = "off_path_invalid"

    # Likewise: stuck_offer_alt_path with no alt paths in the prompt
    # context is downgraded to off_path_invalid by the orchestrator,
    # not here -- this module doesn't know about alt-path availability.

    return EvaluatorOutcome(
        signal=signal_str,  # type: ignore[arg-type]
        confidence=confidence,
        matched_mistake_id=matched_id,
        step_advance=step_advance,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def evaluate(
    *,
    problem: Problem,
    path: SolutionPath,
    current_step: SolutionStep,
    next_step: SolutionStep | None,
    hints: list[StepHint],
    mistakes: list[CommonMistake],
    alt_paths: list[SolutionPath],
    guided: GuidedProblemSession,
    student_message: str,
) -> EvaluatorOutcome:
    """One evaluator call, with timeout, cache, and graceful fallback.

    Always returns an `EvaluatorOutcome` -- `_FALLBACK` on any failure
    so the orchestrator never has to handle exceptions. Callers can
    check `outcome.used_fallback` if they want to alter behavior on
    degraded turns (e.g. log it).
    """
    if not student_message.strip():
        return _FALLBACK

    cache_key = _cache_key(current_step.id, student_message)
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    settings = get_settings()
    llm = get_llm_client()

    user_payload = _build_user_payload(
        problem=problem,
        path=path,
        current_step=current_step,
        next_step=next_step,
        hints=hints,
        mistakes=mistakes,
        alt_paths=alt_paths,
        guided=guided,
        student_message=student_message,
    )
    messages = [
        MessageInput(role="system", content=_SYSTEM_PROMPT),
        MessageInput(role="user", content=user_payload),
    ]
    valid_mistake_ids: set[UUID] = {m.id for m in mistakes}
    timeout_s = max(0.1, settings.step_evaluator_timeout_ms / 1000.0)

    try:
        raw = await asyncio.wait_for(
            llm.complete(
                messages,
                model=settings.step_evaluator_model,
                max_tokens=200,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.info(
            "step_evaluator: timeout after %.2fs (model=%s)",
            timeout_s,
            settings.step_evaluator_model,
        )
        return _FALLBACK
    except Exception:
        logger.warning(
            "step_evaluator: API call failed (model=%s)",
            settings.step_evaluator_model,
            exc_info=True,
        )
        return _FALLBACK

    outcome = _parse(raw or "", valid_mistake_ids=valid_mistake_ids)
    if outcome is None:
        logger.info(
            "step_evaluator: empty / unparsable / invalid JSON; using fallback"
        )
        return _FALLBACK

    await _cache_put(cache_key, outcome)
    return outcome


__all__ = [
    "EvaluatorOutcome",
    "EvaluatorSignal",
    "evaluate",
]
