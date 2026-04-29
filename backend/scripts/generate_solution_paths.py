"""Phase 10A — Generate per-problem solution graphs (paths + steps + hints + mistakes).

Three modes:

  * `--from-annotations`   Pull problems that already have a row in
                           `problem_annotations` but no `solution_paths`
                           yet (the 205 already-annotated set, Decision A
                           in docs/phase10_solution_graphs.md). The
                           existing annotation JSON is fed into the
                           prompt as additional input scaffolding.

  * (no flag)              Pull problems with no `solution_paths` row
                           in the target language. Used by 10E for the
                           curriculum-led expansion. Will iterate the
                           full corpus until you stop it (--limit caps
                           the run).

  * `--problem-id <uuid>`  Generate for ONE specific problem. Useful
                           for /admin/paths "regenerate" actions later.

For every generated path we ALSO run a small LLM-as-judge pre-filter
(Decision N) which scores the generated path on (correctness, hint
quality, mistake plausibility, step granularity) and writes the
composite `critic_score` into `solution_paths.critic_score`. The
human verification queue (10C) is sorted by that score so high-
confidence paths bubble to the top.

Usage (from `backend/`, venv active):

    # Backfill the 205 already-annotated problems (Decision A).
    python -m scripts.generate_solution_paths --from-annotations --limit 5

    # Dry run, no DB writes:
    python -m scripts.generate_solution_paths --from-annotations \
                                              --limit 1 --dry-run

    # Curriculum-led expansion (10E):
    python -m scripts.generate_solution_paths --limit 50

    # One specific problem (e.g. after editing the prompt):
    python -m scripts.generate_solution_paths \
        --problem-id 12345678-1234-1234-1234-123456789abc --overwrite

Cost (rule of thumb at gpt-5-mini for generation + gpt-5 for critic):
    ~$0.025-0.035 per problem (generator ~$0.02 + critic ~$0.01).
    500 problems => ~$15-20.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db import repositories as repo  # noqa: E402
from app.db.schemas import (  # noqa: E402
    CommonMistakeInsert,
    Language,
    Problem,
    SolutionPathInsert,
    SolutionStepInsert,
    StepHintInsert,
)
from app.embeddings.openai_embeddings import OpenAIEmbeddingsClient  # noqa: E402
from app.llm.openai_client import (  # noqa: E402
    _reasoning_kwargs,
    _token_limit_kwargs,
)

logger = logging.getLogger("generate_solution_paths")

PROMPT_PATH = BACKEND_ROOT / "app" / "prompts" / "path_gen_v1.txt"
MATERIAL_TOP_K = 4
MAX_PATHS_PER_PROBLEM = 3
MAX_STEPS_PER_PATH = 8
MAX_HINTS_PER_STEP = 3
MAX_MISTAKES_PER_STEP = 3
GENERATOR_MAX_TOKENS = 3500
CRITIC_MAX_TOKENS = 400


# ---------------------------------------------------------------------------
# Pydantic shapes for the generator's JSON output (validated before insert)
# ---------------------------------------------------------------------------
class GeneratedMistake(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=400)
    detection_hint: str | None = None
    pedagogical_hint: str = Field(..., min_length=1, max_length=800)
    remediation_topic: str | None = None


class GeneratedStep(BaseModel):
    goal: str = Field(..., min_length=1, max_length=500)
    expected_action: str | None = None
    expected_state: str | None = None
    is_terminal: bool = False
    hints: list[str] = Field(default_factory=list)
    common_mistakes: list[GeneratedMistake] = Field(default_factory=list)


class GeneratedPath(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    rationale: str | None = None
    preferred: bool = False
    steps: list[GeneratedStep]


class GeneratedGraph(BaseModel):
    paths: list[GeneratedPath]


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def _load_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise SystemExit(f"Missing prompt: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _format_material(material: list[dict]) -> str:
    if not material:
        return "(no OpenStax chunks retrieved; rely on the problem and solution.)"
    parts: list[str] = []
    for i, row in enumerate(material, start=1):
        parts.append(
            f"---\n[#{i}] {row.get('source')}:{row.get('book_slug')} "
            f"pages {row.get('page_start')}-{row.get('page_end')} "
            f"sim={row.get('similarity', 0):.2f}\n{row.get('body', '')}"
        )
    return "\n".join(parts)


def _format_existing_annotation(payload: dict | None) -> str:
    if not payload:
        return "(no existing teaching annotation for this problem.)"
    return json.dumps(payload, ensure_ascii=False, indent=0)


async def _user_payload(
    *,
    emb: OpenAIEmbeddingsClient,
    problem: Problem,
    existing_annotation: dict | None,
) -> str:
    """Build the user-message payload for the generator call.

    Reuses the same OpenStax retrieval as `annotate_problems.py`. When
    we already have an L3 annotation for this problem (from
    `problem_annotations`), we hand it to the generator as
    scaffolding -- we still want the new paths shape, but the
    generator gets a head start on hint phrasing + mistake patterns.
    """
    q = f"{problem.type}\n\n{problem.problem_en}"
    try:
        vec = await emb.embed_one(q)
        raw = await asyncio.to_thread(
            repo.search_teaching_material, vec, match_count=MATERIAL_TOP_K
        )
        material = [h.model_dump() for h in raw] if raw else []
    except Exception:
        logger.warning(
            "openstax retrieval failed for problem=%s; continuing without",
            problem.id,
            exc_info=True,
        )
        material = []
    return (
        f"PROBLEM TYPE: {problem.type}\n"
        f"DIFFICULTY: {problem.difficulty or 'unknown'}\n\n"
        f"PROBLEM:\n{problem.problem_en}\n\n"
        f"WORKED SOLUTION (verified, internal -- do not paste to student):\n"
        f"{problem.solution_en}\n\n"
        f"OPENTSTAX EXCERPTS (may be empty):\n{_format_material(material)}\n\n"
        f"EXISTING TEACHING ANNOTATION (use as scaffolding; produce the new "
        f"paths shape, do not just copy):\n"
        f"{_format_existing_annotation(existing_annotation)}\n"
    )


def _critic_payload(
    *,
    problem: Problem,
    graph: GeneratedGraph,
) -> str:
    return (
        f"PROBLEM TYPE: {problem.type}\n"
        f"DIFFICULTY: {problem.difficulty or 'unknown'}\n\n"
        f"PROBLEM:\n{problem.problem_en}\n\n"
        f"WORKED SOLUTION (verified, internal):\n{problem.solution_en}\n\n"
        f"GENERATED SOLUTION GRAPH (to be scored):\n"
        f"{graph.model_dump_json(indent=2)}\n"
    )


_CRITIC_SYSTEM_PROMPT = """\
You are a critic for a math tutoring system's solution graphs.

You will see ONE problem, its verified worked solution, and a
generated solution graph (paths + steps + hints + common_mistakes).
Your job: score the generated graph on FOUR axes, return a single
JSON object with the scores plus an overall (1-5).

AXES:
  - correctness          (0-5): does each step actually advance the
                                problem and end at the right answer?
  - hint_quality         (0-5): are the 3 hints per step graduated
                                (gentle -> stronger -> last) and free
                                of the final answer?
  - mistake_plausibility (0-5): are the common_mistakes things real
                                students actually do, with pedagogical
                                hints that nudge (don't correct)?
  - step_granularity     (0-5): is each step ONE meaningful
                                transformation -- not arithmetic
                                atomized, not whole solutions crammed?

Return STRICT JSON, no surrounding prose, no markdown:
  {
    "correctness": 4,
    "hint_quality": 3,
    "mistake_plausibility": 5,
    "step_granularity": 4,
    "overall": 4.0,
    "notes": "<short explanation, <200 chars>"
  }

`overall` is the mean of the four axes (you compute it). Be strict
but fair: a path that solves correctly with one weak hint is a 4,
not a 2. A path with a wrong final answer is a 1, not a 3.
""".strip()


# ---------------------------------------------------------------------------
# JSON parsing (resilient to fences and prose)
# ---------------------------------------------------------------------------
def _extract_json_object(raw: str) -> dict | None:
    """Best-effort JSON parser. Mirrors state_updater._extract_json shape."""
    if not raw:
        return None
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Generator + critic LLM calls
# ---------------------------------------------------------------------------
async def _generate(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    user_msg: str,
) -> GeneratedGraph | None:
    """One generation call. Returns None on parse / validation failure."""
    try:
        res = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            **_token_limit_kwargs(model, GENERATOR_MAX_TOKENS),
            **_reasoning_kwargs(model),
        )
    except Exception:
        logger.warning("generator: API call failed", exc_info=True)
        return None

    text = (res.choices[0].message.content or "").strip()
    parsed = _extract_json_object(text)
    if not parsed:
        logger.warning("generator: empty / unparsable JSON")
        return None
    try:
        graph = GeneratedGraph.model_validate(parsed)
    except ValidationError as exc:
        logger.warning("generator: JSON failed schema validation: %s", exc)
        return None

    return _enforce_caps(graph)


def _enforce_caps(graph: GeneratedGraph) -> GeneratedGraph:
    """Hard-cap arrays so a verbose generator can't blow the schema.

    These match the SQL constraints + the documented limits in the
    prompt. Trimming silently is fine -- the prompt already says these
    are limits; an LLM that exceeds them is producing low-quality
    content anyway.
    """
    paths = graph.paths[:MAX_PATHS_PER_PROBLEM]
    for path in paths:
        path.steps = path.steps[:MAX_STEPS_PER_PATH]
        for step in path.steps:
            step.hints = [h[:600] for h in step.hints[:MAX_HINTS_PER_STEP]]
            step.common_mistakes = step.common_mistakes[:MAX_MISTAKES_PER_STEP]

    # Ensure exactly one preferred=True. If the generator marked zero,
    # promote the first; if it marked multiple, keep only the first.
    seen_preferred = False
    for path in paths:
        if path.preferred and not seen_preferred:
            seen_preferred = True
        elif path.preferred and seen_preferred:
            path.preferred = False
    if not seen_preferred and paths:
        paths[0].preferred = True

    # Ensure exactly one terminal step per path (the last one).
    for path in paths:
        if not path.steps:
            continue
        for step in path.steps:
            step.is_terminal = False
        path.steps[-1].is_terminal = True

    return GeneratedGraph(paths=paths)


async def _critique(
    client: AsyncOpenAI,
    model: str,
    problem: Problem,
    graph: GeneratedGraph,
) -> float | None:
    """Run the LLM-as-judge pre-filter. Returns the `overall` score (0-5).

    Decision N: critic uses a STRONGER model than the generator
    (default gpt-5 vs the generator's gpt-5-mini) so it can actually
    catch generator errors instead of just rubber-stamping them.
    Failures are non-fatal -- the path is still inserted, just
    without a critic_score (sorts to the bottom of /admin/paths).
    """
    try:
        res = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": _critic_payload(problem=problem, graph=graph)},
            ],
            response_format={"type": "json_object"},
            **_token_limit_kwargs(model, CRITIC_MAX_TOKENS),
            **_reasoning_kwargs(model),
        )
    except Exception:
        logger.debug("critic: API call failed (non-fatal)", exc_info=True)
        return None

    parsed = _extract_json_object((res.choices[0].message.content or "").strip())
    if not parsed:
        return None
    try:
        overall = float(parsed.get("overall"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(5.0, overall))


# ---------------------------------------------------------------------------
# Persistence: write a generated graph into the relational schema
# ---------------------------------------------------------------------------
def _persist_graph(
    *,
    problem_id: UUID,
    graph: GeneratedGraph,
    language: Language,
    model: str,
    critic_score: float | None,
    source: str = "generator",
    overwrite: bool = False,
) -> int:
    """Write the generated graph into solution_paths/_steps/_hints/_mistakes.

    Returns the number of paths persisted. When `overwrite=True` we
    delete any existing paths for this problem in this language first
    (cascade kills steps + hints + step-scoped mistakes too). Without
    `overwrite`, conflicts on (problem_id, name, language) overwrite
    just the path metadata via upsert -- safe for re-runs but children
    pile up. `overwrite` is the right move when re-generating after
    prompt iteration.
    """
    if overwrite:
        existing = repo.get_paths_for_problem(problem_id, language=language)
        for p in existing:
            repo.delete_path(p.id)

    written = 0
    for gen_path in graph.paths:
        path_row = repo.insert_solution_path(
            SolutionPathInsert(
                problem_id=problem_id,
                name=gen_path.name,
                rationale=gen_path.rationale,
                preferred=gen_path.preferred,
                language=language,
                model=model,
                critic_score=critic_score,
                source=source,
            )
        )
        # Steps in order (1-based step_index per path).
        step_inserts = [
            SolutionStepInsert(
                path_id=path_row.id,
                step_index=i,
                goal=gen_step.goal,
                expected_action=gen_step.expected_action,
                expected_state=gen_step.expected_state,
                is_terminal=gen_step.is_terminal,
            )
            for i, gen_step in enumerate(gen_path.steps, start=1)
        ]
        steps = repo.bulk_insert_steps(step_inserts)
        # Map step_index -> id for hint + mistake inserts.
        step_id_by_index = {s.step_index: s.id for s in steps}

        hint_inserts: list[StepHintInsert] = []
        mistake_inserts: list[CommonMistakeInsert] = []
        for i, gen_step in enumerate(gen_path.steps, start=1):
            step_id = step_id_by_index.get(i)
            if step_id is None:
                continue
            for hi, hint_body in enumerate(gen_step.hints, start=1):
                if hi > MAX_HINTS_PER_STEP:
                    break
                hint_inserts.append(
                    StepHintInsert(
                        step_id=step_id, hint_index=hi, body=hint_body
                    )
                )
            for gm in gen_step.common_mistakes:
                mistake_inserts.append(
                    CommonMistakeInsert(
                        step_id=step_id,
                        pattern=gm.pattern,
                        detection_hint=gm.detection_hint,
                        pedagogical_hint=gm.pedagogical_hint,
                        remediation_topic=gm.remediation_topic,
                    )
                )
        repo.bulk_insert_hints(hint_inserts)
        repo.bulk_insert_mistakes(mistake_inserts)
        written += 1

    return written


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
async def _process_one(
    *,
    client: AsyncOpenAI,
    emb: OpenAIEmbeddingsClient,
    system_prompt: str,
    problem: Problem,
    settings: Any,
    args: argparse.Namespace,
) -> bool:
    """Generate + critique + persist one problem. Returns True on success."""
    existing_annotation: dict | None = None
    if args.from_annotations:
        # The annotation is the input scaffolding for the generator.
        annos = await asyncio.to_thread(
            repo.get_annotations_for_problem_ids, [problem.id]
        )
        ann_row = annos.get(problem.id)
        if ann_row:
            existing_annotation = ann_row.get("payload")

    user_msg = await _user_payload(
        emb=emb,
        problem=problem,
        existing_annotation=existing_annotation,
    )
    graph = await _generate(
        client, settings.path_gen_model, system_prompt, user_msg
    )
    if not graph or not graph.paths:
        print(f"  FAIL gen problem_id={problem.id}", flush=True)
        return False

    critic_score: float | None = None
    if not args.no_critic:
        critic_score = await _critique(
            client, settings.path_critic_model, problem, graph
        )

    if args.dry_run:
        print(
            f"  DRY ok problem_id={problem.id} paths={len(graph.paths)} "
            f"critic={critic_score if critic_score is not None else 'n/a'}",
            flush=True,
        )
        # Show a tiny preview so dry-runs are useful for prompt iteration.
        preview = graph.model_dump_json(indent=0)[:1500]
        print(preview, flush=True)
        return True

    written = await asyncio.to_thread(
        _persist_graph,
        problem_id=problem.id,
        graph=graph,
        language=args.language,
        model=settings.path_gen_model,
        critic_score=critic_score,
        source="annotation_backfill" if args.from_annotations else "generator",
        overwrite=args.overwrite,
    )
    print(
        f"  ok problem_id={problem.id} paths={written} "
        f"critic={critic_score if critic_score is not None else 'n/a'}",
        flush=True,
    )
    return True


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    emb = OpenAIEmbeddingsClient()
    oa = AsyncOpenAI(api_key=settings.openai_api_key)
    system_prompt = _load_prompt()

    sem = asyncio.Semaphore(args.concurrency)
    cap = args.limit
    if cap is not None and cap < 0:
        cap = None

    # Resolve which problems to work on.
    targets: list[Problem] = []
    if args.problem_id:
        problem = await asyncio.to_thread(
            repo.get_problem, UUID(args.problem_id)
        )
        if not problem:
            print(f"problem not found: {args.problem_id}", flush=True)
            return 1
        targets = [problem]
    else:
        done = 0
        while cap is None or done < cap:
            need = 100 if cap is None else min(100, cap - done)
            if need < 1:
                break
            if args.from_annotations:
                batch = await asyncio.to_thread(
                    repo.list_annotated_problems_without_solution_paths,
                    args.language,
                    need,
                )
            else:
                batch = await asyncio.to_thread(
                    repo.list_problems_without_solution_paths,
                    args.language,
                    need,
                )
            if not batch:
                break

            async def one(p: Problem) -> bool:
                async with sem:
                    return await _process_one(
                        client=oa,
                        emb=emb,
                        system_prompt=system_prompt,
                        problem=p,
                        settings=settings,
                        args=args,
                    )

            results = await asyncio.gather(*[one(p) for p in batch])
            done += sum(1 for ok in results if ok)
            if cap is not None and done >= cap:
                break
            if len(batch) < need:
                break
        return 0

    # Single-problem path.
    async def one(p: Problem) -> bool:
        async with sem:
            return await _process_one(
                client=oa,
                emb=emb,
                system_prompt=system_prompt,
                problem=p,
                settings=settings,
                args=args,
            )

    await asyncio.gather(*[one(p) for p in targets])
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(
        description=(
            "Generate solution graphs (paths + steps + hints + mistakes) "
            "for the math problem corpus. See "
            "docs/phase10_solution_graphs.md for design rationale."
        )
    )
    ap.add_argument(
        "--from-annotations",
        action="store_true",
        help=(
            "Pull only problems that already have an L3 annotation row "
            "(the 205-set, Decision A). Existing annotations become "
            "input scaffolding for the generator."
        ),
    )
    ap.add_argument(
        "--problem-id",
        default=None,
        help="Generate for a single problem (uuid). Bypasses --limit.",
    )
    ap.add_argument(
        "--language",
        default="en",
        choices=("en", "hu"),
        help="Target language for the generated paths (default: en).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max problems to process; omit = run until the queue is empty.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help=(
            "Concurrent generations (default: 2). Mind the OpenAI 60s "
            "per-request timeout in embeddings/openai_embeddings.py and "
            "120s in llm/openai_client.py."
        ),
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Delete existing paths for the target problems before "
            "re-generating. Cascades to steps + hints + step-scoped "
            "mistakes."
        ),
    )
    ap.add_argument(
        "--no-critic",
        action="store_true",
        help=(
            "Skip the LLM-as-judge pre-filter. Saves ~30%% cost; the "
            "/admin/paths queue won't have critic_score sorting."
        ),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate + critique but don't write anything to the DB.",
    )
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
