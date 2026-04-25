"""Generate per-problem teaching annotations (JSON) with OpenAI + OpenStax RAG.

Prerequisites: `sql/005_...` applied; OpenStax material ingested; problems in DB.

Usage (from `backend/`, venv active):

    # 5 problems, one batch (dev)
    python -m scripts.annotate_problems --limit 5

    # Dry run (no DB writes, one problem)
    python -m scripts.annotate_problems --limit 1 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings
from app.db import repositories as repo
from app.embeddings.openai_embeddings import OpenAIEmbeddingsClient

PROMPT_PATH = BACKEND_ROOT / "app" / "prompts" / "annotation_v1.txt"
MATERIAL_TOP_K = 4


def _load_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise SystemExit(f"Missing prompt: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _format_material(material: list[dict]) -> str:
    if not material:
        return "(no OpenStax chunks retrieved; rely on the problem and solution only.)"
    parts: list[str] = []
    for i, row in enumerate(material, start=1):
        parts.append(
            f"---\n[#{i}] {row.get('source')}:{row.get('book_slug')} "
            f"pages {row.get('page_start')}-{row.get('page_end')} "
            f"sim={row.get('similarity', 0):.2f}\n{row.get('body', '')}"
        )
    return "\n".join(parts)


async def _annotator_messages(
    emb: OpenAIEmbeddingsClient,
    problem: str,
    solution: str,
    prob_type: str,
) -> list[dict[str, str]]:
    q = f"{prob_type}\n\n{problem}"
    vec = await emb.embed_one(q)
    try:
        raw = await asyncio.to_thread(
            lambda: repo.search_teaching_material(
                vec, match_count=MATERIAL_TOP_K
            )
        )
        material = [h.model_dump() for h in raw] if raw else []
    except Exception:
        material = []
    ex = _format_material(material)
    system = _load_prompt()
    user = (
        f"PROBLEM TYPE: {prob_type}\n\n"
        f"PROBLEM:\n{problem}\n\n"
        f"WORKED SOLUTION (internal, do not paste to student):\n{solution}\n\n"
        f"OPENTSTAX EXCERPTS (may be empty):\n{ex}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def _annotate_one(
    client: AsyncOpenAI,
    model: str,
    emb: OpenAIEmbeddingsClient,
    problem: str,
    solution: str,
    prob_type: str,
) -> dict[str, Any] | None:
    messages = await _annotator_messages(emb, problem, solution, prob_type)
    res = await client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        max_tokens=2000,
        response_format={"type": "json_object"},
    )
    text = (res.choices[0].message.content or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    model = args.model or settings.openai_model
    emb = OpenAIEmbeddingsClient()
    oa = AsyncOpenAI(api_key=settings.openai_api_key)

    sem = asyncio.Semaphore(args.concurrency)
    count_lock = asyncio.Lock()
    done = 0
    cap = args.limit
    if cap is not None and cap < 0:
        cap = None

    while cap is None or done < cap:
        need = 500 if cap is None else min(500, cap - done)
        if need < 1:
            break
        batch = repo.list_problems_without_annotations(need)
        if not batch:
            break

        async def one(p) -> None:  # type: ignore[no-untyped-def]
            nonlocal done
            async with sem:
                data = await _annotate_one(
                    oa,
                    model,
                    emb,
                    p.problem_en,
                    p.solution_en,
                    p.type,
                )
            if not data:
                print(f"  FAIL parse {p.id}", flush=True)
                return
            if args.dry_run:
                print(json.dumps(data, ensure_ascii=False, indent=0)[:1200], flush=True)
            else:
                await asyncio.to_thread(
                    repo.upsert_problem_annotation, p.id, data, model
                )
            async with count_lock:
                done += 1
                d = done
            print(f"  ok {d} id={p.id}", flush=True)

        await asyncio.gather(*[one(p) for p in batch])
        if cap is not None and done >= cap:
            break
        if len(batch) < need:
            break

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max problems; omit = one full batch")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument(
        "--model",
        default=None,
        help="Override OPENAI model (default: settings openai_model).",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
