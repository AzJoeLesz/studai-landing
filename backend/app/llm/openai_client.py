"""OpenAI implementation of LLMClient.

Uses the official `openai` SDK's async client so we can `await` the
streaming response and yield tokens one at a time.
"""

from typing import AsyncIterator

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.db.schemas import MessageInput
from app.llm.base import LLMClient


# Models that REQUIRE the newer `max_completion_tokens` parameter
# instead of the legacy `max_tokens`. The OpenAI API rejects
# `max_tokens` on these with a 400. Match by prefix.
#
# Sources for inclusion:
#   - GPT-5 family announcement: max_completion_tokens required.
#   - o-series (o1, o3, o4): same.
#   - GPT-4.1 family: same.
#
# Older families (gpt-4o, gpt-4o-mini, gpt-3.5-turbo, etc.) still
# accept the legacy `max_tokens` parameter, so we keep using it for
# backward compatibility -- some have not yet shipped support for
# `max_completion_tokens` on every endpoint.
_NEW_TOKEN_PARAM_PREFIXES: tuple[str, ...] = (
    "gpt-5",
    "gpt-4.1",
    "o1",
    "o3",
    "o4",
)


def _token_limit_kwargs(model: str, max_tokens: int | None) -> dict:
    """Return the right OpenAI keyword for capping output length.

    GPT-5 family + o-series + gpt-4.1 require `max_completion_tokens`;
    older families want `max_tokens`. Sending the wrong one is a 400
    error from the API ("Unsupported parameter: 'max_tokens' is not
    supported with this model. Use 'max_completion_tokens' instead.").

    `None` -> empty dict (no cap, server default applies).
    """
    if max_tokens is None:
        return {}
    model_lc = (model or "").lower()
    if any(model_lc.startswith(p) for p in _NEW_TOKEN_PARAM_PREFIXES):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


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
        chosen_model = model or self._default_model
        stream = await self._client.chat.completions.create(
            model=chosen_model,
            messages=self._to_openai(messages),  # type: ignore[arg-type]
            stream=True,
            **_token_limit_kwargs(chosen_model, max_tokens),
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
        chosen_model = model or self._default_model
        response = await self._client.chat.completions.create(
            model=chosen_model,
            messages=self._to_openai(messages),  # type: ignore[arg-type]
            stream=False,
            **_token_limit_kwargs(chosen_model, max_tokens),
        )
        return response.choices[0].message.content or ""
