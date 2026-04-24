"""Authentication layer.

We trust exactly one kind of caller: a user holding a valid Supabase
access token (sent as `Authorization: Bearer <token>`).

Modern Supabase projects sign tokens with **ES256 (asymmetric)**. The
private key stays inside Supabase; we only need the public key to verify.
Supabase publishes its public keys at:

    {SUPABASE_URL}/auth/v1/.well-known/jwks.json

We use PyJWT's `PyJWKClient` to fetch + cache them (default 1h lifespan).
When Supabase rotates keys, this keeps working automatically as long as
the `kid` in the JWT header still resolves to a key in the JWKS response —
which Supabase guarantees during the overlap period after a rotation.

If your project is still on legacy HS256 shared-secret JWTs, this will
not work and needs a different verification path.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWKClient

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    email: str | None = None


@lru_cache
def _get_jwks_client() -> PyJWKClient:
    settings = get_settings()
    jwks_url = (
        f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    )
    # PyJWKClient handles its own HTTP fetch + TTL cache.
    return PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=3600)


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if not header or not header.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    return header.split(" ", 1)[1].strip()


def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    token = _extract_bearer_token(request)

    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            # Supabase's asymmetric keys can be ES256 (ECC P-256) or RS256
            # depending on the key type chosen in the dashboard. Accepting
            # both keeps us resilient if the project rotates to a different
            # algorithm later.
            algorithms=["ES256", "RS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}"
        )

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Token missing sub claim"
        )
    try:
        user_id = UUID(sub)
    except ValueError:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid sub format"
        )

    return AuthenticatedUser(user_id=user_id, email=payload.get("email"))


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
