"""Application settings, loaded once at startup from the environment.

Every other module should import `get_settings()` rather than reading
environment variables directly. This makes it trivial to swap configuration
in tests or when we add new deploy targets.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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

    # --- Tutor behavior -----------------------------------------------------
    tutor_max_history_messages: int = Field(
        default=20,
        description="How many recent messages to include as LLM context.",
    )
    tutor_max_response_tokens: int = Field(
        default=1000,
        description="Upper bound on tokens in a single assistant reply.",
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
