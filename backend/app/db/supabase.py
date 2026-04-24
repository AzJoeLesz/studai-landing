"""Supabase client bound to the service_role key.

This client bypasses Row Level Security. That's both the point and the
risk: every function in `repositories.py` must manually enforce
`user_id = caller.user_id` on reads and writes.

We DO NOT ever expose this key — it stays in backend env vars only.
"""

from functools import lru_cache

from supabase import Client, create_client

from app.core.config import get_settings


@lru_cache
def get_supabase_client() -> Client:
    settings = get_settings()
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
