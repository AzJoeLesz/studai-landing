"""Shared FastAPI dependencies, re-exported for ergonomic imports.

`AdminUser` (Phase 10C) is a thin wrapper around `CurrentUser` that
adds a server-side check on `profiles.role == 'admin'`. The frontend
also redirects non-admins client-side, but this is the authoritative
gate -- a curl call to `/admin/*` from a regular user gets a 403,
not a 200.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.security import AuthenticatedUser, CurrentUser
from app.db import repositories as repo


def get_admin_user(user: CurrentUser) -> AuthenticatedUser:
    """Require `profiles.role == 'admin'` on the calling user.

    This is read-once-per-request: cheap (single indexed lookup, same
    table the chat turn already reads). We deliberately don't cache
    the role decision -- if the founder demotes someone in Supabase,
    the next request reflects it.
    """
    profile = repo.get_profile(user.user_id)
    if profile is None or profile.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


AdminUser = Annotated[AuthenticatedUser, Depends(get_admin_user)]


__all__ = ["AdminUser", "CurrentUser"]
