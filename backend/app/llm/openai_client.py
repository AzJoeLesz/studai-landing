"""OpenAI implementation of LLMClient.

Uses the official `openai` SDK's async client so we can `await` the
streaming response and yield tokens one at a time.
"""

from typing import AsyncIterator

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.db.schemas import MessageInput
from app.llm.base import LLMClient


class OpenAIClient(LLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=api_key or settings.openai_api_key)
        self._default_model = default_model or settings.openai_model

    @staticmethod
    def _to_openai(messages: list[MessageInput]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def stream_chat(
        self,
        messages: list[MessageInput],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=model or self._default_model,
            messages=self._to_openai(messages),  # type: ignore[arg-type]
            stream=True,
            max_tokens=max_tokens,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def complete(
        self,
        messages: list[MessageInput],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=model or self._default_model,
            messages=self._to_openai(messages),  # type: ignore[arg-type]
            stream=False,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""
