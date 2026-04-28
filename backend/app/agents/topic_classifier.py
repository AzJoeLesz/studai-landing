"""Topic classifier for the live tutor.

Maps a free-text user message to one of the canonical topic strings in
`grade_priors.json`. Used by the style-policy layer to decide
`register` (topic-grade alignment) without forcing the LLM to do it.

Implementation: precompute (lazily, once per process) an embedding for
every canonical topic label using the same `text-embedding-3-small`
model the rest of the system uses. At classification time, embed the
user message (often we already have this from the RAG pipeline -- see
`agents/retrieval.py` -- so we accept a precomputed vector) and pick
the topic whose centroid has the highest cosine similarity, gated on a
minimum confidence floor.

The cache is process-local and warmed on first call. For an MVP that's
fine; in production we'd persist topic embeddings to Supabase and
hydrate at startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.agents.grade_priors import topic_universe
from app.core.config import get_settings
from app.embeddings import get_embeddings_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TopicClassification:
    topic: str | None
    similarity: float


# Below this similarity, we treat the classification as noise and return
# `None` (caller falls back to `at_level` register). Empirical tuning:
#  * 0.20-0.40 = weakly related on `text-embedding-3-small`
#  * 0.40-0.60 = related
#  * 0.60+     = closely related
# A long story-heavy word problem can score ~0.3 against ANY topic
# centroid (the story words drown the math signal). 0.30 was letting
# clear noise through -- e.g. a chocolate-bar division word problem
# was being matched to "probability basics" at 0.33 and routed to
# `above_level_exploration` for a 4th grader. Bump to 0.40: still
# catches genuine on-topic queries ("what is a derivative?",
# "tell me about parabolas") which score 0.45+, while filtering out
# the weak guesses that cause register false positives.
#
# Override via `TOPIC_CLASSIFIER_CONFIDENCE_FLOOR` env var.
_FALLBACK_CONFIDENCE_FLOOR = 0.40


def _confidence_floor() -> float:
    try:
        return float(get_settings().topic_classifier_confidence_floor)
    except Exception:
        return _FALLBACK_CONFIDENCE_FLOOR


# In-memory centroid cache.
_centroid_cache: dict[str, list[float]] | None = None
_centroid_lock = asyncio.Lock()


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity. Embeddings are already L2-normalized for
    OpenAI's text-embedding-3-small, but we don't depend on that.
    """
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


_DESCRIPTIONS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "topic_descriptions.json"
)


@lru_cache(maxsize=1)
def _topic_descriptions() -> dict[str, str]:
    """Load the (canonical_topic -> embedding source text) map.

    Topics not in the file fall back to embedding the bare canonical
    name. The file may also include a `_meta` entry for documentation
    purposes -- ignored here.
    """
    try:
        raw = json.loads(_DESCRIPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(
            "topic_classifier: failed to load topic_descriptions.json",
            exc_info=True,
        )
        return {}
    return {
        k: v
        for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, str)
    }


async def _ensure_centroids() -> dict[str, list[float]]:
    """Load (and cache) the embedding for every canonical topic label.

    Centroid source text is the topic's entry in
    `data/topic_descriptions.json` if present, otherwise the bare
    topic label. Descriptions give the centroid more semantic surface
    area to match against -- a long student message about chocolate
    bars matches the embedding of "division — splitting things into
    equal groups, sharing equally..." much better than the embedding
    of just "division".
    """
    global _centroid_cache
    if _centroid_cache is not None:
        return _centroid_cache
    async with _centroid_lock:
        if _centroid_cache is not None:
            return _centroid_cache
        topics = list(topic_universe())
        if not topics:
            logger.warning("topic_classifier: empty topic universe")
            _centroid_cache = {}
            return _centroid_cache
        descriptions = _topic_descriptions()
        embedding_sources = [descriptions.get(t, t) for t in topics]
        embeddings = get_embeddings_client()
        try:
            vecs = await embeddings.embed_batch(embedding_sources)
        except Exception:
            logger.warning(
                "topic_classifier: failed to compute centroids; "
                "classification disabled this process",
                exc_info=True,
            )
            _centroid_cache = {}
            return _centroid_cache
        # Cache key is the canonical topic name (what we return), not
        # the description text (which is what we embedded).
        _centroid_cache = dict(zip(topics, vecs))
        with_desc = sum(1 for t in topics if t in descriptions)
        logger.info(
            "topic_classifier: built %d centroids (%d with enriched "
            "descriptions, %d falling back to bare names)",
            len(topics),
            with_desc,
            len(topics) - with_desc,
        )
        return _centroid_cache


async def classify_topic(
    message: str,
    *,
    query_embedding: list[float] | None = None,
    confidence_floor: float | None = None,
) -> TopicClassification:
    """Return the nearest canonical topic for `message`, or None if low-confidence.

    `query_embedding` lets the caller reuse the embedding it already
    computed for RAG (see `agents/retrieval.build_grounding_context`).
    Saves one OpenAI call per turn.

    `confidence_floor=None` reads the configured floor at call time
    (see `Settings.topic_classifier_confidence_floor`).
    """
    if confidence_floor is None:
        confidence_floor = _confidence_floor()
    if not message.strip():
        return TopicClassification(None, 0.0)

    centroids = await _ensure_centroids()
    if not centroids:
        return TopicClassification(None, 0.0)

    vec = query_embedding
    if vec is None:
        try:
            vec = await get_embeddings_client().embed_one(message)
        except Exception:
            logger.warning(
                "topic_classifier: embed_one failed", exc_info=True
            )
            return TopicClassification(None, 0.0)

    best_topic: str | None = None
    best_sim = 0.0
    for topic, centroid in centroids.items():
        sim = _cosine(vec, centroid)
        if sim > best_sim:
            best_sim = sim
            best_topic = topic

    if best_topic is None or best_sim < confidence_floor:
        return TopicClassification(None, best_sim)
    return TopicClassification(best_topic, best_sim)
