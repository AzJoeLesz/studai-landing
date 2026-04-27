"""Application settings, loaded once at startup from the environment.

Every other module should import `get_settings()` rather than reading
environment variables directly. This makes it trivial to swap configuration
in tests or when we add new deploy targets.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_bool_forgiving(v: Any) -> bool:
    """Parse bool from env; tolerate Railway/copy-paste noise (e.g. ' =true', '=true')."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        while s.startswith("="):
            s = s[1:].strip()
        s_lower = s.lower()
        if s_lower in ("true", "1", "yes", "on"):
            return True
        if s_lower in ("false", "0", "no", "off", ""):
            return False
    return False


GroundingDebugLogFlag = Annotated[bool, BeforeValidator(_env_bool_forgiving)]

# Resolve `.env` to an absolute path under `backend/` so the config loads
# correctly no matter the current working directory. Running scripts from
# `backend/scripts/` was breaking before this.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Defensive: env vars copy-pasted from dashboards often pick up a
        # leading tab or trailing space. Strip them automatically so we
        # don't crash with "Invalid URL" or similar deep inside SDKs.
        str_strip_whitespace=True,
    )

    # --- Supabase -----------------------------------------------------------
    supabase_url: str = Field(..., description="https://xxx.supabase.co")
    supabase_service_role_key: str = Field(
        ..., description="service_role key — NEVER expose on the frontend"
    )
    # User tokens are verified against Supabase's JWKS endpoint (ES256).
    # The public keys live at f"{supabase_url}/auth/v1/.well-known/jwks.json"
    # and are cached in-process by PyJWKClient.

    # --- OpenAI -------------------------------------------------------------
    openai_api_key: str = Field(..., description="OpenAI API key")
    openai_model: str = Field(
        default="gpt-4o-mini", description="Model used for chat replies"
    )
    openai_title_model: str = Field(
        default="gpt-4o-mini", description="Model used for session title generation"
    )
    state_extractor_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model used by the post-turn state extractor (Phase 9A). One "
            "small JSON completion per chat turn; keep cheap by default."
        ),
    )
    placement_judge_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model used by the placement-quiz answer judge (Phase 9E). "
            "Single-token YES/NO output; cheap is fine."
        ),
    )

    # --- Tutor behavior -----------------------------------------------------
    tutor_max_history_messages: int = Field(
        default=20,
        description="How many recent messages to include as LLM context.",
    )
    tutor_max_response_tokens: int = Field(
        default=2000,
        description=(
            "Upper bound on tokens in a single assistant reply. For "
            "reasoning models (gpt-5, o-series) this budget is shared "
            "with the model's internal reasoning -- 2000 leaves room "
            "for both. Pure chat models only count visible output."
        ),
    )
    tutor_reasoning_effort: str = Field(
        default="minimal",
        description=(
            "`reasoning_effort` value for gpt-5 / o-series chat. "
            "`minimal` keeps token budget free for visible output -- "
            "chat tutoring doesn't need deep internal reasoning. "
            "Bump to `low`/`medium`/`high` if quality demands it."
        ),
    )
    tutor_answer_guard_enabled: bool = Field(
        default=True,
        description="Run a post-reply LLM check for accidental answer leaks.",
    )

    # --- Personalization (Phase 9) -----------------------------------------
    state_updater_enabled: bool = Field(
        default=True,
        description=(
            "Run the post-turn state extractor (Phase 9A). Disable to "
            "fall back to the cold/profile-only behavior of Phase 7-8."
        ),
    )
    style_policy_enabled: bool = Field(
        default=True,
        description=(
            "Inject the STYLE DIRECTIVES system block into each chat turn "
            "(Phase 9B). Disable for an A/B test of v2-style behavior."
        ),
    )
    progress_block_enabled: bool = Field(
        default=True,
        description=(
            "Inject the STUDENT PROGRESS block (top topics + mastery) "
            "into each chat turn."
        ),
    )
    session_state_block_enabled: bool = Field(
        default=True,
        description=(
            "Inject the SESSION STATE block (current_topic, mode, "
            "summary, struggling_on, mood_signals) into each chat turn."
        ),
    )
    state_extractor_max_tokens: int = Field(
        default=400,
        description=(
            "Upper bound on the JSON output of the post-turn extractor."
        ),
    )
    topic_classifier_confidence_floor: float = Field(
        default=0.40,
        description=(
            "Cosine-similarity floor for the live topic classifier "
            "(`agents/topic_classifier`). Below this, the classifier "
            "returns None and the register defaults to `at_level`. "
            "Lower values (~0.30) admit weak guesses and produce "
            "register false positives -- a chocolate-bar division "
            "word problem getting matched to `probability basics` at "
            "0.33 and routed to `above_level_exploration`. Higher "
            "values (~0.50) miss some genuine on-topic queries."
        ),
    )

    # --- Retrieval-augmented tutoring (Phase 10 v1) -------------------------
    # On every chat turn, search the problem bank with the student's latest
    # message and inject the worked solutions of the closest matches as
    # private context. The tutor uses them as ground truth without revealing.
    rag_enabled: bool = Field(
        default=True,
        description="Enable problem-bank retrieval before each tutor turn.",
    )
    rag_top_k: int = Field(
        default=2,
        description="How many similar problems to inject into the prompt.",
    )
    rag_similarity_threshold: float = Field(
        default=0.55,
        description=(
            "Cosine similarity floor (0-1). Hits below this are dropped to "
            "avoid polluting the prompt with weak matches."
        ),
    )

    # --- OpenStax text RAG (ingested from books_extracted) -------------------
    material_rag_enabled: bool = Field(
        default=True,
        description="Search embedded OpenStax chunks for each tutor turn.",
    )
    material_rag_top_k: int = Field(
        default=4,
        description="How many OpenStax chunks to add to the prompt.",
    )
    material_rag_threshold: float = Field(
        default=0.40,
        description="Cosine similarity floor for OpenStax chunk hits (0-1).",
    )
    annotation_injection_enabled: bool = Field(
        default=True,
        description=(
            "If problem-bank hits have precomputed `problem_annotations` rows, "
            "include them in system context."
        ),
    )
    grounding_debug_log: GroundingDebugLogFlag = Field(
        default=False,
        description=(
            "If true, log one line per chat turn with character counts for each "
            "grounding layer (problem RAG, OpenStax, annotations). Use when testing "
            "that all three layers resolve (see Railway or local server logs)."
        ),
    )

    # --- CORS ---------------------------------------------------------------
    # Stored as raw string (comma-separated) to avoid pydantic-settings'
    # default JSON-list parsing, which makes Railway env vars awkward.
    cors_origins_raw: str = Field(
        default="https://studai.hu,http://localhost:3000",
        alias="CORS_ORIGINS",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_origins_raw.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Safe to call anywhere, any number of times."""
    return Settings()  # type: ignore[call-arg]
