# Phase 9 — Personalization & Adaptation Layer

> **Read this first if you're a new agent session picking up Phase 9.**
> Every decision below is locked in (the founder approved it after a long
> design conversation). Don't relitigate without revisiting this doc and
> deciding to override deliberately. Cross-reference: the main `README.md`
> Phase 9 entry is intentionally short and points here.

---

## Implementation status (current as of first-pass build)

All five slices were implemented in a single pass. Status:

| Slice | Status | Notes |
|-------|--------|-------|
| 9A — Memory plumbing | **Done** | `sql/006_session_state_and_progress.sql`, `db/schemas.py` (`SessionState`, `StudentProgress`, `MasterySignal`, `SessionStateUpdate`, `PlacementAttempt`), `db/repositories.py` (get/upsert/increment session_state, get/upsert progress, bulk_seed_progress, placement attempts repo), `agents/state_updater.py` (post-turn extractor), `prompts/state_extractor_v1.txt`, `prompts/tutor_v3.{txt,py}` with `CURRENT_TUTOR_PROMPT` bumped, and `agents/tutor.py` `_build_context` extended. Profile gained `share_progress_with_parents` + `preferences jsonb`. |
| 9B — Style directives + grade priors + topic-grade alignment | **Done** | `backend/app/data/grade_priors.json` (HU NAT + US CCSS, K-2 → university, with `topic_aliases` and `grade_level_resolver`), `agents/grade_priors.py` (typed loader + `resolve_grade_band` + `grade_priors_seed`), `agents/style_policy.py` (`StyleDirectives` dataclass, `derive_directives`, three `format_*_block` functions), `agents/topic_classifier.py` (centroid-based classifier, lazy-cached). v3 prompt has explicit STYLE DIRECTIVES recipes per directive value, including the four `register` recipes. |
| 9C — Personality micro-survey | **Done** | i18n strings in `messages/{en,hu}.json` under `onboarding.personality`. Onboarding page at `app/[locale]/dashboard/onboarding/page.tsx` collects answers and saves them to `profiles.preferences`. Settings page extended with the same three radio groups + share-progress consent toggle. `style_policy.derive_directives` reads the preferences. |
| 9D — Hybrid BKT + IRT mastery | **Done** | `agents/mastery.py` implements BKT-IDEM update with item-difficulty-effect guess/slip, source-weighted blend, `update_from_extractor` for noisy signals (weight 0.30 × `\|delta\|`), and IRT-style item selection (`mastery_to_theta`, `pick_difficulty_for`, `next_difficulty_after_outcome`). The post-turn extractor calls into it. |
| 9E — Adaptive placement quiz | **Done** | Backend: `/onboarding/placement/{start,answer,status}` in `api/onboarding.py`. Each answer applies `apply_graded_update` with `evidence_source='placement'`. Self-graded ✓ / "I don't know" for the MVP — automatic correctness scoring arrives in Phase 10. Frontend: integrated into the same onboarding page (intro → 5 staircase questions → completion summary). Skippable. Re-take link in Settings. |

**Cross-cutting:**
- `core/config.py` gained four flags: `state_updater_enabled`, `style_policy_enabled`, `progress_block_enabled`, `session_state_block_enabled`, `state_extractor_max_tokens`, plus `state_extractor_model` (defaults `gpt-4o-mini`).
- `main.py` registers the new `onboarding` router.
- `app/[locale]/dashboard/sessions/page.tsx` redirects new users (no sessions, no preferences) to `/dashboard/onboarding` exactly once.
- New i18n keys live under `settings.section{Style,Privacy}`, `settings.shareProgress*`, and the entire `onboarding.*` namespace.

**Required follow-up before going live:**
1. Run `sql/006_session_state_and_progress.sql` in Supabase SQL editor.
2. Redeploy the backend so the new router and prompt v3 ship.
3. (Optional) `python -m scripts.smoke_tutor_grounding` to confirm grounding still works alongside the new system blocks. Set `GROUNDING_DEBUG_LOG=true` once to verify the directives + state + progress blocks are appearing as expected; turn off again.
4. (Optional eval pass) Add fixtures in `backend/evals/` that cover: 4th-grader-vs-10th-grader same input divergence, anxious-vs-curious affect divergence, 3rd-grader-asks-about-quadratics → `register == above_level_exploration`. Not built in this pass — open task.

### Onboarding iteration (post first-pass demo testing)

After running the first-cut onboarding, four real problems showed up. Fixes landed in a follow-up pass:

| Problem | Fix shipped |
|---------|-------------|
| **`seedGradePriors()` was always returning 0** because grade_level wasn't asked anywhere in onboarding. Result: placement quiz fell back to its hardcoded `"linear equations"` topic for all 5 questions; only one row in `student_progress` after. | New first step **About you** in `app/[locale]/dashboard/onboarding/page.tsx` collects display_name, age, grade_level *before* personality. After save, calls `seedGradePriors()` immediately — now the priors table is seeded with grade-appropriate topics, and `_topic_for_placement_round` rotates through them. |
| **Personality questions were one-size-fits-all**, slightly teen-leaning ("Math feels mostly..." reads weird for an 8-year-old). | i18n now ships paired keys: every question and option has both standard and `*Kid` variant. The personality step picks the kid variant when `age <= 11` (constant `KID_AGE_THRESHOLD` in the page). Both `en.json` and `hu.json` updated. |
| **Self-graded ✓ / I-don't-know was a worthless signal** — kids tap ✓ either way, destroying the placement BKT update. | New `agents/answer_judge.py` + `prompts/placement_judge_v1.txt` — single-token YES/NO LLM judge that takes (problem, canonical answer, student answer) and returns a bool. Strict-string normalized fallback when the LLM call fails. Empty/skip/"I don't know"/"nem tudom" short-circuit to NO without a paid call. `placement/answer` endpoint now takes `student_answer: str` (with `problem_text` and `canonical_answer` echoed back for the judge), runs `judge_answer`, then continues with the existing BKT-IDEM pipeline. Returns `was_correct` and `canonical_answer` so the frontend can show feedback. New config flag: `placement_judge_model` (default `gpt-4o-mini`). |
| **Word problems with `$N` currency rendered as broken italic math** ("Edward spent $6 to buy 2 books..." → `6tobuy2bookseachbookcosting...`). The corpus uses bare `$` as a currency symbol; `remark-math` was treating those as `$...$` LaTeX delimiters. | New `escapeBareCurrency()` in `components/chat/markdown-content.tsx`. Heuristic: if any `$...$` pair contains a math indicator (`\`, `^`, `_`, `{`, `}`, `=`), assume math and leave alone; otherwise escape every `$` as `\$`. Conservative — false-negative ("we left a real bug unrendered") is safer than false-positive ("we corrupted real math"). Runs before `normalizeMathDelimiters`. |

**Frontend changes summary (this iteration):**
- `app/[locale]/dashboard/onboarding/page.tsx` — new `Step` union with `aboutYou`, `feedback` added; `AboutYouCard`, `PlacementFeedbackCard` components; placement now uses a free-text `Input` + Submit instead of self-grade buttons; "I don't know" submits empty string and short-circuits to incorrect.
- `lib/api/onboarding.ts` — `PlacementAnswerRequest` now has `student_answer`, `problem_text`, `canonical_answer`; `PlacementAnswerResponse` adds `was_correct` and `canonical_answer`.
- `messages/{en,hu}.json` — full `onboarding.aboutYou.*` namespace + `*Kid` variants for every personality string + placement feedback strings (`feedbackCorrect`, `feedbackIncorrect`, `correctAnswerLabel`, `nextQuestion`, `viewResults`, `topicsHeading`).

**Backend changes summary (this iteration):**
- `backend/app/agents/answer_judge.py` (new)
- `backend/app/prompts/placement_judge_v1.txt` (new)
- `backend/app/api/onboarding.py` — `PlacementAnswerRequest`/`Response` updated; endpoint calls `judge_answer` before BKT update.
- `backend/app/core/config.py` — added `placement_judge_model`.

**Deferred to a later pass (still relevant for product polish):**
- The judge LLM call adds ~300-700ms latency between the user submitting and the feedback card appearing. Acceptable for a 5-question placement; if it ever feels sluggish, switch to streaming or pre-judge in parallel with the next-question fetch.
- The corpus `problems.answer` field is sometimes verbose ("8 books, since 24 / 3 = 8"). The judge prompt handles it but the feedback card displays it verbatim, which can be cluttered. A tiny "extract canonical short answer" step at ingestion time would help — defer to a later content cleanup pass.

### Onboarding iteration #2 (placement quality fixes)

After the first iteration shipped, second-round testing surfaced a deeper bug stack: the placement quiz was showing 5 unrelated questions all labeled "linear equations", with synthetic asdiv/svamp template gibberish like "87 oranges in 93 groups". Root causes (and fixes):

| Problem | Root cause | Fix shipped |
|---------|-----------|-------------|
| **Topic label and shown problem are unrelated.** Quiz says "linear equations" but shows a geometry problem. | `repo.fetch_problem_for_placement` filtered only by `difficulty` — there was no topic filtering at all. | New `_pick_topic_relevant_problem()` in `api/onboarding.py` reuses the existing problem-bank embedding search (`agents/retrieval.find_relevant_problems`) with `similarity_threshold=0.0` to pull the top 20 candidates by topic name, then `repo.fetch_problem_for_placement_by_ids()` enforces source allowlist + length sanity on top, preserving rank order. Used by both `/placement/start` and `/placement/answer`. |
| **All 5 questions on linear equations.** | `_topic_for_placement_round` rotated by `progress[i % len(progress)]` after sorting by `(evidence_count asc, mastery_score asc)` — so once the first turn produced the only evidence row, every subsequent turn picked it again. | Now picks from `[p for p in progress if p.evidence_count == 0]` first (sorted by ascending mastery). Only falls back to the full progress list when *all* priors have been touched. With ~11+ seeded topics this means up to 5 distinct topics in a 5-question quiz. |
| **No grade priors got seeded.** `seedGradePriors()` returned `seeded=0` for many natural inputs (`"4 osztály"`, `"4th grade"`, `"4"`). | `resolve_grade_band` previously only did substring matching against a fixed table of patterns, all of which required a literal period after the digit (`"4."`) or a specific noun phrase (`"grade 4"`). Real users typed many other forms. Now the resolver runs **regex extraction first** (number + noun, in either language, period optional), then the legacy substring table, then a bare-integer fallback. Spelled-out English ordinals work too. Side fix: `grade_priors_seed()` accepts `age=` and falls back to `band_for_age()` when grade resolution fails entirely; `/onboarding/seed-priors` passes `profile.age` so even a typo'd grade still seeds reasonable priors. |
| **Synthetic asdiv/svamp template gibberish** ("87 oranges organized into 93 groups"). | `fetch_problem_for_placement` pulled from the entire 18k corpus with no source filter. | Constants `_PLACEMENT_SOURCE_ALLOWLIST = ("hendrycks", "gsm8k", "openstax")` and `_PLACEMENT_MIN_LEN/MAX_LEN = 30/600` in `repositories.py`. Both placement fetcher functions enforce them. **Live RAG remains uncapped** — the noisy datasets stay useful as private context for the tutor, just not as problems shown verbatim to the student. |
| **Answer input dropdown shows previous attempts** (browser autocomplete on the placement input). | Default browser behavior on text inputs. | `<Input>` in `app/[locale]/dashboard/onboarding/page.tsx` got `autoComplete="off"`, `autoCorrect="off"`, `spellCheck={false}`, `data-form-type="other"` (last one neutralizes 1Password / LastPass injection). |

**New helpers:**
- `agents/grade_priors.py::band_for_age(age)` — public function used by both `style_policy.derive_directives` (already had inline logic) and the seed endpoint.
- `agents/grade_priors.py::_band_for_grade_number(grade)` — single source of truth for the integer → band mapping.
- `db/repositories.py::fetch_problem_for_placement_by_ids(...)` — picks the first acceptable problem from a ranked candidate ID list, preserving the semantic-search rank.
- `api/onboarding.py::_pick_topic_relevant_problem(...)` — orchestrator: embed topic → semantic search → repo filter → fallback chain.

### Onboarding iteration #3 (placement durability + summary clarity)

After iteration #2, third-round testing surfaced two more issues:

1. **The quiz terminated after a single question.** Symptoms: user submitted answer 1, was taken straight to the completion screen. Root cause: `mastery.pick_difficulty_for()` returns logical buckets (`"easy"`, `"medium"`, `"hard"`), but the corpus stores difficulty as `"Level 1"` through `"Level 5"` (Hendrycks) or `null`/`"easy_medium"` (others). `repo.fetch_problem_for_placement(filter_difficulty="easy")` matched **zero rows**, every fallback in `_pick_topic_relevant_problem` returned `None`, and the `placement/answer` endpoint sent `next=None, completed=True`.

2. **Completion screen showed mastery rows that looked like quiz outcomes but were actually grade priors.** A user who answered one question with "I don't know" saw eight topics with percentages (decimals 80%, division 90%, etc.) and read it as "based on the quiz". They came from `seedGradePriors` and were *correct* numbers — just labeled wrong.

**Fixes (this iteration):**

| Area | Change |
|------|--------|
| **`agents/mastery.py`** | New `_LOGICAL_TO_CORPUS_DIFFICULTIES` table + public `corpus_difficulties_for(logical) -> list[str]`. Maps `"easy"` → `["easy", "Level 1", "Level 2", "easy_medium"]`, etc. Single source of truth used by both placement fetchers. |
| **`db/repositories.py`** | Both `fetch_problem_for_placement` and `fetch_problem_for_placement_by_ids` now take `filter_difficulties: list[str] \| None` and use a Postgrest `IN` filter. Empty list / None means "no difficulty filter". Length window relaxed to 40 candidates pre-filter so length sanity has more to choose from. |
| **`api/onboarding.py::_pick_topic_relevant_problem`** | Now translates the logical difficulty via `corpus_difficulties_for` before any DB call. Five-step fallback chain (each step strictly looser than the previous): semantic + difficulty bucket → semantic only → difficulty bucket only (allowlist) → no filters except allowlist → `None`. Quiz survives sparse / unembedded corpora. |
| **Frontend completion card** | New `ProgressList` subcomponent. Splits `summary` rows by `evidence_source`: rows with `'placement'` (or `'extractor'`/`'rating'`/`'step_check'`) go under **"From your answers"**; rows with `'prior'` go under **"From your grade level (estimates)"** with a small explanatory caption. The "From your grade" section is capped at 8 rows so it doesn't dominate the page. New i18n keys: `topicsFromQuizHeading`, `topicsFromGradeHeading`, `topicsFromGradeNote`. The previous one-bucket "Where you are now" heading is replaced with "Where you're starting" / "Innen indulunk" — a more honest framing. |

**Smoke-tested mappings:**

```
corpus_difficulties_for('easy')    -> ['easy', 'Level 1', 'Level 2', 'easy_medium']
corpus_difficulties_for('medium')  -> ['medium', 'Level 2', 'Level 3', 'Level 4', 'easy_medium']
corpus_difficulties_for('hard')    -> ['hard', 'Level 4', 'Level 5']
corpus_difficulties_for(None)      -> []
corpus_difficulties_for('garbage') -> []
```

### Onboarding iteration #4 (per-band placement profile)

Iteration #3 fixed the difficulty mapping but left a more fundamental issue: a 4th grader entering grade `"4"` was getting Hendrycks-flavoured "decimals" problems (Olympiad math) instead of gsm8k word problems. Hendrycks "Level 1" is still 9th-grade-AMC competition math; the source allowlist (`hendrycks`, `gsm8k`, `openstax`) was too coarse for the placement quiz.

**Fix:** corpus subset is now selected **per grade band**.

| Component | Change |
|-----------|--------|
| **`agents/grade_priors.py`** | New frozen dataclass `PlacementProfile(sources: tuple[str,...], difficulty_map: dict[str, list[str]] \| None)` plus `placement_profile_for_band(band)` and `placement_profile_for_user(grade_level, age)`. The mapping: `K-2`/`3-5`/`6-8` → `("gsm8k",)` with default difficulty mapping; `9-10` → `("hendrycks", "gsm8k")` with override `easy=Level 1`, `medium=Level 1-2`, `hard=Level 2-3`; `11-12` → `("hendrycks", "openstax")` with shifted-up override; `university` → `("hendrycks",)` Level 3-5 only. Unknown band falls back to a permissive `(hendrycks, gsm8k)` default. |
| **`db/repositories.py`** | The global `_PLACEMENT_SOURCE_ALLOWLIST` was renamed to `_PLACEMENT_DEFAULT_SOURCES` and is only used as a *fallback default*. Both `fetch_problem_for_placement` and `fetch_problem_for_placement_by_ids` now take an explicit `sources: list[str] \| None` parameter — per-band callers always pass an explicit list from `placement_profile_for_band`. |
| **`api/onboarding.py`** | New helpers: `_difficulties_for_request(profile, logical) -> list[str]` (uses the band's override if present, else default mapping) and `_load_placement_profile(user_id) -> PlacementProfile` (loads profile, resolves grade or falls back to age). Both `placement_start` and `placement_answer` call `_load_placement_profile` once per request and pass the result through to `_pick_topic_relevant_problem`, which now takes `placement_profile` and threads `sources` + `difficulty_map`-derived `diffs` through the entire fallback chain. |

**Smoke-tested band → profile mappings (confirmed before deletion):**

```
band         -> sources                      difficulty_map
K-2/3-5/6-8  -> ['gsm8k']                    (default)
9-10         -> ['hendrycks', 'gsm8k']       easy=L1, medium=L1-2, hard=L2-3
11-12        -> ['hendrycks', 'openstax']    easy=L2, medium=L3, hard=L4-5
university   -> ['hendrycks']                easy=L3, medium=L4, hard=L5
None/unknown -> ['hendrycks', 'gsm8k']       (default)
```

```
grade='4. évfolyam' / '4' / '4th grade'  + age=10  -> ('gsm8k', default)
grade='9. évfolyam'                       + age=15  -> 9-10 profile
grade='Grade 11'                          + age=17  -> 11-12 profile
grade='University 2nd year'               + age=20  -> university profile
grade=None                                + age=9   -> ('gsm8k', default)   [age fallback]
grade='asdf'                              + age=11  -> ('gsm8k', default)   [age fallback to 6-8]
grade=None                                + age=None -> ('hendrycks', 'gsm8k') [permissive default]
```

**Note on corpus dependency:** the K-8 fix relies on you having ingested gsm8k. If you only ran `python -m scripts.ingest_problems --source hendrycks`, K-8 students will fall through the five-step fallback chain and eventually serve a Hendrycks problem (better than dying). To use the per-band feature properly: `python -m scripts.ingest_problems --source gsm8k --embed`.

**Live tutor RAG is unchanged** — it still pulls from the full corpus (asdiv/svamp included) because those problems are private grounding context, not shown verbatim. The band-source filter applies *only* to the placement quiz.

**Resolver behavior** (confirmed in a throwaway script before deletion):

```
'9. évfolyam'      -> ('hu_nat', '9-10')
'4 osztály'        -> ('hu_nat', '3-5')   # no period
'4. evf'           -> ('hu_nat', '3-5')   # short form
'4th grade'        -> ('us_ccss', '3-5')
'fourth grade'     -> ('us_ccss', '3-5')
'Year 11'          -> ('us_ccss', '11-12')
'12'               -> ('us_ccss', '11-12')   # bare digit
'9th'              -> ('us_ccss', '9-10')
'PhD'              -> ('us_ccss', 'university')
'kindergarten'     -> ('us_ccss', 'K-2')
'asdf' + age=10    -> 14 priors via age fallback
None + age=15      -> 34 priors
```

**No new migrations.** All changes are code-only on top of `006`.

---

## TL;DR — what Phase 9 is, in one paragraph

Phase 9 turns StudAI from "GPT with a system prompt" into a tutor whose
adaptation is **measurable, scientifically grounded, and visibly different
across student profiles**. It does this by introducing a memory substrate
(`session_state`, `student_progress`), a deterministic **style-policy
layer** between the student model and the LLM prompt, a **hybrid BKT+IRT
mastery model**, a three-layer **cold-start strategy** (grade priors →
personality micro-survey → optional adaptive placement quiz), and a new
tutor prompt **v3** that consumes structured directives instead of
freeform "be age-appropriate" guidance. The investor-facing payoff: the
same student message produces visibly different replies for a 4th grader
vs. a 10th grader vs. a confident vs. anxious learner — and we can
defend every difference by pointing at concrete inputs.

---

## What was already shipped before Phase 9 began

Some of the original "Phase 9" scope from the README landed during
Phase 7 / 8 plumbing and is **already done**:

- `sql/004_profile_extensions.sql` — added `age, grade_level, interests,
  learning_goals, notes` to `public.profiles` with RLS + length caps.
- `app/[locale]/dashboard/settings/page.tsx` — full profile form,
  reads/writes `profiles` directly via Supabase JS.
- `backend/app/agents/tutor.py::_format_profile_snippet` — already weaves
  profile into the system context as a separate `system` message,
  loaded in parallel with history + grounding.

What's **not** done yet (and is the actual work of Phase 9 below):
session_state, student_progress, the style-policy layer, mastery math,
cold-start strategy, prompt v3, and topic-grade alignment.

---

## Decisions (locked — all approved)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Free-text topic strings now**; FK to canonical `topics` table later | Phase 11 introduces the real taxonomy. Free text is faster to ship; data migration is straightforward when Phase 11 lands. |
| 2 | **New tutor prompt v3** in `prompts/tutor_v3.{txt,py}`; bump `CURRENT_TUTOR_PROMPT` | Don't amend v2 — preserves prompt evolution traceability and the eval harness can A/B v2 vs v3. |
| 3 | **Post-turn LLM extractor** runs fire-and-forget, same pattern as `_check_answer_leak` | Zero latency impact on the user-facing stream. Failures are non-fatal; state goes stale, not corrupt. |
| 4 | **Style-directives layer** between profile/state and prompt | Adaptation becomes deterministic, auditable, testable. Not "be age-appropriate" — explicit knobs the LLM follows. This is the technical centerpiece. |
| 5 | **Hybrid BKT + IRT** for mastery (BKT state + IRT-modulated update + IRT item selection) | BKT gives a single per-skill number that's easy to display and reason about; IRT properly weights evidence by problem difficulty and powers adaptive item selection. See "Mastery model" section below. |
| 6 | **Three-layer cold start**: grade priors (always-on), personality micro-survey (always-on at signup), adaptive placement quiz (optional skip) | Prevents the "empty profile = empty tutor" failure. Each layer is independently valuable. |
| 7 | **Both Hungarian NAT and US Common Core** in the grade-priors table | TAM is HU first but EN matters for the corpus. Translating topic labels EN↔HU is cheap; getting the priors right per curriculum is the value. |
| 8 | **Topic-grade alignment as a structured pre-LLM check**, not LLM guidance | Detect topic from the same embedding we already compute for RAG, compare to grade-band map, inject a `register` directive if the gap is large. The LLM follows explicit directives much better than abstract instructions. |
| 9 | **`evidence_source` column** on every progress update | Distinguishes noisy extractor signals from clean rating/step-check signals. Lets us weight differently and audit later. |
| 10 | **`share_progress_with_parents` consent flag** added now (Phase 13 will read it) | Cheap to add now; expensive to retrofit after parents are linked. |

---

## Architecture overview

```
+---------+   +---------------+   +-----------------+
| profile | + | session_state | + | student_progress|
+----+----+   +-------+-------+   +--------+--------+
     |                |                    |
     +----------------+--------------------+
                      |
                      v
            +-------------------+
            |  style_policy.py  |  (deterministic)
            +---------+---------+
                      |
                      v
       +--------------------------+
       |  STYLE DIRECTIVES block  |  injected as private system msg
       +-------------+------------+
                     |
                     v
       +-------------+------------+
       |  prompts/tutor_v3.txt    |  reads directives + register
       +-------------+------------+
                     |
                     v
                  LLM call
                     |
                     v
       +-------------+------------+
       | post-turn extractor      |  fire-and-forget, writes back
       | (state + progress delta) |  to session_state + progress
       +--------------------------+
```

System-message order in the prompt (top → bottom):

1. Persona (`tutor_v3.txt`)
2. Profile snippet (existing `_format_profile_snippet`)
3. **Style directives** (NEW — derived from profile + state + progress)
4. **Student progress** (NEW — top ~5 topics by recency)
5. **Session state** (NEW — current_topic, mode, attempts, struggling_on, summary)
6. Grounding L1 (problem-bank RAG, existing)
7. Grounding L2 (OpenStax excerpts, existing)
8. Grounding L3 (precomputed annotations, existing)
9. Recent history (truncated; replaced by `session_state.summary` past N turns)
10. User turn

---

## Mastery model: hybrid BKT + IRT

**State (BKT):** `student_progress.mastery_score ∈ [0,1]` per `(user_id, topic)`.

**Update rule (BKT-IDEM, Pardos & Heffernan 2011):** standard BKT but
the guess/slip probabilities are modulated by item difficulty.

```python
# Difficulty mapping (problems.difficulty is currently a string)
b = {"easy": -1, "medium": 0, "hard": +1}[difficulty]

# Item-difficulty-effect guess/slip
P_G = clip(0.2 - 0.1 * b, 0.05, 0.4)   # easy → 0.30, hard → 0.10
P_S = clip(0.1 + 0.1 * b, 0.05, 0.4)   # easy → 0.05, hard → 0.20
P_T = 0.10                              # transit per attempt

# Bayesian update on observation
if correct:
    posterior = prior * (1 - P_S) / (
        prior * (1 - P_S) + (1 - prior) * P_G
    )
else:
    posterior = prior * P_S / (
        prior * P_S + (1 - prior) * (1 - P_G)
    )

mastery = posterior + (1 - posterior) * P_T
```

**Item selection (IRT, 1-parameter Rasch):** for the placement quiz
and any "what should I show next" decision:

```python
theta = logit(mastery_score)        # student ability per topic
# pick problem whose b is closest to theta (argmin |b - theta|)
```

For our discrete `{easy, medium, hard}` corpus this collapses to picking
the bucket nearest `round(theta)`. When Phase 10/12 produce real outcome
data we can fit continuous `b` values per problem and the same code keeps
working — only the difficulty lookup changes.

**Citations to keep in marketing/legal:**

- Corbett & Anderson (1994) — original BKT.
- Pardos & Heffernan (2011) — KT with item difficulty effect.
- Lord (1980), de la Torre (2009) — IRT background.

**Where each evidence source plugs in:**

| Source | Phase | `evidence_source` value | Weight | Notes |
|--------|-------|------------------------|--------|-------|
| Grade priors | 9A/9B | `'prior'` | n/a (only seeds) | Static lookup, never updates after first seed |
| Placement quiz | 9E | `'placement'` | full | Treated as graded |
| Personality micro-survey | 9C | n/a (writes profile, not progress) | — | — |
| Post-turn LLM extractor | 9A | `'extractor'` | reduced (e.g. 0.3) | Noisy — soft updates only |
| Explicit 👍/👎 | Phase 12 | `'rating'` | medium | Cleaner than extractor, but still indirect |
| Verified step-check | Phase 10 | `'step_check'` | full | Gold standard once it exists |

The "weight" idea is implemented by scaling `P_T` and the strength of
the Bayesian update for low-weight sources. Concrete formula TBD during
9D — the simplest version is to multiply the posterior shift by a
weight factor and clip to `[0,1]`.

---

## Cold start (3 layers)

Each layer is independently valuable; they compose.

### Layer 1 — Grade-based priors (always-on, free)

A static lookup table: `(curriculum, grade_band, topic) → prior_mastery`.

- **Curricula:** Hungarian NAT (`'hu_nat'`), US Common Core (`'us_ccss'`).
- **Grade bands:** keep coarse — e.g. `K-2`, `3-5`, `6-8`, `9-10`,
  `11-12`, `university`. Avoids tuning per individual grade where
  curriculum overlap is high.
- **Topic granularity:** match the free-text topics the extractor
  produces (after normalization). We can refine when Phase 11 lands.
- **Format:** committed JSON file in `backend/app/data/grade_priors.json`
  (cheap, version-controlled, reviewable in PRs).
- **Source for priors:** curriculum standards documents (NAT 2020 for
  HU, CCSSM for US). Cite in `docs/grade_priors_sources.md` (TODO).
- **Use:** when a new student fills in `grade_level`, write priors for
  ~20-30 core topics into `student_progress` with
  `evidence_source = 'prior'`. The tutor already adapts on turn one.

### Layer 2 — Personality micro-survey at signup (always-on, ~60 seconds)

Three multiple-choice questions, posted once at signup (skippable but
encouraged). Stored on `profiles` (probably as a `preferences jsonb`
column added in migration 006 alongside session_state/progress, OR as
explicit columns — TBD during 9C).

| Question | Options | Maps to style directive |
|----------|---------|-------------------------|
| When you're stuck on a problem, what helps most? | fast hints / figure it out yourself / a worked example | `hint_timing` (early/late/by-example) |
| Math feels mostly… | fun and curious / fine, just a subject / hard, I get anxious | `affect`, `praise_frequency` |
| What kind of problems do you like? | word problems with stories / pure equations / visual/geometry | `example_flavor` |

These three questions cover the big variance in tutoring style for
~120 seconds of friction. Storing answers in `profiles` keeps them
stable across sessions; the user can revise them in Settings.

### Layer 3 — Adaptive placement quiz (optional, ~5 minutes)

After signup, offer "Want to take a quick check so I know where to
start?". User can skip.

- 5 questions per session.
- Start at declared grade-level difficulty (`b ≈ 0` in the grade band).
- Adaptive staircase: right → harder; wrong → easier.
- Each answer Bayes-updates `student_progress` with
  `evidence_source = 'placement'` (full weight).
- After 5 questions, `theta` for ~3 topics is well-localized.

Frontend: dedicated onboarding flow under `app/[locale]/dashboard/
onboarding/` (or similar). Reuses existing `MessageBubble` /
`MathRenderer` for problem display.

---

## Topic-grade alignment (the "3rd grader asks about quadratics" rule)

The desired behavior is *not* "refuse" and *not* "tutor as if normal".
It's: validate curiosity, give a concrete intuition, mark as exploration
rather than mastery, offer to redirect.

**Mechanism:**

1. On each turn, before the LLM call, classify the student's question's
   topic. We already compute an embedding for RAG — reuse it. Cheapest
   path: nearest topic in a small precomputed embedding lookup
   (`topic_label → centroid_embedding`).
2. Look up `(topic, grade_band)` in `grade_priors.json` → expected
   prior mastery for the student's grade.
3. If prior < 0.05 → topic is **above level**. Set
   `style_directives.register = 'above_level_exploration'`.
4. If prior > 0.95 → topic is **below level**. Set
   `register = 'below_level_warmup'`.
5. Otherwise → `register = 'at_level'` (default).

The `tutor_v3.txt` prompt has explicit recipes for each register:

```
REGISTER = above_level_exploration:
- Validate the curiosity warmly ("cool question, that's a topic for
  older students").
- Give ONE concrete intuition (e.g. parabola = path of a thrown ball).
- Do NOT pose practice problems or ask Socratic questions.
- Offer to redirect: "want to keep exploring, or shall we work on
  something for your grade?"
```

This is testable in the eval harness — a fixture with grade=3 + question
about quadratics should produce a reply that contains zero "what's the
next step?" patterns.

---

## Style directives — the deterministic adaptation layer

A small struct, computed by `style_policy.py` from `(profile,
session_state, student_progress, register)`. Injected into the prompt
as a private system block.

### The directives

| Directive | Values | Primary inputs |
|-----------|--------|----------------|
| `vocabulary_level` | `concrete-everyday`, `concrete-mathy`, `abstract-formal` | grade band |
| `step_size` | `micro`, `normal`, `leap-allowed` | grade, mastery on current topic, attempts_count |
| `praise_frequency` | `high`, `medium`, `sparse` | personality (affect), age |
| `hint_timing` | `early`, `balanced`, `late` | personality, attempts_count |
| `example_flavor` | `story-narrative`, `visual`, `pure-math`, `mixed` | personality |
| `register` | `at_level`, `above_level_exploration`, `below_level_warmup`, `remedial` | topic-grade alignment |
| `affect` | `curious-engaged`, `neutral`, `anxious-needs-reassurance` | personality, session_state.mood_signals |

### Injection format

```
STYLE DIRECTIVES (private — follow exactly, do not recite):
- Vocabulary level: concrete-everyday
- Step size: micro
- Praise frequency: high
- Hint timing: early
- Example flavor: story-narrative
- Register: at_level
- Affect: curious-engaged
```

The v3 prompt has a corresponding **"How to read STYLE DIRECTIVES"**
section that defines what each value means in concrete tutoring terms.

### Why this is the technical centerpiece

- **Auditable.** Every adaptation decision is a function from explicit
  inputs to explicit outputs. We can log `(profile, state, progress) →
  directives` and review.
- **Testable.** Eval fixtures can lock the directives and assert the
  reply matches; or vary one directive and assert the reply changes
  in the expected dimension.
- **Demoable.** Show an investor the same input message, two students,
  side-by-side directive changes, side-by-side reply differences.
- **Composable.** When Phase 15 (emotion) lands, it just writes
  `affect` from audio signals; when Phase 12 (ratings) lands, it just
  refines mastery which feeds `step_size` and `hint_timing`. The
  contract is stable.

---

## Slices and shipping order

Each slice is independently shippable and (from 9B onward) demo-able.

### 9A — Memory plumbing (~1 week)
- `sql/006_session_state_and_progress.sql` (idempotent, RLS).
- New rows in `db/schemas.py`: `SessionState`, `StudentProgress`.
- New repo functions: `get_session_state`, `upsert_session_state`,
  `get_top_progress`, `bump_progress_raw` (heuristic, replaced in 9D).
- `agents/state_updater.py` — post-turn LLM extractor.
- Extend `agents/tutor.py::_build_context` with state + progress blocks
  and `asyncio.gather` to load them.
- `prompts/tutor_v3.{txt,py}` (initial draft — directives section
  arrives in 9B, but the v3 file itself ships here so we can iterate).
- Add `share_progress_with_parents bool` to `profiles`.
- **Demo difference:** none yet. Internal-only foundation.

### 9B — Style directives + grade priors + topic-grade alignment (~4–5 days)
- `backend/app/data/grade_priors.json` — both `hu_nat` and `us_ccss`,
  ~20-30 topics × ~6 grade bands.
- `agents/style_policy.py` — pure function `(profile, state, progress)
  → StyleDirectives`.
- `agents/topic_classifier.py` (or fold into `retrieval.py`) — embed
  the user message, find nearest topic centroid.
- Prompt v3 gains: STYLE DIRECTIVES section, register recipes.
- Eval fixtures: same prompt, vary grade/personality, assert directives
  + reply diverge as expected.
- **Demo difference:** **first big one.** Same input → visibly
  different output across grades and personalities.

### 9C — Personality micro-survey (~3 days)
- Migration: extend `profiles` with personality fields (or `preferences
  jsonb` — decide during 9C).
- Frontend: 3-question modal/page after signup, results POSTed to
  Supabase via existing client.
- Update `style_policy.py` to read personality fields.
- **Demo difference:** new signup → divergent first conversation
  before anyone has typed anything.

### 9D — Hybrid BKT + IRT mastery (~3–4 days)
- `agents/mastery.py` — `update_mastery(prior, correct, difficulty,
  weight) → posterior`.
- Replace heuristic `bump_progress_raw` with the BKT-IDEM update.
- Add `evidence_source` column to `student_progress`; backfill for
  existing rows as `'extractor'`.
- IRT item selector for the placement quiz.
- Optional: tiny "topics you're working on" read-only widget in
  Settings showing mastery numbers (great for demo).
- **Demo difference:** real numbers crawl up over a session;
  defensible "scientifically grounded mastery model" line in the deck.

### 9E — Adaptive placement quiz (~1 week, OPTIONAL onboarding skip)
- New table `placement_attempts` (or just rows in `messages` with
  special role/metadata — TBD).
- Frontend: onboarding route, 5-question staircase using IRT selector.
- Writes `student_progress` rows with `evidence_source = 'placement'`.
- Skip button always visible; default behavior on skip is "rely on
  grade priors only".
- **Demo difference:** the killer cold-start moment. Investor watches
  a 4th grader and a 10th grader sign up; their first chats are
  visibly different.

**Total Phase 9 effort:** ~3–4 weeks for 9A–9D; +1 week for 9E.

---

## Deferred items — and when they come back

Anything we explicitly chose to NOT do in Phase 9 lives here so future
sessions don't reintroduce it.

| Deferred | Comes back in | Why deferred |
|----------|---------------|--------------|
| Canonical `topics` table | **Phase 11** (lesson mode) | Free-text strings work fine for now; Phase 11 is the natural place for the taxonomy. |
| Full IRT calibration with continuous `b` per problem | After **Phase 12** has 1k+ ratings | Need outcome data to fit; until then `{easy, medium, hard}` is enough. |
| Voice-derived affect signals | **Phase 15** | Will overwrite/augment `session_state.mood_signals`. The schema field is already there. |
| Verified step-checking as evidence source | **Phase 10** (solution graphs) | Gold-standard signal but needs the graph infrastructure. |
| Mastery decay / spaced repetition (forgetting curve) | After multi-week retention data exists | Needs real session-over-session usage. |
| Parent dashboard reading `student_progress` | **Phase 13** | The `share_progress_with_parents` consent flag is added now to avoid retrofit. |
| Continuous personality model (vs 3 multi-choice) | Possibly never; revisit if signal looks weak | Cheap baseline first. |
| LLM-generated personalized lesson plans from progress | **Phase 11** | Lesson mode owns this. |
| A/B testing v2 vs v3 prompt | After v3 ships and we have ratings | **Phase 12** explicitly. |

---

## Future integration points (so Phases 10+ don't surprise us)

This section is the contract: any phase below WILL read or write the
fields named here, so we can't quietly rename them.

- **Phase 10 (solution graphs):**
  - reads `session_state.current_topic`, `session_state.attempts_count`
  - writes `student_progress` with `evidence_source = 'step_check'`
  - reads `style_directives.step_size` to decide guidance density
- **Phase 11 (lesson mode):**
  - reads `student_progress` to recommend topics
  - reads `session_state.mode` (and may set it to `'lesson'`)
  - introduces canonical `topics` table; will need a one-time data
    migration from free-text strings in `session_state.current_topic`
    and `student_progress.topic`
- **Phase 12 (quality loop):**
  - writes `student_progress` with `evidence_source = 'rating'`
  - feeds A/B test data on `prompts/tutor_v3.txt` vs successors
- **Phase 13 (parent view):**
  - reads `profiles.share_progress_with_parents` (consent gate)
  - reads `student_progress` and a derived "topics seen this week" view
- **Phase 15 (emotion):**
  - writes `session_state.mood_signals` (jsonb) from audio features
  - feeds `style_directives.affect`

---

## Schema sketch (for migration 006)

```sql
-- session_state: per-session jsonb-ish snapshot, written by extractor
create table public.session_state (
  session_id        uuid primary key references public.tutor_sessions(id) on delete cascade,
  current_topic     text,
  mode              text check (mode in ('problem','concept','verification','conversational','lesson')),
  attempts_count    int  not null default 0,
  struggling_on     text,
  mood_signals      jsonb not null default '{}'::jsonb,
  summary           text,
  updated_at        timestamptz not null default now()
);
-- RLS: same shape as messages — join through tutor_sessions.user_id

-- student_progress: per-(user, topic) mastery
create table public.student_progress (
  user_id          uuid not null references public.profiles(id) on delete cascade,
  topic            text not null,
  mastery_score    numeric not null default 0.5 check (mastery_score between 0 and 1),
  evidence_count   int     not null default 0,
  evidence_source  text    not null default 'prior'
                   check (evidence_source in ('prior','placement','extractor','rating','step_check')),
  last_seen_at     timestamptz not null default now(),
  primary key (user_id, topic)
);
-- RLS: own rows only (auth.uid() = user_id)

-- profiles: add consent flag and (optionally) personality preferences
alter table public.profiles
  add column if not exists share_progress_with_parents boolean not null default false,
  add column if not exists preferences jsonb not null default '{}'::jsonb;
-- preferences shape (TBD during 9C):
-- { "hint_style": "...", "math_affect": "...", "example_flavor": "..." }
```

Final shape gets locked when 9A is implemented; this is the working
sketch.

---

## Risks / things to watch

- **Token bloat.** Persona + profile + directives + progress + state +
  3 RAG layers + history is a lot. Mitigations: per-block char caps
  (already present for profile), `style_directives` block is compact
  (~10 lines), summarize history past N turns into
  `session_state.summary`.
- **Topic taxonomy drift.** Free-text topics from a small LLM will
  produce `"quadratic equations"`, `"quadratics"`, `"másodfokú
  egyenletek"` for the same thing. Normalize on write: lowercase,
  strip, optional embedding-dedup against existing topics for the same
  user. Phase 11 cleans this up canonically.
- **Memory leakage to the visible reply.** v2 already forbids "Do you
  understand?". v3 must firmly forbid the cousin failure: "Last week
  you struggled with quadratics — want to revisit?" on turn 1 of a
  new session. Privacy of memory blocks is non-negotiable.
- **Prompt instruction overload.** Many system messages can cause the
  LLM to revert to default behavior. Eval the v3 prompt with a full
  context payload before declaring 9B done.
- **Idempotence under SSE drops.** Post-turn extractor must be safe to
  re-run; consider a lazy reconciliation on next session load.
- **Extractor cost.** One small LLM call per turn. At gpt-4o-mini
  prices this is negligible (<$0.001/turn) but track it; if it scales
  badly we can move to a cheaper local classifier or batch.

---

## Open questions (decide when you reach the relevant slice)

- During 9A: should `session_state.summary` be regenerated from scratch
  every ~10 turns or appended-and-truncated? (Recommendation: append
  one-sentence delta per turn; full regen every 10.)
- During 9B: should `topic_classifier` use a precomputed centroid
  lookup, or a tiny LLM call? (Recommendation: centroid first; LLM
  fallback only if confidence is low.)
- During 9C: explicit columns on `profiles` for personality, or one
  `preferences jsonb`? (Lean: jsonb for flexibility, with documented
  shape.)
- During 9D: how aggressively should low-weight (extractor) sources
  move mastery? (Recommendation: scale posterior shift by 0.3.)
- During 9E: place skipped users straight into chat, or show a "hello,
  I'll get to know you as we go" intro? (UX call, defer.)

---

## Citations to keep

- Corbett, A. T., & Anderson, J. R. (1994). *Knowledge tracing:
  Modeling the acquisition of procedural knowledge.* User Modeling
  and User-Adapted Interaction, 4(4).
- Pardos, Z. A., & Heffernan, N. T. (2011). *KT-IDEM: Introducing
  item difficulty to the knowledge tracing model.* UMAP 2011.
- Lord, F. M. (1980). *Applications of item response theory to
  practical testing problems.*
- de la Torre, J. (2009). *DINA model and parameter estimation.*
- AutoTutor / Vail et al. (2016) — already cited in v2 for inference
  questions.
- Hungarian National Core Curriculum (NAT 2020).
- Common Core State Standards for Mathematics (CCSSM).

---

## How this doc is meant to be used

- **New session picking up Phase 9?** Read the TL;DR + Decisions table
  + the slice you're working on. Skim the rest.
- **Completed a slice?** Update its status (mark done) and any
  decisions you made on the open questions for that slice. Move
  finalized choices into the Decisions table at the top.
- **Starting Phase 10?** Read the "Future integration points"
  section first to see what Phase 9 promised you.
- **Disagreeing with a locked decision?** Flag explicitly in chat,
  decide deliberately, then update this doc rather than just the code.

*Last updated: created during Phase 9 design conversation.*
