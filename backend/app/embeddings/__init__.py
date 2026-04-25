"""Embeddings layer.

Single factory: `get_embeddings_client()`. Mirrors the LLM layer pattern
so a future swap (Cohere, local model, etc.) is one line.

`text-embedding-3-small` was chosen because:
  * 1536 dims -> reasonable storage cost (~6 KB/row for 18k rows = ~110 MB)
  * ~5x cheaper than `text-embedding-3-large`
  * MTEB scores are within a couple of points of `large` for retrieval
"""

from functools import lru_cache

from app.embeddings.openai_embeddings import OpenAIEmbeddingsClient


@lru_cache
def get_embeddings_client() -> OpenAIEmbeddingsClient:
    return OpenAIEmbeddingsClient()


__all__ = ["OpenAIEmbeddingsClient", "get_embeddings_client"]
