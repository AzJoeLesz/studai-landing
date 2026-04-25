"""OpenAI embeddings client.

Two operations:
  * `embed_one(text)` -> single 1536-dim vector
  * `embed_batch(texts)` -> list of 1536-dim vectors, batched efficiently

OpenAI's batch endpoint accepts up to 2048 inputs and ~8192 tokens per
input. We batch at 96 inputs per request to keep individual requests under
~5s and leave headroom for retries. Big ingestions should run this in
multiple parallel tasks.
"""

import asyncio
from typing import Sequence

from openai import AsyncOpenAI

from app.core.config import get_settings

DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIM = 1536
MAX_BATCH = 96


class OpenAIEmbeddingsClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=api_key or settings.openai_api_key)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        # Hardcoded for `text-embedding-3-small`. If we swap models, the SQL
        # column type (vector(1536)) becomes wrong too -- intentional coupling.
        return DEFAULT_DIM

    async def embed_one(self, text: str) -> list[float]:
        result = await self.embed_batch([text])
        return result[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed many texts. Splits into <=MAX_BATCH chunks under the hood."""
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), MAX_BATCH):
            chunk = list(texts[i : i + MAX_BATCH])
            res = await self._client.embeddings.create(
                model=self._model,
                input=chunk,
            )
            out.extend(d.embedding for d in res.data)
        return out

    async def embed_concurrent(
        self,
        texts: Sequence[str],
        concurrency: int = 4,
    ) -> list[list[float]]:
        """Embed many texts with N concurrent batch requests.

        Order of results matches order of inputs. Use this for big ingestions.
        """
        if not texts:
            return []

        # Pre-split into batches preserving original index ranges.
        batches: list[tuple[int, list[str]]] = []
        for i in range(0, len(texts), MAX_BATCH):
            batches.append((i, list(texts[i : i + MAX_BATCH])))

        sem = asyncio.Semaphore(concurrency)

        async def _run(start: int, chunk: list[str]) -> tuple[int, list[list[float]]]:
            async with sem:
                res = await self._client.embeddings.create(
                    model=self._model,
                    input=chunk,
                )
                return start, [d.embedding for d in res.data]

        results = await asyncio.gather(
            *(_run(start, chunk) for start, chunk in batches)
        )
        # Reassemble in order.
        results.sort(key=lambda x: x[0])
        out: list[list[float]] = []
        for _, chunk_vecs in results:
            out.extend(chunk_vecs)
        return out
