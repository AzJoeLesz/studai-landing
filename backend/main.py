"""StudAI backend — FastAPI entry point.

Run locally:
    uvicorn main:app --reload --port 8000

Run on Railway:
    uvicorn main:app --host 0.0.0.0 --port $PORT
(this is what railway.json configures for you)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, chat, health, onboarding, problems, sessions
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="StudAI backend",
        description="Tutor brain. Streams LLM responses and persists sessions.",
        version="0.1.0",
    )

    @app.get("/", tags=["health"])
    def root() -> dict[str, str]:
        """Some platforms probe / during deploy; /health is the main check."""
        return {"status": "ok"}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(problems.router)
    app.include_router(onboarding.router)
    app.include_router(admin.router)

    return app


app = create_app()
