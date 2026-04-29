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
    placement_reranker_enabled: bool = Field(
        default=True,
        description=(
            "Run an LLM reranker over the top-N semantic-search "
            "candidates for each placement question, instead of just "
            "taking the first hit that passes filters. Costs one "
            "small LLM call per question (~$0.0005 on gpt-4o-mini). "
            "Worth it when the corpus has many topic-adjacent matches "
            "(see iteration #11 in docs/phase9_personalization.md)."
        ),
    )
    placement_reranker_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model used by the placement-quiz candidate reranker. "
            "~500-token input, single-integer output. gpt-4o-mini "
            "is plenty for ranking 5-15 candidates."
        ),
    )
    answer_guard_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model used by the post-reply answer-leak guard "
            "(`tutor._check_answer_leak`). Single-token YES/NO output, "
            "fires once per assistant turn. Kept on a cheap model "
            "independently of `OPENAI_MODEL` so bumping the chat to "
            "gpt-5 / gpt-5-mini doesn't accidentally also bill 10x for "
            "the leak guard. Bump only if you start seeing missed "
            "leaks in Railway logs."
        ),
    )

    # --- Phase 10: solution graphs -----------------------------------------
    # See docs/phase10_solution_graphs.md for the full design rationale.
    # Generation is offline (scripts/generate_solution_paths.py) and the
    # step evaluator is a per-turn pre-LLM call (10B). All four knobs
    # default to safe values; override via env in production only when
    # quality demands it.
    path_gen_model: str = Field(
        default="gpt-5-mini",
        description=(
            "Model used by `scripts/generate_solution_paths.py` to "
            "produce solution paths + step hints + common mistakes. "
            "Decision H (lock): gpt-5-mini for quality on long "
            "structured-JSON output (~2000 tokens out per problem). "
            "Cost is rounding noise (~$20 for 500-700 problems). "
            "Downgrade to gpt-4o-mini if outputs disappoint at scale."
        ),
    )
    path_critic_model: str = Field(
        default="gpt-5",
        description=(
            "Model used by the LLM-as-judge pre-filter that scores "
            "every generated path on (correctness, hint quality, "
            "mistake plausibility, step granularity) before it hits "
            "the human verification queue. Decision N (lock): use a "
            "STRONGER model than the generator so the critic can "
            "actually catch generator errors. ~$0.01/path => "
            "~$5-10 for 500-700 problems."
        ),
    )
    step_evaluator_model: str = Field(
        default="gpt-4o-mini",
        description=(
            "Model used by the per-turn step evaluator (Phase 10B). "
            "BLOCKING pre-LLM call (Decision D): classifies the "
            "student's latest message vs. the current step's "
            "expected_action. Hard-clamped by `step_evaluator_timeout_ms`. "
            "Cheap model is fine -- the task is short JSON output."
        ),
    )
    step_evaluator_timeout_ms: int = Field(
        default=600,
        description=(
            "Hard timeout on the step evaluator LLM call, in ms. On "
            "timeout we degrade gracefully -- the main LLM runs "
            "without an evaluator signal (Phase 10's GUIDED PATH "
            "block falls back to its 'no_step_yet' template). 600ms "
            "keeps the worst-case TTFT regression bounded."
        ),
    )
    guided_mode_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for Phase 10 guided mode. Off = act "
            "exactly like Phase 9 (no eligibility check, no "
            "evaluator, no GUIDED PATH block). Useful for A/B "
            "testing v3-only vs v3+guided once Phase 12 ratings exist."
        ),
    )
    guided_mode_similarity_threshold: float = Field(
        default=0.85,
        description=(
            "Cosine-similarity floor on the top RAG hit for a new "
            "guided-mode activation. Decision F (lock): 0.85 (vs "
            "Phase 9's RAG threshold of 0.55). High precision "
            "matters more than recall here -- a wrong activation "
            "on a problem we don't have a real verified path for "
            "would be worse than no activation at all."
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
