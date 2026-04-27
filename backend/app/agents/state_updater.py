"""Post-turn state extractor.

Runs as a fire-and-forget task after the assistant's reply has finished
streaming, exactly like the answer-leak guard in `agents/tutor.py`. It
makes one cheap LLM call that returns structured JSON describing what
just happened in the session, then writes that into:

  * `session_state`     — current_topic, mode, struggling_on, mood, summary
  * `student_progress`  — mastery_signals via 9D's BKT-IDEM update

Failures are non-fatal: if the LLM call breaks or returns junk, we log
and move on. The student state goes stale, not corrupt. Latency on the
user-visible stream is unaffected (the task is scheduled with
`asyncio.create_task`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Iterable
from uuid import UUID

from app.agents import mastery
from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import (
    MessageInput,
    SessionState,
    SessionStateUpdate,
)
from app.llm import get_llm_client

logger = logging.getLogger(__name__)


_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "state_extractor_v1.txt"
_EXTRACTOR_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

_MAX_SUMMARY_CHARS = 4000
_MAX_DELTA_CHARS = 240


def _normalize_topic(label: str | None) -> str | None:
    """Light dedup: lowercase, strip, collapse whitespace.

    The Phase 11 canonical taxonomy will replace this; for now it just
    keeps `student_progress` from accumulating duplicate topic rows that
    differ only in casing or spacing.
    """
    if not label:
        return None
    cleaned = re.sub(r"\s+", " ", label).strip().lower()
    return cleaned[:120] or None


def _extract_json(raw: str) -> dict | None:
    """Best-effort JSON parser for LLM responses.

    Handles: bare JSON, ```json ... ``` fences, leading/trailing prose.
    """
    if not raw:
        return None
    text = raw.strip()
    # Try a direct parse first -- the prompt asks for exactly this.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find the outermost {...} block and try that.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _append_summary(prev: str | None, delta: str | None) -> str | None:
    if not delta:
        return prev
    delta = delta.strip()[:_MAX_DELTA_CHARS]
    if not delta:
        return prev
    if not prev:
        return delta
    combined = f"{prev.strip()} {delta}"
    if len(combined) <= _MAX_SUMMARY_CHARS:
        return combined
    # Truncate from the front when we run out of room. Phase 9A does not
    # do periodic full-regen summarization (open question in the design
    # doc); the truncation keeps the prompt bounded until 9A+.
    return combined[-_MAX_SUMMARY_CHARS:]


async def _call_extractor(
    user_message: str,
    assistant_reply: str,
    prior_state: SessionState | None,
) -> SessionStateUpdate | None:
    settings = get_settings()
    llm = get_llm_client()

    prior_block = "PRIOR SESSION STATE (may be empty):\n"
    if prior_state is not None:
        prior_block += json.dumps(
            {
                "current_topic": prior_state.current_topic,
                "mode": prior_state.mode,
                "struggling_on": prior_state.struggling_on,
                "mood_signals": prior_state.mood_signals or {},
                "summary": (prior_state.summary or "")[-1500:],
                "attempts_count": prior_state.attempts_count,
            },
            ensure_ascii=False,
        )
    else:
        prior_block += "(none yet)"

    user_payload = (
        f"{prior_block}\n\n"
        f"STUDENT MESSAGE:\n{user_message[:3000]}\n\n"
        f"TUTOR REPLY:\n{assistant_reply[:3000]}"
    )

    messages = [
        MessageInput(role="system", content=_EXTRACTOR_SYSTEM_PROMPT),
        MessageInput(role="user", content=user_payload),
    ]
    raw = await llm.complete(
        messages,
        model=settings.state_extractor_model,
        max_tokens=400,
    )
    parsed = _extract_json(raw or "")
    if not parsed:
        logger.debug("state_updater: empty/unparsable JSON from LLM")
        return None
    try:
        return SessionStateUpdate.model_validate(parsed)
    except Exception:
        logger.debug(
            "state_updater: JSON did not match SessionStateUpdate shape",
            exc_info=True,
        )
        return None


def _apply_mastery_signals(
    user_id: UUID,
    signals: Iterable,
    *,
    fallback_topic: str | None,
) -> None:
    """Translate extractor signals into BKT-IDEM updates.

    Difficulty is unknown at extractor-time (we don't have a graded
    problem), so we treat it as 'medium' and let `mastery.update_from_extractor`
    apply the low-weight 'extractor' source factor.
    """
    for sig in signals:
        topic = _normalize_topic(sig.topic) or _normalize_topic(fallback_topic)
        if not topic:
            continue
        try:
            mastery.update_from_extractor(
                user_id=user_id, topic=topic, delta=float(sig.delta)
            )
        except Exception:
            logger.debug(
                "state_updater: mastery update failed for topic=%s",
                topic,
                exc_info=True,
            )


async def update_state_after_turn(
    session_id: UUID,
    user_id: UUID,
    user_message: str,
    assistant_reply: str,
) -> None:
    """Top-level entry point. Safe to schedule with `asyncio.create_task`.

    Order:
      1. LLM extracts structured update.
      2. Bump attempts_count for this session.
      3. Merge update into `session_state` (only filled fields overwrite).
      4. Apply mastery_signals via BKT-IDEM (writes to `student_progress`).
    """
    try:
        prior_state = await asyncio.to_thread(repo.get_session_state, session_id)
        update = await _call_extractor(user_message, assistant_reply, prior_state)

        await asyncio.to_thread(repo.increment_session_attempts, session_id)

        if update is None:
            return

        new_summary = _append_summary(
            prior_state.summary if prior_state else None,
            update.summary_delta,
        )

        await asyncio.to_thread(
            repo.upsert_session_state,
            session_id,
            current_topic=_normalize_topic(update.current_topic),
            mode=update.mode,
            struggling_on=(
                update.struggling_on.strip()[:400]
                if update.struggling_on
                else None
            ),
            mood_signals=update.mood_signals or None,
            summary=new_summary,
        )

        if update.mastery_signals:
            await asyncio.to_thread(
                _apply_mastery_signals,
                user_id,
                update.mastery_signals,
                fallback_topic=update.current_topic
                or (prior_state.current_topic if prior_state else None),
            )
    except Exception:
        # Truly never let a state update bring down a turn -- the answer
        # has already been delivered to the user by the time this runs.
        logger.warning(
            "state_updater: post-turn extraction failed (non-fatal)",
            exc_info=True,
        )
