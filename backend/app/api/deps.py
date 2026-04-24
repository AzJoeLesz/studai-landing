"""Shared FastAPI dependencies, re-exported for ergonomic imports."""

from app.core.security import CurrentUser

__all__ = ["CurrentUser"]
