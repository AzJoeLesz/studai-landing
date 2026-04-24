"""StudAI backend — FastAPI entry point.

Run locally:
    uvicorn main:app --reload --port 8000

Run on Railway:
    uvicorn main:app --host 0.0.0.0 --port $PORT
(this is what railway.json configures for you)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, health, sessions
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="StudAI backend",
        description="Tutor brain. Streams LLM responses and persists sessions.",
        version="0.1.0",
    )

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

    return app


app = create_app()
