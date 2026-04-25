"""Streaming chat endpoint.

Wire format: Server-Sent Events (SSE). Each frame looks like:

    event: <name>
    data:  <payload>
    <blank line>

Events this endpoint emits, in order:
  * `token` (many)  — chunks of the assistant's reply as they arrive
  * `title` (0 or 1) — only sent on the first turn of a session
  * `done`  (1)     — signals clean end of stream
  * `error` (0 or 1) — replaces `done` if something went wrong midway

The browser reads the stream and renders tokens as they appear, which is
what gives the chat a ChatGPT-like live-typing feel.
"""

import asyncio
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agents.tutor import generate_session_title, run_tutor_turn
from app.api.deps import CurrentUser
from app.db import repositories as repo

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: UUID
    message: str = Field(..., min_length=1, max_length=8000)


def _sse_frame(event: str, data: str) -> bytes:
    # SSE uses \n as a delimiter between fields, so embedded newlines in the
    # payload need to be escaped. The frontend reverses this.
    safe = data.replace("\r", "").replace("\n", "\\n")
    return f"event: {event}\ndata: {safe}\n\n".encode("utf-8")


async def _chat_stream(
    session_id: UUID,
    user_id: UUID,
    user_message: str,
    is_first_turn: bool,
) -> AsyncIterator[bytes]:
    try:
        async for token in run_tutor_turn(session_id, user_id, user_message):
            yield _sse_frame("token", token)

        if is_first_turn:
            try:
                title = await generate_session_title(user_message)
                await asyncio.to_thread(
                    repo.update_session_title, session_id, title
                )
                yield _sse_frame("title", title)
            except Exception:
                # A missing title must never break a successful conversation.
                pass

        yield _sse_frame("done", "")
    except Exception as exc:  # pragma: no cover — surfaced to the client
        yield _sse_frame("error", f"{type(exc).__name__}: {exc}")


@router.post("")
async def chat(payload: ChatRequest, user: CurrentUser) -> StreamingResponse:
    session = await asyncio.to_thread(
        repo.get_session_for_user, payload.session_id, user.user_id
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")

    existing = await asyncio.to_thread(repo.list_messages, payload.session_id)
    is_first_turn = not any(m.role == "user" for m in existing)

    return StreamingResponse(
        _chat_stream(
            payload.session_id, user.user_id, payload.message, is_first_turn
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells reverse proxies not to buffer
        },
    )
