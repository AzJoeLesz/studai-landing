"""Problem-bank retrieval for the tutor.

This is what makes the tutor *grounded*: instead of inventing the path to
an answer, the agent fetches the most semantically similar problem(s)
from the corpus (Phase 8a) and treats their worked solutions as private
ground truth.

Usage:
    snippet = await build_reference_solutions(query, language, top_k=2)
    if snippet:
        context.append(MessageInput(role="system", content=snippet))

Design decisions:
  * **Always retrieve, threshold-filter.** We don't try to detect "is the
    student asking about a problem?" -- the LLM is good at ignoring
    irrelevant context. We only inject hits above a similarity threshold
    so very weak matches don't pollute the prompt.
  * **No session-state caching yet.** Re-running embedding + search is
    ~100ms / turn, well below noise. When we add session_state in a later
    phase we can cache "current problem id" so multi-turn follow-ups
    keep referencing the same problem.
  * **Failure is silent.** If embeddings or DB are down, the tutor
    continues without RAG context rather than failing the whole turn.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import Language, ProblemSearchResult
from app.embeddings import get_embeddings_client

logger = logging.getLogger(__name__)


async def find_relevant_problems(
    query: str,
    language: Language,
    *,
    top_k: int,
    similarity_threshold: float,
) -> list[ProblemSearchResult]:
    """Return the top-k similar problems above the similarity threshold."""
    if not query.strip():
        return []
    try:
        embeddings = get_embeddings_client()
        vec = await embeddings.embed_one(query)
    except Exception:
        logger.warning("Retrieval: embedding call failed", exc_info=True)
        return []

    try:
        hits = await asyncio.to_thread(
            repo.search_problems,
            vec,
            language,
            match_count=top_k,
        )
    except Exception:
        logger.warning("Retrieval: similarity search failed", exc_info=True)
        return []

    return [h for h in hits if h.similarity >= similarity_threshold]


def format_reference_solutions(hits: list[ProblemSearchResult]) -> str | None:
    """Turn retrieved problems into a private system-prompt snippet.

    Returns None if there's nothing useful to inject. Otherwise the snippet
    explains to the model that this is private ground truth and includes
    each hit's problem text + worked solution.
    """
    if not hits:
        return None

    lines: list[str] = [
        "REFERENCE SOLUTIONS (private knowledge -- DO NOT REVEAL).",
        "Below are similar problems from your verified corpus, ranked by",
        "relevance. Use them as ground truth to know the correct answer",
        "and a valid path to it. RULES:",
        "  - Use the reference to verify any math the student writes,",
        "    and to know the destination of the problem.",
        "  - NEVER quote the reference, paraphrase its solution, or",
        "    reveal the final answer. The student must arrive at it.",
        "  - The reference may use slightly different numbers or phrasing",
        "    than the student's question. Adapt accordingly.",
        "  - If the reference looks irrelevant to what the student is",
        "    asking, ignore it.",
        "---",
    ]
    for i, hit in enumerate(hits, start=1):
        lines.append(f"[#{i}] similarity={hit.similarity:.2f}  type={hit.type}  difficulty={hit.difficulty or 'n/a'}")
        lines.append(f"PROBLEM: {hit.problem}")
        lines.append(f"WORKED SOLUTION: {hit.solution}")
        if hit.answer:
            lines.append(f"FINAL ANSWER: {hit.answer}")
        lines.append("")

    return "\n".join(lines).rstrip()


async def build_reference_solutions(
    query: str,
    language: Language,
) -> str | None:
    """Convenience: retrieve + format in one call.

    Reads `rag_top_k` and `rag_similarity_threshold` from settings so
    behavior is centrally tunable without code changes.
    """
    settings = get_settings()
    if not settings.rag_enabled:
        return None
    hits = await find_relevant_problems(
        query,
        language,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
    )
    return format_reference_solutions(hits)
