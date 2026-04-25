# StudAI eval lab

Research-grounded LLM-as-judge framework for measuring tutor prompt quality.

## Why

Every prompt change is a bet. Without measurement we can only argue from
intuition about whether v3 is better than v2. With this lab, every change is
a measurable bet against ~50 fake conversations across 17 pedagogical
dimensions.

## Theoretical basis

The rubric set is grounded in current learning-sciences research on AI
tutoring evaluation:

- **Macina et al. 2025 (EMNLP) — MathTutorBench**: 4 pedagogical principles
  (correctness, scaffolding, encourage self-correction, don't overload),
  3 evaluation skill categories (math expertise, student understanding,
  pedagogical ability). Critical empirical finding: subject expertise and
  pedagogy form a **trade-off** — the better an LLM is at solving math,
  the more it tends to dump answers instead of guiding.

- **Maurya et al. 2025 (NAACL) — MRBench**: 8-dimension taxonomy used by
  human annotators on a curated benchmark (Mistake Identification, Mistake
  Location, Revealing of the Answer, Providing Guidance, Actionability,
  Coherence, Tutor Tone, Humanlikeness). Most of our rubrics map directly.

- **Vail et al. 2016 (EDM)**: distinguishes inference questions
  ("how would you fix this?") from evaluative questions ("do you understand?").
  Inference questions correlate with learning gains; evaluative questions
  with novices don't.

- **VanLehn et al. 2007**: model–scaffold–fade — a hint should match the
  student's evident competence, not be uniform.

- **Graesser et al. 1998 — AutoTutor**: ONE dialogue move per turn beats
  bundling multiple. Cognitive load matters.

The rubrics try to operationalize these principles in a way that's:
1. Evaluatable from a single tutor reply (no need for full session context
   beyond what's in the case).
2. Produces real variance — rubrics that always score 1.0 get pruned.
3. Cheap — entire run is ~€0.06.

## Quick start

```powershell
# from backend/ with venv active and OPENAI_API_KEY set
python -m evals.run                                    # all cases vs current prompt
python -m evals.run --filter-tag adversarial           # only adversarial cases
python -m evals.run --filter-tag long_multi_turn       # only the long ones
python -m evals.run --prompt-file app/prompts/tutor_v2.txt --label v2
```

Output:

- Terminal summary with per-rubric averages, pass rates, weighted total.
- HTML report in `evals/reports/<timestamp>_<label>.html` with full
  per-case detail (response text, every rubric's score + reason).

## Layout

```
backend/evals/
├── lab.py          framework: 17 rubrics, runner, reporter, types
├── cases.yaml      50 test cases — add to grow the corpus
├── run.py          CLI entry (`python -m evals.run`)
├── _check.py       quick sanity check (no LLM calls)
├── reports/        generated HTML reports (gitignored)
└── README.md       you are here
```

## The 17 rubrics

Grouped as in `lab.py`:

### A. Mistake handling — *only on cases where the student made a wrong claim*
| Rubric | Weight | Source |
|---|---|---|
| `mistake_identified` | 2.5 | MRBench |
| `mistake_located` | 2.0 | MRBench, Daheim et al. 2024 |

### B. Solution disclosure
| Rubric | Weight | Source |
|---|---|---|
| `does_not_reveal_answer` | 3.0 | MRBench, MathTutorBench principle (b) |

### C. Guidance quality
| Rubric | Weight | Source |
|---|---|---|
| `provides_guidance` | 2.0 | MRBench |
| `actionable_guidance` | 2.0 | MRBench |
| `single_focused_move` | 1.5 | Graesser AutoTutor; principle (d) |
| `inference_not_evaluative_question` | 2.0 | Vail et al. 2016 EDM |
| `calibrated_to_student` | 1.5 | VanLehn 2007 |

### D. Conversation quality
| Rubric | Weight | Source |
|---|---|---|
| `coherent_with_prior_turns` | 2.0 | MRBench |

### E. Affective / relational
| Rubric | Weight | Source |
|---|---|---|
| `encouraging_tone` | 1.5 | MRBench (3-way scale) |
| `humanlike` | 1.0 | MRBench |
| `avoids_empty_praise` | 2.0 | Sycophancy research; MathTutorBench Table 6 |

### F. Style / form
| Rubric | Weight | Source |
|---|---|---|
| `language_match` | 3.0 | hard product requirement |
| `age_appropriate` | 1.5 | hard product requirement |
| `on_topic` | 2.0 | hard product requirement |
| `uses_latex` | 1.0 | (pattern check, not LLM-judge) |

### G. Mathematical correctness
| Rubric | Weight | Source |
|---|---|---|
| `mathematically_correct` | 3.0 | MathTutorBench principle (a) |

## Cost

Roughly **€0.06 per full run** of 50 cases × ~12 rubrics on average × `gpt-4o-mini` judge (~586 LLM calls).

## Adding a test case

Append to `cases.yaml`:

```yaml
- id: "hu_my_new_case"          # unique slug
  description: "What this case tests, in one line"
  tags: [single_turn, hu, mistake]
  context:
    student_age: 14
    grade: 9
    language: hu                # used by language_match + age_appropriate judges
    has_mistake: true           # informational; mistake rubrics fire when included
  conversation:
    - role: user
      content: "Az itteni szöveg a diák üzenete."
  rubrics:                      # only the rubrics that apply to this case
    - mistake_identified
    - mistake_located
    - does_not_reveal_answer
    - provides_guidance
    - encouraging_tone
    - language_match
    - on_topic
    - mathematically_correct
```

Rules:
- `id` must be unique
- The last turn in `conversation` must be `role: user`
- Each rubric in `rubrics` must exist in `lab.RUBRICS`
- Only include rubrics that *apply* to this case — including
  `mistake_identified` on a case where the student didn't make a mistake will
  produce noise

## Adding a rubric

Open `lab.py`, jump to the rubric template that's most similar to what you
need. Templates follow a consistent structure (per the MathTutorBench paper's
finding that "extended" judge prompts beat simple ones by ~10pp). Add the
template + a new `Rubric(...)` entry in `RUBRICS`.

For pattern-based (deterministic) rubrics use the `pattern_fn=` flavor —
see `uses_latex_pattern` for an example.

## Filtering

```powershell
python -m evals.run --filter-tag adversarial
python -m evals.run --filter-tag long_multi_turn
python -m evals.run --filter-tag mistake
python -m evals.run --filter-tag hu
```

Available tags (see `cases.yaml`): `single_turn`, `multi_turn`,
`long_multi_turn`, `hu`, `en`, `algebra`, `geometry`, `calculus`,
`word_problem`, `lesson`, `concept`, `mistake`, `praise_trap`,
`adversarial`, `off_topic`, `social`, `mood`, `affective`, `stuck`,
`encourage`, `verification`, `lucky`, `persistent`.

## Comparing two prompts (today: manual)

```powershell
python -m evals.run --prompt-file app/prompts/tutor_v1.txt --label v1
python -m evals.run --prompt-file app/prompts/tutor_v2.txt --label v2
```

Compare the two HTML reports side by side. A `compare` subcommand that
auto-diffs them is on the wishlist for when we have multiple prompt
versions in flight.

## How the corpus grows

1. **Initial seed (this commit)**: cases drafted from imagination, calibrated
   to the rubric set.
2. **Survey data**: real student questions from the parent/student surveys
   → add as cases.
3. **Phase 12 quality-loop data**: low-rated live sessions → become
   regression test cases.
4. **Phase 10 authenticated solution graphs**: each problem with a known
   solution path becomes a structured multi-turn case verifying the AI
   never reveals the answer at any branch.

The corpus grows organically. Resist over-engineering — cases are cheap to
add when needed.

## Limitations & honest caveats

- **LLM-as-judge is noisy**. The MathTutorBench paper's best LLM-as-judge
  baseline scored ~80% accuracy at distinguishing expert from novice
  teacher responses; their fine-tuned reward model hit 84%. Treat individual
  scores as soft signal; trust *aggregates* and *deltas between prompt
  versions*.
- **Coverage gaps**: this set focuses on problem-solving and concept
  questions in HU/EN. We don't yet evaluate: long-term memory across
  sessions, factual retrieval from a curated library, multi-modal cases,
  safety edge cases.
- **Self-judging risk**: when both tutor and judge are `gpt-4o-mini`, we
  can't catch failures the judge itself shares. For higher-stakes
  validation use a different judge model (`--judge-model gpt-4o`).
