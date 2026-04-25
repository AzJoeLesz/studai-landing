"""StudAI eval lab — a tiny LLM-as-judge framework for tutor prompts.

Why this exists:
  Every prompt change is a bet. Without measurement you can't tell if v3
  is actually better than v2 or just feels like it on the cases you tried.
  This module lets us define a corpus of test cases + pedagogical rubrics
  and score any prompt against them in seconds.

How it works:
  1. Load a YAML file of test cases (each is a fake conversation + tags +
     which rubrics apply).
  2. For each case, call the LLM with the system prompt under test +
     the case's conversation. Capture the assistant reply.
  3. For each rubric on the case, ask a *judge LLM* to score the reply
     against a structured rubric prompt. Get back {score, reason}.
  4. Aggregate: per-rubric averages, per-case scores, low scorers.

Conventions:
  - Scores are 0.0–1.0. `null` means N/A (rubric doesn't apply to this case).
  - Higher is always better.
  - Rubrics may have weights (default 1.0) used for the weighted aggregate.
  - Adding a rubric is a one-line `Rubric(...)` literal at the bottom of
    `RUBRICS`. Adding a test case is a YAML entry. No framework code edits.

Cost: with 30 cases × ~5 rubrics each × gpt-4o-mini judge ~= €0.03 per run.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template
from openai import AsyncOpenAI


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class Case:
    id: str
    description: str
    conversation: list[Turn]
    rubrics: list[str]
    tags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Score:
    score: float | None  # None = N/A
    reason: str


@dataclass
class Rubric:
    """A single evaluator. Either LLM-as-judge or a pure pattern check."""

    name: str
    weight: float
    description: str
    # For LLM-as-judge rubrics: a Jinja template producing the judge prompt.
    # The template has access to: case, response.
    judge_template: str | None = None
    # For pattern rubrics: a function (response, case) -> Score
    pattern_fn: Any = None  # Callable[[str, Case], Score] | None


@dataclass
class CaseResult:
    case: Case
    response: str
    scores: dict[str, Score]  # rubric name -> Score
    elapsed_ms: int


@dataclass
class RunResult:
    prompt_label: str
    model: str
    cases: list[CaseResult]
    total_elapsed_ms: int


# ---------------------------------------------------------------------------
# Rubrics — add new ones here.
#
# LLM-as-judge prompts must end by asking for STRICT JSON output of shape:
#   {"score": <float 0..1 or null>, "reason": "<one short sentence>"}
# ---------------------------------------------------------------------------
SOCRATIC_TEMPLATE = """You are evaluating a math tutor's response for Socratic teaching style.

The student wrote:
\"\"\"{{ case.last_user_message }}\"\"\"

The tutor replied:
\"\"\"{{ response }}\"\"\"

A Socratic tutor GUIDES the student to discover the answer through questions or hints. They do NOT reveal the final answer or perform the calculation for the student.

Score:
  1.0  Tutor asked a guiding question OR gave a hint, did NOT reveal the answer.
  0.7  Tutor mostly guided but slipped a partial calculation or strong hint.
  0.3  Tutor gave most of the solution, leaving only a trivial step.
  0.0  Tutor gave the full answer / did the calculation.

Special case:
  - If the student's message is NOT a math problem (e.g. greeting, off-topic, conceptual question with no specific problem to solve), output {"score": null, "reason": "not a problem-solving turn"}.
  - If the student EXPLICITLY insists on a direct answer after the tutor already tried Socratic, do not penalize giving the answer.

Output ONLY a JSON object: {"score": <float 0-1 or null>, "reason": "<one short sentence>"}"""


GUIDES_WITH_QUESTION_TEMPLATE = """You are evaluating a math tutor's response.

The student wrote:
\"\"\"{{ case.last_user_message }}\"\"\"

The tutor replied:
\"\"\"{{ response }}\"\"\"

Did the tutor ask the student a question (in any form) that invites the student to think or respond?

Score:
  1.0  Reply contains a clear question that prompts the student.
  0.5  Reply gently suggests next thinking ("try X" / "what if X?") without explicit question mark.
  0.0  Reply is purely declarative or just gives the answer.

If the student's message was a greeting or thank-you and a question would feel forced, output {"score": null, "reason": "no question expected"}.

Output ONLY: {"score": <float 0-1 or null>, "reason": "<one short sentence>"}"""


LANGUAGE_MATCH_TEMPLATE = """You are evaluating whether a tutor reply is in the SAME language as the student's message.

The student wrote (language: {{ case.context.language|default('unknown') }}):
\"\"\"{{ case.last_user_message }}\"\"\"

The tutor replied:
\"\"\"{{ response }}\"\"\"

Score:
  1.0  Reply is entirely in the same language as the student.
  0.5  Reply is mostly the same language but mixes in another (e.g. random English in a Hungarian reply).
  0.0  Reply is in a different language entirely.

LaTeX, math symbols, and short proper nouns don't count as language switches.

Output ONLY: {"score": <float 0-1>, "reason": "<one short sentence>"}"""


AGE_APPROPRIATE_TEMPLATE = """You are evaluating whether a tutor's response uses age-appropriate language.

The student is {{ case.context.student_age|default('a teenager') }} years old, in grade {{ case.context.grade|default('unknown') }}.

The tutor replied:
\"\"\"{{ response }}\"\"\"

Score:
  1.0  Vocabulary, sentence length, and concept density are appropriate for the age.
  0.5  Mostly fine but with a few too-advanced words or one over-complex sentence.
  0.0  Reads like a research paper or like talking to a small child — clearly mismatched.

Output ONLY: {"score": <float 0-1>, "reason": "<one short sentence>"}"""


KIND_TONE_TEMPLATE = """You are evaluating the tone of a tutor's reply.

The student wrote:
\"\"\"{{ case.last_user_message }}\"\"\"

The tutor replied:
\"\"\"{{ response }}\"\"\"

A good tutor is warm and supportive — never patronizing ("good job, little one!"), never cold or dismissive, never lecturing.

Score:
  1.0  Warm + supportive + treats the student as capable.
  0.5  Neutral; no warmth but no negative tone.
  0.0  Patronizing OR dismissive OR cold OR lecturing.

Output ONLY: {"score": <float 0-1>, "reason": "<one short sentence>"}"""


ON_TOPIC_TEMPLATE = """You are evaluating whether a tutor stayed on topic.

The student wrote:
\"\"\"{{ case.last_user_message }}\"\"\"

The tutor replied:
\"\"\"{{ response }}\"\"\"

A math tutor should stay focused on math (or on bringing the conversation back to math, gently). Off-topic chatter is bad.

Score:
  1.0  Reply is about math, OR gently redirects an off-topic question back to learning.
  0.5  Reply mixes math + significant off-topic content.
  0.0  Reply is entirely off-topic.

Output ONLY: {"score": <float 0-1>, "reason": "<one short sentence>"}"""


def uses_latex_pattern(response: str, case: Case) -> Score:
    """Pattern rubric: if the response discusses math, it should use LaTeX."""
    has_latex = bool(re.search(r"\$[^$\n]+\$|\$\$[\s\S]+?\$\$", response))
    has_unicode_math = bool(
        re.search(r"[²³⁴⁵√∫∑∏≈≠≤≥πθ∞]", response)
    )
    last_user = case.last_user_message.lower()
    looks_like_math_question = any(
        kw in last_user
        for kw in [
            "egyenlet", "függvény", "derivált", "integrál", "deriv",
            "equation", "function", "derivative", "integral", "solve",
            "x", "x²", "x^2", "számold", "compute", "calculate",
        ]
    )

    if not looks_like_math_question:
        return Score(score=None, reason="no math expected in reply")
    if has_unicode_math and not has_latex:
        return Score(score=0.0, reason="used Unicode math symbols instead of LaTeX")
    if has_latex:
        return Score(score=1.0, reason="uses LaTeX delimiters")
    if not has_latex:
        return Score(score=0.5, reason="math context but no LaTeX (might be conceptual reply)")
    return Score(score=1.0, reason="ok")


RUBRICS: dict[str, Rubric] = {
    r.name: r
    for r in [
        Rubric(
            name="socratic",
            weight=3.0,
            description="Did NOT reveal the answer; guided instead.",
            judge_template=SOCRATIC_TEMPLATE,
        ),
        Rubric(
            name="guides_with_question",
            weight=2.0,
            description="Asked a question that invites student thinking.",
            judge_template=GUIDES_WITH_QUESTION_TEMPLATE,
        ),
        Rubric(
            name="language_match",
            weight=3.0,
            description="Replied in the same language as the student.",
            judge_template=LANGUAGE_MATCH_TEMPLATE,
        ),
        Rubric(
            name="age_appropriate",
            weight=1.5,
            description="Vocabulary and complexity match the student's age.",
            judge_template=AGE_APPROPRIATE_TEMPLATE,
        ),
        Rubric(
            name="kind_tone",
            weight=1.0,
            description="Warm and supportive without being patronizing.",
            judge_template=KIND_TONE_TEMPLATE,
        ),
        Rubric(
            name="on_topic",
            weight=2.0,
            description="Stays on math (or redirects back gently).",
            judge_template=ON_TOPIC_TEMPLATE,
        ),
        Rubric(
            name="uses_latex",
            weight=1.0,
            description="Uses LaTeX delimiters when math is in the reply.",
            pattern_fn=uses_latex_pattern,
        ),
    ]
}


# Helper used by Jinja templates.
def _attach_helpers(case: Case) -> Case:
    """Add the `last_user_message` convenience attribute used in templates."""
    last_user = next(
        (t.content for t in reversed(case.conversation) if t.role == "user"),
        "",
    )
    # mutate; Cases are short-lived per run
    case.last_user_message = last_user  # type: ignore[attr-defined]
    return case


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cases: list[Case] = []
    for entry in raw:
        cases.append(
            Case(
                id=entry["id"],
                description=entry.get("description", ""),
                conversation=[
                    Turn(role=t["role"], content=t["content"])
                    for t in entry["conversation"]
                ],
                rubrics=entry.get("rubrics", []),
                tags=entry.get("tags", []),
                context=entry.get("context", {}),
            )
        )
    return cases


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
async def _run_one_case(
    case: Case,
    *,
    system_prompt: str,
    model: str,
    judge_model: str,
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
) -> CaseResult:
    case = _attach_helpers(case)
    started = time.monotonic()

    # 1. Get the tutor's response.
    messages = [
        {"role": "system", "content": system_prompt},
        *[{"role": t.role, "content": t.content} for t in case.conversation],
    ]
    async with sem:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
        )
    response = completion.choices[0].message.content or ""

    # 2. Score it against each requested rubric (in parallel).
    async def score_one(rubric_name: str) -> tuple[str, Score]:
        rubric = RUBRICS.get(rubric_name)
        if rubric is None:
            return rubric_name, Score(score=None, reason=f"unknown rubric: {rubric_name}")

        if rubric.pattern_fn is not None:
            return rubric_name, rubric.pattern_fn(response, case)

        assert rubric.judge_template is not None
        judge_prompt = Template(rubric.judge_template).render(
            case=case, response=response
        )
        async with sem:
            judge_completion = await client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": judge_prompt}],
                stream=False,
                response_format={"type": "json_object"},
            )
        raw = judge_completion.choices[0].message.content or "{}"
        try:
            parsed = json.loads(raw)
            return rubric_name, Score(
                score=parsed.get("score"),
                reason=str(parsed.get("reason", ""))[:300],
            )
        except json.JSONDecodeError:
            return rubric_name, Score(score=None, reason=f"judge returned non-JSON: {raw[:80]}")

    score_results = await asyncio.gather(
        *(score_one(name) for name in case.rubrics)
    )
    scores = dict(score_results)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return CaseResult(case=case, response=response, scores=scores, elapsed_ms=elapsed_ms)


async def run_eval(
    cases: list[Case],
    *,
    system_prompt: str,
    prompt_label: str,
    model: str = "gpt-4o-mini",
    judge_model: str = "gpt-4o-mini",
    concurrency: int = 6,
    api_key: str | None = None,
) -> RunResult:
    """Run all cases against `system_prompt` and score them. Returns aggregated results."""
    client = AsyncOpenAI(api_key=api_key)
    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()

    case_results = await asyncio.gather(
        *(
            _run_one_case(
                c,
                system_prompt=system_prompt,
                model=model,
                judge_model=judge_model,
                client=client,
                sem=sem,
            )
            for c in cases
        )
    )

    return RunResult(
        prompt_label=prompt_label,
        model=model,
        cases=case_results,
        total_elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# Reporter — terminal-friendly summary + optional HTML
# ---------------------------------------------------------------------------
def aggregate(result: RunResult) -> dict[str, Any]:
    rubric_scores: dict[str, list[float]] = {}
    rubric_pass: dict[str, list[bool]] = {}
    for cr in result.cases:
        for name, score in cr.scores.items():
            if score.score is None:
                continue
            rubric_scores.setdefault(name, []).append(score.score)
            rubric_pass.setdefault(name, []).append(score.score >= 0.5)

    per_rubric = {
        name: {
            "avg": sum(scores) / len(scores) if scores else 0,
            "pass_rate": sum(rubric_pass[name]) / len(rubric_pass[name])
            if rubric_pass.get(name)
            else 0,
            "n": len(scores),
            "weight": RUBRICS[name].weight if name in RUBRICS else 1.0,
        }
        for name, scores in rubric_scores.items()
    }

    # weighted total = sum(avg * weight) / sum(weight)
    total_weight = sum(per_rubric[n]["weight"] for n in per_rubric) or 1
    weighted_total = (
        sum(per_rubric[n]["avg"] * per_rubric[n]["weight"] for n in per_rubric)
        / total_weight
    )

    return {
        "per_rubric": per_rubric,
        "weighted_total": weighted_total,
        "n_cases": len(result.cases),
    }


def print_terminal_report(result: RunResult) -> None:
    agg = aggregate(result)
    print()
    print(
        f"Eval results: {result.prompt_label} | {agg['n_cases']} cases | model={result.model} | {result.total_elapsed_ms / 1000:.1f}s"
    )
    print("─" * 72)
    print(f"{'Rubric':<26}{'Avg':>8}  {'Pass%':>6}   {'N':>4}   weight")
    print("─" * 72)
    for name, stats in sorted(
        agg["per_rubric"].items(),
        key=lambda kv: -kv[1]["weight"],
    ):
        print(
            f"{name:<26}"
            f"{stats['avg']:>8.2f}  "
            f"{stats['pass_rate'] * 100:>5.0f}%   "
            f"{stats['n']:>4}   {stats['weight']:>4.1f}"
        )
    print("─" * 72)
    print(f"Weighted total: {agg['weighted_total']:.2f}")
    print()

    failures = [
        cr
        for cr in result.cases
        if any(s.score is not None and s.score < 0.5 for s in cr.scores.values())
    ]
    if failures:
        print(f"Cases with at least one failing rubric ({len(failures)}):")
        for cr in failures[:10]:
            failed = [
                f"{n} ({s.score:.1f}: {s.reason})"
                for n, s in cr.scores.items()
                if s.score is not None and s.score < 0.5
            ]
            print(f"  • {cr.case.id}")
            for f in failed:
                print(f"      ↳ {f}")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more (see HTML report)")
        print()


def write_html_report(result: RunResult, out_path: Path) -> None:
    agg = aggregate(result)
    rows = []
    for cr in result.cases:
        score_cells = []
        for name in cr.case.rubrics:
            s = cr.scores.get(name)
            if s is None or s.score is None:
                score_cells.append(
                    f"<td style='color:#888'>n/a</td>"
                )
            else:
                color = "#16a34a" if s.score >= 0.7 else "#eab308" if s.score >= 0.4 else "#dc2626"
                score_cells.append(
                    f"<td style='color:{color}' title='{_html_escape(s.reason)}'>{s.score:.2f}</td>"
                )
        rows.append(
            f"<tr><td><code>{cr.case.id}</code><br><small>{_html_escape(cr.case.description)}</small></td>"
            f"<td><pre style='white-space:pre-wrap;max-width:560px'>{_html_escape(cr.response)}</pre></td>"
            + "".join(score_cells)
            + "</tr>"
        )

    rubric_headers = (
        "<th>" + "</th><th>".join(
            sorted(
                {n for cr in result.cases for n in cr.case.rubrics},
                key=lambda n: -RUBRICS[n].weight if n in RUBRICS else 0,
            )
        ) + "</th>"
    )
    summary = "".join(
        f"<li><strong>{n}</strong>: avg {s['avg']:.2f}, pass {s['pass_rate'] * 100:.0f}%</li>"
        for n, s in agg["per_rubric"].items()
    )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>StudAI eval — {result.prompt_label}</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui; padding: 24px; max-width: 1400px; margin: auto; color: #18181b }}
  h1 {{ font-weight: 500; margin-bottom: 4px }}
  .meta {{ color: #71717a; margin-bottom: 16px }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px }}
  th, td {{ border-bottom: 1px solid #e4e4e7; padding: 8px; text-align: left; vertical-align: top }}
  th {{ background: #f4f4f5; font-weight: 500; font-size: 12px; text-transform: uppercase; color: #52525b }}
  pre {{ font-family: ui-monospace, monospace; font-size: 12px; margin: 0 }}
  code {{ font-family: ui-monospace, monospace; font-size: 12px }}
  ul {{ line-height: 1.7 }}
</style></head>
<body>
  <h1>StudAI eval — {result.prompt_label}</h1>
  <div class="meta">{agg['n_cases']} cases · model={result.model} · {result.total_elapsed_ms / 1000:.1f}s · weighted total <strong>{agg['weighted_total']:.2f}</strong></div>
  <ul>{summary}</ul>
  <table>
    <thead><tr><th>Case</th><th>Response</th>{rubric_headers}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
