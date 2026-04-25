"""Tutor orchestration.

This is the brain of StudAI. Today it is a single function: load history,
append user message, stream an LLM reply, persist the reply. When we add
tools (SymPy for step-by-step algebra, a problem bank, etc.) or split
tutoring into sub-agents, only this file's body changes — the API layer
keeps calling `run_tutor_turn(session_id, user_message)` and getting back
a token stream.

Design rules for this file:
  * The API layer must never import from `llm/` or `prompts/` directly.
  * Database access goes through `db.repositories`.
  * This is the ONLY place that knows about the tutor's persona.
"""

import asyncio
import logging
from typing import AsyncIterator
from uuid import UUID

from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import Message, MessageInput
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
# Context assembly
# ---------------------------------------------------------------------------


def _build_context(
    history: list[Message],
    user_message: str,
    max_history: int,
) -> list[MessageInput]:
    """Assemble the message list we send to the LLM.

    Strategy v1: system prompt + last N messages + the new user turn.
    Strategy v2 (later): system prompt + running summary + last N messages.
    """
    recent = history[-max_history:]
    return [
        MessageInput(role="system", content=CURRENT_TUTOR_PROMPT),
        *[MessageInput(role=m.role, content=m.content) for m in recent],
        MessageInput(role="user", content=user_message),
    ]


# ---------------------------------------------------------------------------
# Main turn
# ---------------------------------------------------------------------------


async def run_tutor_turn(
    session_id: UUID,
    user_message: str,
) -> AsyncIterator[str]:
    """Execute one conversational turn.

    Order of operations (important):
      1. Load existing history — this is what gets sent to the LLM as context.
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

    history = await asyncio.to_thread(repo.list_messages, session_id)
    await asyncio.to_thread(
        repo.append_message, session_id, "user", user_message
    )

    context = _build_context(
        history, user_message, settings.tutor_max_history_messages
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
