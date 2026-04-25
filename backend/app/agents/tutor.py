"""Tutor orchestration.

This is the brain of StudAI. Today it is a single function: load history,
append user message, stream an LLM reply, persist the reply. When we add
tools (SymPy for step-by-step algebra, a problem bank, etc.) or split
tutoring into sub-agents, only this file's body changes — the API layer
keeps calling `run_tutor_turn(session_id, user_id, user_message)` and
getting back a token stream.

Design rules for this file:
  * The API layer must never import from `llm/` or `prompts/` directly.
  * Database access goes through `db.repositories`.
  * This is the ONLY place that knows about the tutor's persona.
"""

import asyncio
import logging
from typing import AsyncIterator
from uuid import UUID

from app.agents.retrieval import GroundingContext, build_grounding_context
from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import Message, MessageInput, Profile
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
    """Fire-and-forget guard: checks if the reply leaked the answer.

    Runs after the response has already been streamed to the user, so it
    adds zero latency to the chat. Its purpose is monitoring — the logs
    let us spot prompt weaknesses and iterate.
    """
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
    grounding: GroundingContext | None = None,
) -> list[MessageInput]:
    """Assemble the message list we send to the LLM.

    Strategy v1: system prompt + last N messages + the new user turn.
    Strategy v2 (later): system prompt + running summary + last N messages.
    """
    system_messages: list[MessageInput] = [
        MessageInput(role="system", content=CURRENT_TUTOR_PROMPT),
    ]
    profile_snippet = _format_profile_snippet(profile)
    if profile_snippet:
        # Separate system message keeps the persona prompt frozen and the
        # profile context easy to inspect/replace independently.
        system_messages.append(
            MessageInput(role="system", content=profile_snippet)
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
      1. Load existing history + the student's profile in parallel -- both
         are read-only and feed the LLM context.
      2. Persist the user's message. Even if the LLM call fails afterwards,
         we don't lose what the student wrote.
      3. Stream tokens from the LLM, yielding each one to the caller.
      4. After the stream ends cleanly, persist the assembled assistant
         reply in full.
      5. Fire the answer-leak guard asynchronously (no latency impact).

    Returns an async generator of string tokens. Caller (the API route) is
    responsible for framing them into the wire format (SSE, WebSocket, etc.).
    """
    settings = get_settings()
    llm = get_llm_client()

    # All three reads are independent -- pull them in parallel so retrieval
    # latency overlaps with profile + history loads.
    history, profile, grounding = await asyncio.gather(
        asyncio.to_thread(repo.list_messages, session_id),
        asyncio.to_thread(repo.get_profile, user_id),
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


async def generate_session_title(first_user_message: str) -> str:
    """Derive a short title from the first user message.

    Called once, right after the first assistant reply finishes. Failures
    here are non-fatal — a sessionless title just stays NULL.
    """
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
