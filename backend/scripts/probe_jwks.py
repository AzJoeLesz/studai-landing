"""One-off script to confirm we can reach the Supabase JWKS endpoint.

Usage (from backend/ with venv active, env vars set):
    python scripts/probe_jwks.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.security import _get_jwks_client  # noqa: E402


def main() -> None:
    client = _get_jwks_client()
    print(f"JWKS URL: {client.uri}")
    print("Fetching...")
    jwk_set = client.get_jwk_set()
    print(f"Got {len(jwk_set.keys)} signing key(s) from Supabase:")
    for key in jwk_set.keys:
        meta = getattr(key, "_jwk_data", {}) or {}
        print(
            f"  kid={key.key_id}  "
            f"kty={meta.get('kty')}  "
            f"alg={meta.get('alg')}  "
            f"crv={meta.get('crv')}"
        )


if __name__ == "__main__":
    main()
