"""Problem bank search endpoint.

The tutor grounds replies via RAG in `app.agents.retrieval` (not via this
HTTP route). This endpoint is the read API for the corpus: signed-in
semantic search for admin and future UIs.

Why a single search endpoint instead of full CRUD:
  * Problems are READ-ONLY content for end users. Mutation happens only
    via offline ingestion scripts.
  * One semantic-search verb covers every UI surface we'll need
    (admin browsing, "find me problems like this one", AI tool calls).
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser
from app.db import repositories as repo
from app.db.schemas import Language, ProblemSearchResult
from app.embeddings import get_embeddings_client

router = APIRouter(prefix="/problems", tags=["problems"])


@router.get("/search", response_model=list[ProblemSearchResult])
async def search_problems(
    user: CurrentUser,
    q: str = Query(..., min_length=2, max_length=2000, description="Free-text query."),
    language: Language = Query("en", description="'en' or 'hu' (falls back to 'en' if no translation)."),
    limit: int = Query(10, ge=1, le=50),
    type: str | None = Query(None, description="Filter by problem type (e.g. 'Algebra')."),
    difficulty: str | None = Query(
        None, description="Filter by difficulty (e.g. 'Level 3', 'easy_medium')."
    ),
) -> list[ProblemSearchResult]:
    """Semantic search over the problem bank.

    Embeds `q` once, then runs cosine similarity against the embeddings
    stored in the `language` corpus. If a problem has no `language`
    translation, it's still findable via its English embedding -- but only
    if the English embedding was indexed (it is, by default).

    Authentication: any signed-in user can search (problems are shared
    content). Quota / rate-limiting will land later if abuse becomes real.
    """
    embeddings = get_embeddings_client()
    try:
        vec = await embeddings.embed_one(q)
    except Exception as exc:  # OpenAI errors, network, etc.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Embedding service failed: {type(exc).__name__}",
        ) from exc

    results = await asyncio.to_thread(
        repo.search_problems,
        vec,
        language,
        match_count=limit,
        filter_type=type,
        filter_difficulty=difficulty,
    )
    return results
