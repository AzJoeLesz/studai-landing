"""LLM layer.

Single factory: `get_llm_client()`. Change the body of that function (one
line) to switch provider; none of the callers need to know.
"""

from functools import lru_cache

from app.llm.base import LLMClient
from app.llm.openai_client import OpenAIClient


@lru_cache
def get_llm_client() -> LLMClient:
    return OpenAIClient()


__all__ = ["LLMClient", "get_llm_client"]
