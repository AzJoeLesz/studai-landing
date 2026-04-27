"""Tutor orchestration.

This is the brain of StudAI. Today it is a single function: load history,
profile, session state, and progress in parallel; assemble the system
context (persona + profile + style directives + progress + session state
+ RAG layers); stream an LLM reply; persist the reply; fire post-turn
side effects (answer-leak guard + state extractor) without blocking.

Design rules for this file:
  * The API layer must never import from `llm/` or `prompts/` directly.
  * Database access goes through `db.repositories`.
  * This is the ONLY place that knows about the tutor's persona.
  * The order of system messages is intentional. Do not reorder without
    updating the v3 prompt's "PRIVATE CONTEXT BLOCKS YOU MAY RECEIVE"
    section.
"""

import asyncio
import logging
from typing import AsyncIterator
from uuid import UUID

from app.agents import state_updater, style_policy
from app.agents.retrieval import GroundingContext, build_grounding_context
from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import (
    Message,
    MessageInput,
    Profile,
    SessionState,
    StudentProgress,
)
from app.llm import get_llm_client
from app.prompts import CURRENT_TUTOR_PROMPT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Answer-leak guard
# ---------------------------------------------------------------------------

_GUARD_PROMPT = """\
You are an answer-leak detector for a math tutoring system.

A tutor's reply will be shown to you along with the student's message.
Your ONLY job: determine whether the tutor revealed the FINAL ANSWER
or a COMPLETE SOLUTION to the student's math problem.

Guidelines:
- Giving a HINT, asking a QUESTION, or showing ONE intermediate step
  is NOT leaking — that is good tutoring.
- Revealing the final numerical/algebraic answer IS leaking.
- Writing out a full worked solution IS leaking.
- If there is no math problem being solved (just conversation or concept
  explanation), reply NO.

Reply with exactly one word: YES or NO.
""".strip()


async def _check_answer_leak(
    user_message: str, assistant_reply: str, session_id: UUID
) -> None:
    """Fire-and-forget guard: checks if the reply leaked the answer."""
    try:
        llm = get_llm_client()
        probe = [
            MessageInput(role="system", content=_GUARD_PROMPT),
            MessageInput(
                role="user",
                content=(
                    f"STUDENT MESSAGE:\n{user_message}\n\n"
                    f"TUTOR REPLY:\n{assistant_reply}"
                ),
            ),
        ]
        verdict = await llm.complete(probe, max_tokens=3)
        verdict = verdict.strip().upper()

        if verdict.startswith("YES"):
            logger.warning(
                "ANSWER-LEAK detected | session=%s | student=%.80s | reply=%.120s",
                session_id,
                user_message,
                assistant_reply,
            )
    except Exception:
        logger.debug("Answer-leak guard failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Student profile -> prompt context
# ---------------------------------------------------------------------------


def _format_profile_snippet(profile: Profile | None) -> str | None:
    """Turn a profile row into a short paragraph the model can read.

    Returns None if the profile is missing or has no useful fields filled
    in -- in that case we don't add anything to the system context, and
    the tutor falls back to its generic behavior.

    We keep this deliberately short. Long profiles waste context tokens
    and dilute the actual tutoring instructions.
    """
    if profile is None:
        return None

    parts: list[str] = []
    if profile.display_name:
        parts.append(f"Name: {profile.display_name.strip()}")
    if profile.age is not None:
        parts.append(f"Age: {profile.age}")
    if profile.grade_level:
        parts.append(f"Grade level: {profile.grade_level.strip()}")
    if profile.interests:
        parts.append(f"Interests: {profile.interests.strip()}")
    if profile.learning_goals:
        parts.append(f"Learning goals: {profile.learning_goals.strip()}")
    if profile.notes:
        parts.append(f"Notes from the student: {profile.notes.strip()}")

    if not parts:
        return None

    body = "\n".join(parts)
    return (
        "STUDENT PROFILE\n"
        "Use this to personalize the conversation: address the student by\n"
        "name when natural, calibrate vocabulary and examples to their age\n"
        "and grade level, and use their interests to motivate examples\n"
        "where it fits. Don't reference the profile explicitly unless\n"
        "the student asks.\n"
        "---\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _build_context(
    history: list[Message],
    user_message: str,
    max_history: int,
    profile: Profile | None,
    session_state: SessionState | None,
    progress: list[StudentProgress] | None,
    grounding: GroundingContext | None = None,
) -> list[MessageInput]:
    """Assemble the message list we send to the LLM.

    Order (matches the "PRIVATE CONTEXT BLOCKS YOU MAY RECEIVE" section
    in tutor_v3.txt):
      1. Persona (CURRENT_TUTOR_PROMPT)
      2. Profile snippet
      3. Style directives (Phase 9B)
      4. Student progress (Phase 9A/9D)
      5. Session state (Phase 9A)
      6. Grounding L1 (problem RAG)
      7. Grounding L2 (OpenStax)
      8. Grounding L3 (annotations)
      9. Recent history (truncated; running summary covers older turns)
     10. New user turn

    Strategy v1: system prompt + last N messages + the new user turn.
    Strategy v2 (Phase 9A): the running `session_state.summary` is
    always present in the SESSION STATE block and effectively replaces
    older raw history past the truncation window.
    """
    settings = get_settings()
    system_messages: list[MessageInput] = [
        MessageInput(role="system", content=CURRENT_TUTOR_PROMPT),
    ]
    profile_snippet = _format_profile_snippet(profile)
    if profile_snippet:
        system_messages.append(
            MessageInput(role="system", content=profile_snippet)
        )

    if settings.style_policy_enabled:
        directives = style_policy.derive_directives(
            profile=profile,
            session_state=session_state,
            top_progress=progress,
        )
        system_messages.append(
            MessageInput(
                role="system",
                content=style_policy.format_directives_block(directives),
            )
        )

    if settings.progress_block_enabled:
        progress_block = style_policy.format_progress_block(progress)
        if progress_block:
            system_messages.append(
                MessageInput(role="system", content=progress_block)
            )

    if settings.session_state_block_enabled:
        state_block = style_policy.format_session_state_block(session_state)
        if state_block:
            system_messages.append(
                MessageInput(role="system", content=state_block)
            )

    g = grounding or GroundingContext()
    for snippet in (
        g.problem_reference,
        g.openstax_excerpts,
        g.teaching_annotations,
    ):
        if snippet:
            system_messages.append(MessageInput(role="system", content=snippet))

    recent = history[-max_history:]
    return [
        *system_messages,
        *[MessageInput(role=m.role, content=m.content) for m in recent],
        MessageInput(role="user", content=user_message),
    ]


# ---------------------------------------------------------------------------
# Main turn
# ---------------------------------------------------------------------------


async def run_tutor_turn(
    session_id: UUID,
    user_id: UUID,
    user_message: str,
) -> AsyncIterator[str]:
    """Execute one conversational turn.

    Order of operations (important):
      1. Load existing history + profile + session_state + progress +
         grounding in parallel — all are read-only and feed the LLM
         context.
      2. Persist the user's message. Even if the LLM call fails, we
         don't lose what the student wrote.
      3. Stream tokens from the LLM, yielding each one to the caller.
      4. After the stream ends cleanly, persist the assembled assistant
         reply in full.
      5. Fire two fire-and-forget tasks:
            a) Answer-leak guard (existing).
            b) Post-turn state extractor (Phase 9A). Updates
               `session_state` + `student_progress` from the latest
               exchange. Zero latency impact on the user-visible stream.
    """
    settings = get_settings()
    llm = get_llm_client()

    history, profile, session_state, progress, grounding = await asyncio.gather(
        asyncio.to_thread(repo.list_messages, session_id),
        asyncio.to_thread(repo.get_profile, user_id),
        asyncio.to_thread(repo.get_session_state, session_id),
        asyncio.to_thread(repo.get_top_progress, user_id, limit=8),
        build_grounding_context(user_message, "en"),
    )
    await asyncio.to_thread(
        repo.append_message, session_id, "user", user_message
    )

    if settings.grounding_debug_log:
        p = grounding.problem_reference
        o = grounding.openstax_excerpts
        a = grounding.teaching_annotations
        print(
            "tutor_grounding | "
            f"session_id={session_id} | "
            f"L1_problem_chars={len(p or '')} "
            f"L2_openstax_chars={len(o or '')} "
            f"L3_annotations_chars={len(a or '')} | "
            f"L1_on={bool(p and p.strip())} "
            f"L2_on={bool(o and o.strip())} "
            f"L3_on={bool(a and a.strip())} | "
            f"L1_ids={','.join(grounding.problem_hit_ids) or 'none'} "
            f"L3_annotation_ids={','.join(grounding.annotation_hit_ids) or 'none'}",
            flush=True,
        )

    context = _build_context(
        history,
        user_message,
        settings.tutor_max_history_messages,
        profile,
        session_state,
        progress,
        grounding,
    )

    chunks: list[str] = []
    async for token in llm.stream_chat(
        context, max_tokens=settings.tutor_max_response_tokens
    ):
        chunks.append(token)
        yield token

    full_reply = "".join(chunks).strip()
    if full_reply:
        await asyncio.to_thread(
            repo.append_message, session_id, "assistant", full_reply
        )

        if settings.tutor_answer_guard_enabled:
            asyncio.create_task(
                _check_answer_leak(user_message, full_reply, session_id)
            )

        if settings.state_updater_enabled:
            asyncio.create_task(
                state_updater.update_state_after_turn(
                    session_id=session_id,
                    user_id=user_id,
                    user_message=user_message,
                    assistant_reply=full_reply,
                )
            )


async def generate_session_title(first_user_message: str) -> str:
    """Derive a short title from the first user message."""
    settings = get_settings()
    llm = get_llm_client()

    prompt = [
        MessageInput(
            role="system",
            content=(
                "Generate a very short title (3-6 words) summarizing the "
                "user's question. Respond with ONLY the title: no quotes, "
                "no trailing punctuation. Match the language of the question."
            ),
        ),
        MessageInput(role="user", content=first_user_message),
    ]

    raw = await llm.complete(
        prompt,
        model=settings.openai_title_model,
        max_tokens=20,
    )
    return raw.strip().strip('"').strip("'").strip()[:100]
