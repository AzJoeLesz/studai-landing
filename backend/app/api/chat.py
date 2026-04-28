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

In addition we emit periodic SSE comment lines (`: keep-alive\n\n`)
during silence. Comments are ignored by SSE clients (per spec), but
they keep the underlying TCP connection live so reverse proxies
(Railway, Vercel, CloudFlare) don't tear it down for being idle.
That was the cause of the `RemoteProtocolError: ConnectionTerminated`
errors we observed when the LLM (especially gpt-5-mini with
internal reasoning) took several seconds before emitting any token.

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

# Emit a `:keep-alive\n\n` SSE comment if no real frame has been
# yielded for this many seconds. Long enough that we don't waste
# bytes on every fast turn; short enough to beat typical proxy
# idle timeouts (Railway is ~30s, CloudFlare/AWS are ~60s).
_HEARTBEAT_INTERVAL_SECONDS = 15.0


class ChatRequest(BaseModel):
    session_id: UUID
    message: str = Field(..., min_length=1, max_length=8000)


def _sse_frame(event: str, data: str) -> bytes:
    # SSE uses \n as a delimiter between fields, so embedded newlines in the
    # payload need to be escaped. The frontend reverses this.
    safe = data.replace("\r", "").replace("\n", "\\n")
    return f"event: {event}\ndata: {safe}\n\n".encode("utf-8")


async def _with_heartbeat(
    inner: AsyncIterator[bytes],
    *,
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[bytes]:
    """Wrap an SSE byte-stream and inject a `:keep-alive` comment when idle.

    Pulls items from `inner` in a background task and forwards them
    through a queue. If the queue is silent for `interval` seconds, we
    emit a comment line. Comment lines (lines starting with `:`) are
    ignored by EventSource per the SSE spec, so the frontend doesn't
    need any special handling.
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def runner() -> None:
        try:
            async for item in inner:
                await queue.put(item)
        except BaseException as exc:  # noqa: BLE001
            await queue.put(exc)
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(runner())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                # Surface the inner exception to the StreamingResponse so
                # FastAPI logs it; the inner stream already converted
                # known errors into `error` SSE frames.
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


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

    # One line per request in Railway deploy logs (we do not log message text).
    print(
        "chat_stream_started | "
        f"session_id={payload.session_id} user_id={user.user_id} "
        f"msg_chars={len(payload.message)} first_turn={is_first_turn}",
        flush=True,
    )

    return StreamingResponse(
        _with_heartbeat(
            _chat_stream(
                payload.session_id,
                user.user_id,
                payload.message,
                is_first_turn,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells reverse proxies not to buffer
        },
    )
