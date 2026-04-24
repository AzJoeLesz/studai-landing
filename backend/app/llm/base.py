"""Provider-agnostic LLM interface.

Only two operations exist in the contract:

  * `stream_chat` — yield tokens as they arrive. Used for chat replies so
    the frontend can render them progressively.
  * `complete`   — one-shot call. Used for lightweight utility calls like
    session title generation.

If you're adding a second provider (Anthropic, a local model, etc.), create
a sibling of `OpenAIClient` that subclasses `LLMClient`, then change the
`get_llm_client()` factory in `app/llm/__init__.py`.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.db.schemas import MessageInput


class LLMClient(ABC):
    @abstractmethod
    def stream_chat(
        self,
        messages: list[MessageInput],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Async generator yielding text chunks as the model produces them."""
        raise NotImplementedError

    @abstractmethod
    async def complete(
        self,
        messages: list[MessageInput],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return the full response as a single string."""
        raise NotImplementedError
