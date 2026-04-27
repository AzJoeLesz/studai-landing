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
_NEW_TOKEN_PARAM_PREFIXES: tuple[str, ...] = (
    "gpt-5",
    "gpt-4.1",
    "o1",
    "o3",
    "o4",
)

# Models that internally do reasoning before producing visible output,
# and bill those reasoning tokens against `max_completion_tokens`. For
# tutoring chat we want short Socratic replies, NOT deep reasoning,
# so default to "minimal" effort. Without this, gpt-5-mini sometimes
# burns the entire token budget on reasoning and emits zero visible
# tokens -- the chat looks dead from the user's perspective.
#
# gpt-4.1 family is on the new max_completion_tokens parameter but
# does NOT use the reasoning protocol, so excluded here.
_REASONING_PREFIXES: tuple[str, ...] = (
    "gpt-5",
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


def _reasoning_kwargs(model: str) -> dict:
    """Return reasoning-effort kwargs for models that support them.

    Defaults to `reasoning_effort="minimal"` for GPT-5 / o-series,
    which keeps tokens available for visible output. Returns an empty
    dict for models without the parameter.
    """
    model_lc = (model or "").lower()
    if any(model_lc.startswith(p) for p in _REASONING_PREFIXES):
        settings = get_settings()
        return {"reasoning_effort": settings.tutor_reasoning_effort}
    return {}


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
            **_reasoning_kwargs(chosen_model),
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
            **_reasoning_kwargs(chosen_model),
        )
        return response.choices[0].message.content or ""
