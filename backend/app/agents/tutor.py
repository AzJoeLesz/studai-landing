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
from typing import AsyncIterator
from uuid import UUID

from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import Message, MessageInput
from app.llm import get_llm_client
from app.prompts import CURRENT_TUTOR_PROMPT


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
