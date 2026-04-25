# StudAI eval lab

Tiny LLM-as-judge framework for measuring tutor prompt quality.
Lives next to the prompts it evaluates.

## Why

Every prompt change is a bet. Without measurement, we can't tell if v3 is
actually better than v2 — only how it *feels* on the cases we tried.
This lab runs a curated set of fake conversations through any system
prompt, scores the replies on a handful of pedagogical rubrics, and
reports averages + per-case detail.

We can iterate on prompts confidently: edit the prompt, re-run the lab,
read the deltas.

## Quick start

```powershell
# from backend/ with venv active
$env:OPENAI_API_KEY = "sk-..."
python -m evals.run
```

You'll see a per-rubric summary in the terminal plus a path to a generated
HTML report with full case-by-case detail.

## Layout

```
backend/evals/
├── lab.py          framework: types, rubrics, runner, reporter
├── cases.yaml      the test corpus — add cases here
├── run.py          CLI entry (`python -m evals.run`)
├── reports/        generated HTML reports (gitignored)
└── README.md       you are here
```

## Cost

Roughly **€0.03 per full run** of 30 cases × ~6 rubrics each, with
`gpt-4o-mini` for both the tutor and the judge.

## Adding a test case

Append an entry to `cases.yaml`:

```yaml
- id: "hu_my_new_case"
  description: "What this case tests, in one line"
  tags: [single_turn, hu]
  context: { student_age: 14, grade: 9, language: hu }
  conversation:
    - role: user
      content: "Az itteni szöveg a diák üzenete."
  rubrics: [socratic, language_match, kind_tone, on_topic]
```

Rules:
- `id` must be unique
- The last turn in `conversation` must be `role: user`
- `rubrics` must reference names defined in `lab.RUBRICS`

For multi-turn cases, include prior `assistant` turns in the conversation
so the model has the same context the real backend would give it.

## Adding a rubric

Open `lab.py`, scroll to the `RUBRICS` block at the bottom, and add an
entry. Two flavors:

**LLM-as-judge** (most common — scoring requires reasoning):
```python
Rubric(
    name="my_new_rubric",
    weight=1.5,
    description="Short summary",
    judge_template=MY_NEW_TEMPLATE,
),
```
The template is a Jinja string that ends asking for JSON
`{"score": <float 0-1 or null>, "reason": "..."}`.

**Pattern** (deterministic — regex / keyword check):
```python
Rubric(
    name="my_new_rubric",
    weight=1.0,
    description="Short summary",
    pattern_fn=my_check_function,
),
```
Where `my_check_function(response: str, case: Case) -> Score`.

Then add `my_new_rubric` to the `rubrics:` list of any case it should
apply to.

## Comparing two prompts

Today: run twice, compare reports manually.

```powershell
python -m evals.run --prompt-file app/prompts/tutor_v1.txt --label v1 --html out/v1.html
python -m evals.run --prompt-file app/prompts/tutor_v2.txt --label v2 --html out/v2.html
```

Future enhancement (when we actually have a v2): a `compare` subcommand
that produces a side-by-side diff report.

## Filtering

Run only a subset:
```powershell
python -m evals.run --filter-tag adversarial
python -m evals.run --filter-tag multi_turn
python -m evals.run --filter-tag hu
```

## Where the test cases come from (over time)

1. Initial seed (this commit): cases I drafted from imagination.
2. Real student questions from your friend's surveys → add as cases.
3. Cases the live product fails on (low ratings in Phase 12 quality loop)
   → become regression tests.
4. Authenticated solution graphs (Phase 10) become structured multi-turn
   cases verifying the AI never reveals the answer.

The corpus grows organically. Resist the urge to over-engineer; cases
are cheap to add when needed.
