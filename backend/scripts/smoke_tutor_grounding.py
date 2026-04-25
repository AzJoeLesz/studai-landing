"""Live DB check for tutor grounding layers (problem RAG, OpenStax, annotations).

Does not open the website. Uses the same `build_grounding_context` path as the
tutor. Run from `backend/` with `backend/.env` (or env vars) set.

Usage:
    python -m scripts.smoke_tutor_grounding
    SMOKE_BACKEND_URL=https://your-app.up.railway.app python -m scripts.smoke_tutor_grounding
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.agents.retrieval import build_grounding_context
from app.core.config import get_settings
from app.db.schemas import Language

DEFAULT_QUERIES: list[tuple[str, Language]] = [
    ("How do I solve 2x + 5 = 17?", "en"),
    ("Factor the quadratic x^2 - 5x + 6 = 0.", "en"),
    (
        "Mi a merőleges kör egyenlete ha a középpont (2,3) és sugár 4?",
        "hu",
    ),
]


def _len_part(s: str | None) -> int:
    return len(s) if s else 0


def _on(s: str | None) -> bool:
    return bool(s and s.strip())


def _line_layer(name: str, n: int) -> str:
    status = "ON " if n > 0 else "OFF"
    return f"  {name}: {status}  ({n} chars in prompt text)"


async def _run_db_checks() -> bool:
    print(
        "What this means:\n"
        "  L1 = problem bank (similar exercises + worked solutions)\n"
        "  L2 = OpenStax chunks (book excerpts)\n"
        "  L3 = precomputed problem_annotations (only if L1 found a problem that has a row)\n"
        "Each line ON/OFF = whether that text was non-empty for this test query.\n"
    )
    settings = get_settings()
    print("Settings (grounding):")
    print(
        f"  rag_enabled={settings.rag_enabled!r}  top_k={settings.rag_top_k}  th={settings.rag_similarity_threshold}"
    )
    print(
        f"  material_rag_enabled={settings.material_rag_enabled!r}  top_k={settings.material_rag_top_k}  th={settings.material_rag_threshold}"
    )
    print(f"  annotation_injection_enabled={settings.annotation_injection_enabled!r}")
    print()

    any_l1 = any_l2 = any_l3 = False
    for q, lang in DEFAULT_QUERIES:
        ctx = await build_grounding_context(q, lang)
        pr = _len_part(ctx.problem_reference)
        ox = _len_part(ctx.openstax_excerpts)
        ta = _len_part(ctx.teaching_annotations)
        if pr:
            any_l1 = True
        if ox:
            any_l2 = True
        if ta:
            any_l3 = True
        short = f"{q[:72]}..." if len(q) > 72 else q
        print(f"--- Query ({lang}): {short}")
        print(_line_layer("L1 problem bank  ", pr))
        print(_line_layer("L2 OpenStax      ", ox))
        print(_line_layer("L3 annotations   ", ta))
        print()

    print("=" * 60)
    print("Summary (across the 3 test queries above):")
    print(f"  L1 was ON at least once: {'YES' if any_l1 else 'NO'}")
    print(f"  L2 was ON at least once: {'YES' if any_l2 else 'NO'}")
    print(f"  L3 was ON at least once: {'YES' if any_l3 else 'NO  (expected if L1 never matched an annotated id)'}")
    print("=" * 60)
    if not any_l2 and not any_l1:
        print("WARNING: No layer had text. Check flags, data, and thresholds.")
        return False
    return True


def _health(url: str) -> bool:
    u = url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(u, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
        print(f"GET {u} -> {r.status}")
        print(f"  {body[:200]}")
        return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        print(f"GET {u} failed: {e}")
        return False


async def _async_main(health_url: str | None) -> int:
    db_ok = await _run_db_checks()
    code = 0 if db_ok else 1
    if health_url:
        print("---")
        h_ok = _health(health_url)
        if not h_ok and code == 0:
            code = 1
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--health-url",
        default=os.environ.get("SMOKE_BACKEND_URL"),
        help="If set, GET {url}/health (or env SMOKE_BACKEND_URL).",
    )
    args = ap.parse_args()
    return asyncio.run(_async_main(args.health_url))


if __name__ == "__main__":
    raise SystemExit(main())
