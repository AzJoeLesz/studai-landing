"""Retrieval for the math tutor: problem bank, OpenStax chunks, annotations.

The tutor is grounded in three optional layers (all private system context):
  1) Similar *problems* and their worked solutions (existing corpus RAG).
  2) Similar *OpenStax* textbook excerpts (ingested from extracted PDFs).
  3) *Precomputed teaching annotations* for similar problems, when present.

A single user-message embedding is reused for (1) and (2) to save latency
and cost. Annotation lookup uses the top problem hits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from uuid import UUID

from app.core.config import get_settings
from app.db import repositories as repo
from app.db.schemas import Language, ProblemSearchResult, TeachingMaterialHit
from app.embeddings import get_embeddings_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroundingContext:
    """Optional system snippets to append after profile (in this order)."""

    problem_reference: str | None = None
    openstax_excerpts: str | None = None
    teaching_annotations: str | None = None


# --- Problem bank -------------------------------------------------------------


async def find_relevant_problems(
    query: str,
    language: Language,
    *,
    top_k: int,
    similarity_threshold: float,
    query_embedding: list[float] | None = None,
) -> list[ProblemSearchResult]:
    if not query.strip():
        return []
    try:
        embeddings = get_embeddings_client()
        vec = query_embedding
        if vec is None:
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
        logger.warning("Retrieval: problem search failed", exc_info=True)
        return []

    return [h for h in hits if h.similarity >= similarity_threshold]


def format_reference_solutions(hits: list[ProblemSearchResult]) -> str | None:
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
        lines.append(
            f"[#{i}] similarity={hit.similarity:.2f}  type={hit.type}  "
            f"difficulty={hit.difficulty or 'n/a'}"
        )
        lines.append(f"PROBLEM: {hit.problem}")
        lines.append(f"WORKED SOLUTION: {hit.solution}")
        if hit.answer:
            lines.append(f"FINAL ANSWER: {hit.answer}")
        lines.append("")

    return "\n".join(lines).rstrip()


# --- OpenStax material --------------------------------------------------------


async def find_relevant_material(
    query: str,
    *,
    top_k: int,
    similarity_threshold: float,
    query_embedding: list[float] | None = None,
) -> list[TeachingMaterialHit]:
    if not query.strip():
        return []
    try:
        embeddings = get_embeddings_client()
        vec = query_embedding
        if vec is None:
            vec = await embeddings.embed_one(query)
    except Exception:
        logger.warning("Retrieval: embedding call failed (material)", exc_info=True)
        return []

    try:
        hits = await asyncio.to_thread(
            repo.search_teaching_material,
            vec,
            match_count=top_k,
        )
    except Exception:
        logger.warning("Retrieval: teaching material search failed", exc_info=True)
        return []

    return [h for h in hits if h.similarity >= similarity_threshold]


def format_openstax_excerpts(hits: list[TeachingMaterialHit]) -> str | None:
    if not hits:
        return None

    lines: list[str] = [
        "OPENTSTAX EXCERPTS (private -- CC-BY source material).",
        "Short textbook passages for definitions and standard methods. Do not",
        "read these aloud verbatim; use them to stay mathematically correct.",
        "Cite ideas in your own words. If a passage is irrelevant, ignore it.",
        "---",
    ]
    for i, h in enumerate(hits, start=1):
        lines.append(
            f"[#{i}] {h.source}:{h.book_slug} p.{h.page_start}-{h.page_end} "
            f"sim={h.similarity:.2f}"
        )
        lines.append(h.body)
        lines.append("")

    return "\n".join(lines).rstrip()


# --- Precomputed problem annotations -----------------------------------------


def format_teaching_annotations(
    problem_hits: list[ProblemSearchResult],
    by_id: dict[UUID, dict],
) -> str | None:
    if not problem_hits or not by_id:
        return None

    blocks: list[str] = [
        "PRECOMPUTED TEACHING NOTES (private -- do not recite as your own plan).",
        "Structured hints, mistakes, and outlines for problems similar to the",
        "student's. Use to calibrate Socratic questions; never dump answers.",
        "---",
    ]
    any_note = False
    for i, hit in enumerate(problem_hits, start=1):
        row = by_id.get(hit.id)
        if not row:
            continue
        payload = row.get("payload")
        if payload is None:
            continue
        any_note = True
        show = (
            json.dumps(payload, ensure_ascii=False, indent=0)
            if isinstance(payload, (dict, list))
            else str(payload)
        )
        blocks.append(
            f"[note #{i}] problem_hit similarity={hit.similarity:.2f} "
            f"type={hit.type} id={hit.id}"
        )
        blocks.append(show)
        blocks.append("")

    if not any_note:
        return None
    return "\n".join(blocks).rstrip()


# --- One-shot assembly --------------------------------------------------------


async def build_grounding_context(user_message: str, language: Language) -> GroundingContext:
    """One embedding; then problem RAG, material RAG, optional annotation join."""
    settings = get_settings()
    query = user_message.strip()
    if not query:
        return GroundingContext()

    use_problem = bool(settings.rag_enabled)
    use_material = bool(settings.material_rag_enabled)
    use_anno = bool(settings.annotation_injection_enabled)
    if not (use_problem or use_material):
        return GroundingContext()

    vec: list[float] | None = None
    if use_problem or use_material:
        try:
            vec = await get_embeddings_client().embed_one(query)
        except Exception:
            logger.warning("Retrieval: grounding embed failed", exc_info=True)
            vec = None

    prob_hits: list[ProblemSearchResult] = []
    if use_problem:
        if vec is not None:
            prob_hits = await find_relevant_problems(
                query,
                language,
                top_k=settings.rag_top_k,
                similarity_threshold=settings.rag_similarity_threshold,
                query_embedding=vec,
            )
        else:
            prob_hits = await find_relevant_problems(
                query,
                language,
                top_k=settings.rag_top_k,
                similarity_threshold=settings.rag_similarity_threshold,
            )

    mat_hits: list[TeachingMaterialHit] = []
    if use_material:
        if vec is not None:
            mat_hits = await find_relevant_material(
                query,
                top_k=settings.material_rag_top_k,
                similarity_threshold=settings.material_rag_threshold,
                query_embedding=vec,
            )
        else:
            mat_hits = await find_relevant_material(
                query,
                top_k=settings.material_rag_top_k,
                similarity_threshold=settings.material_rag_threshold,
            )

    ann_text: str | None = None
    if use_anno and prob_hits:
        ids = [h.id for h in prob_hits]
        by_id: dict[UUID, dict] = await asyncio.to_thread(
            repo.get_annotations_for_problem_ids,
            ids,
        )
        ann_text = format_teaching_annotations(prob_hits, by_id)

    return GroundingContext(
        problem_reference=format_reference_solutions(prob_hits) if use_problem else None,
        openstax_excerpts=format_openstax_excerpts(mat_hits) if use_material else None,
        teaching_annotations=ann_text,
    )

