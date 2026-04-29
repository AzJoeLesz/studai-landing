# Phase 10 — Authenticated Solution Graphs (the moat)

> **Read this first if you're a new agent session picking up Phase 10.**
> Every decision below was approved by the founder after a long design
> conversation. Don't relitigate without revisiting this doc and
> deciding to override deliberately. Cross-reference: the main
> `README.md` Phase 10 entry is intentionally short and points here.
> Phase 9's locked decisions are in `docs/phase9_personalization.md`
> and Phase 10 builds directly on them — read that doc's "Future
> integration points" section before changing anything here.

---

## TL;DR — what Phase 10 is, in one paragraph

Phase 10 turns StudAI from "a tutor that has private context about
similar problems" into "a tutor that knows the structured solution
path for THIS problem and can check the student's every step against
it". It does this by promoting the existing L3 annotations
(`problem_annotations.payload` JSON) into a relational schema with
multiple named **paths** per problem, ordered **steps** per path,
graduated **hints** per step, and structured **common mistakes** that
trigger pedagogical responses (never the answer). A new pre-LLM
**step evaluator** call classifies the student's latest message
against the active path's expected next step; the result is injected
into the system prompt so the main LLM call can write a perfectly
targeted reply this turn (not next turn). Verified paths drive the
runtime; unverified paths sit waiting in `/admin/paths` for human
spot-check. The investor-facing payoff: when a student types a
problem the system has a verified path for, the tutor visibly stops
asking generic Socratic questions and starts asking step-precise
questions ("you correctly subtracted 3 — what should you do with the
2 in front of x?"). This is the difference between a chatbot with a
prompt and an actual structured math tutor.

---

## Implementation status (current as of Phase 10 design lock-in)

Nothing built yet. Five slices planned:

| Slice | Status | Notes |
|-------|--------|-------|
| 10A — Schema, repo layer, generation script | **Not started** | `sql/007_solution_graphs.sql`, `db/schemas.py` extensions, `db/repositories.py` extensions, `scripts/generate_solution_paths.py`, `prompts/path_gen_v1.txt`, LLM critic pre-filter. Backfills the 205 existing `problem_annotations` rows as path #1 (verified=false). |
| 10B — Step evaluator + GUIDED PATH block + tutor wiring | **Not started** | `agents/step_evaluator.py`, `agents/guided_mode.py`, new system block in `tutor._build_context`, `style_policy.should_suppress_grounding` extended into a struct. **First demo-able slice** for the verified-by-fiat MVP set. |
| 10C — `/admin/paths` verification UI | **Not started** | `app/[locale]/admin/paths/page.tsx`, role gate via new `profiles.role` column, approve/reject/edit flow. Unblocks 10D's quality story. |
| 10D — Mistake handling + step_check writes + dynamic path-switching | **Not started** | `common_mistakes` wiring in evaluator output; `mastery.apply_graded_update` calls with `evidence_source='step_check'`; extractor coordination (mode + struggling_on owned by guided mode); `stuck_offer_alt_path` evaluator signal that triggers the dad-style "want to try this a different way?" intervention. |
| 10E — Curriculum-led generation expansion + eval fixtures + RAG-hit logging | **Not started** | Generate paths for ~300-500 curriculum-curated problems (gsm8k for K-8, openstax/hendrycks Levels 1-2 for 9-12); eval fixture battery in `backend/evals/`; lightweight RAG-hit counter on `problems` for future traffic-based prioritization (when student usage exists). |

**Required follow-up before going live (per slice):**
- 10A → 10E each runs `sql/007_solution_graphs.sql` once at slice 10A's deploy (idempotent, safe to re-run).
- 10B → redeploy backend so the new system block ships.
- 10C → frontend deploy + first admin user gets `profiles.role='admin'` set manually in Supabase.
- 10D → no migration; redeploy backend.
- 10E → no migration; new env knobs documented at the bottom.

---

## Decisions (locked — all approved)

| # | Decision | Rationale |
|---|----------|-----------|
| A | **Backfill the existing 205 `problem_annotations` payloads as path #1 (verified=false)** in the new schema | Free pedagogy that costs only a one-shot mapper script; gives 10A immediate test data even before generation runs at scale. |
| B | **No path versioning yet**; `unique(problem_id, name, language)` with overwrite-bumping `model` column is enough for MVP | Phase 12 ratings will tell us if we need real versioning; until then it's premature complexity. |
| C | **EN-only generation now**; HU at runtime via translation later (deferred) | Same call we made on the corpus; consistent with Phase 9's Hungarian-content-gap acknowledgment. |
| D | **Step evaluator is a BLOCKING pre-LLM call** (~400ms TTFT cost), not parallel and not next-turn-stale | The whole point of Phase 10 is the model knowing where the student is *right now*. One-turn-stale defeats the purpose. 400ms is acceptable; Phase 9's gpt-5-mini reasoning pause already trains users to expect a few seconds before tokens. |
| E | **Per-step `step_check` BKT writes**, full weight (1.0), topic = `problems.type`, fired post-LLM as background tasks | Density of signal (4× more BKT updates), partial credit when student abandons mid-problem, multi-topic problems get richer per-topic credit, BKT's `P_T = 0.10` transit param is *defined* per-attempt. The writes themselves are fire-and-forget so they cost the user zero latency. |
| F | **Activate guided mode at RAG similarity ≥ 0.85** (vs current Phase 9 RAG threshold of 0.55) | Higher precision matters more than recall — a wrong activation on a problem we don't have a real verified path for would be worse than no activation. Tunable via env. |
| G | **Verification UI is a Next.js `/admin/paths` route**, not a CLI script | 3-day investment that pays back within Phase 10 itself (faster verification at ~20s/path vs ~60s in CLI), AND saves ~2 days each on Phase 12 (ratings admin) and Phase 13 (parent-link admin). Net savings: 1-2 weeks across the roadmap. |
| H | **Generate paths with gpt-5-mini** by default (not gpt-4o-mini) | Quality is the focus; cost difference is rounding noise (~$20/500 problems vs ~$4); the structured-JSON-output quality on long generations is meaningfully better. Downgradeable via env if outputs disappoint at scale. |
| I | **Initial generation set: 205 already-annotated + curriculum-led ~300-500** (~10-20 representative problems per topic in `grade_priors.json`, weighted toward gsm8k for K-8 and openstax/hendrycks Levels 1-2 for 9-12) | Without student traffic logs, curriculum-led prioritization is the honest substitute. Total ~500-700 verified paths ≈ ~3-4 hours of focused verification. Realistic for one weekend. |
| J | **Guided mode owns `session_state.mode` and `session_state.struggling_on` while active**; the post-turn extractor skips writing those two fields | Guided mode has authoritative knowledge (mode is definitively `'problem'`; struggling_on is definitively `step.goal`). Without this contract, the extractor would race and clobber better data. The extractor still owns `summary_delta`, `mood_signals`, and `mastery_signals`. |
| K | **Suppress L1 (problem-bank RAG) and L3 (annotations) when guided is active**; keep L2 (OpenStax) | L1 is redundant (we already have THE problem); L3 is redundant (we have the structured path); L2 is still useful for definitions and standard methods. Saves ~3-4k chars of context and removes the conflict between "here's a similar problem's worked solution" and "here's THE expected next step". |
| L | **Path-picker UX is silent for `preferred=true` start, but dynamic-switch on stuck**: model silently picks the `preferred=true` path; if the student gets stuck (≥2 attempts on the same step + invalid/mistake signal), the evaluator emits `stuck_offer_alt_path` and the GUIDED PATH block instructs the model to *offer* an alternative path in its tutor voice — student says yes, system swaps `active_path_id` and resets step counters | This is the dad-as-tutor behavior the founder explicitly wants — "when I struggled, my dad gave me insight on how I might try another way that was more in tune with my thinking." It's surprisingly cheap to add (the path-switch machinery already exists for `off_path_valid`); refusing to ship it would amputate the human-tutor feel from Phase 10. |
| M | **Visible UI signal: a small "guided mode" check-icon + tooltip near the assistant bubble**, no progress bar, no step counter visible to the student | Students seeing "step 2 of 4" would game the system (rush through, watch the bar). The icon justifies why the tutor is suddenly more precise without leaking the path structure. |
| N | **Quality validation without student traffic: LLM-as-judge pre-filter at generation time + eval fixture battery + manual curation** | We don't have hundreds of students yet — traffic-based quality signals don't exist. Compensate via: gpt-5/Claude critic auto-rejects bottom 20% of generated paths before they hit the verification queue; ~30 hand-curated gold-standard fixtures act as a regression test for the generation prompt; you (the human) do final spot-check on the survivors. Once student usage exists (months from now), add usage-derived quality signals on top. |

---

## Architecture overview

```
                     +-----------------+
                     |   chat turn     |
                     +--------+--------+
                              |
       +----------------------+---------------------+
       |                      |                      |
       v                      v                      v
   load history          load profile           build grounding
   (existing)            session_state          context (existing)
                         progress (existing)    + topic_classifier
                              |
                              v
                     +-----------------+
                     | Guided-mode     |
                     |   gate          |
                     +--------+--------+
                              |
                  +-----------+-----------+
                  | Active guided session?|
                  +-----------+-----------+
                  YES |              NO |
                      v                  v
                 load active_path,      check top RAG hit:
                 current_step,          similarity >= 0.85
                 next_step,             AND verified path exists?
                 attempts_on_step             |
                      |                       v
                      v                  +----+----+
              +---------------+          |  YES?   |
              | step          |   +------+         +------+
              | evaluator     |   | YES               NO  |
              | (BLOCKING     |   v                       v
              |  ~400ms       | propose new          no guided
              |  hard cap)    | guided session       just go
              +-------+-------+ + run evaluator
                      |
                      v
              +---------------+
              | bump          |
              | current_step, |
              | attempts,     |
              | hints_used    |
              +-------+-------+
                      |
                      v
              +---------------+
              | format        |
              | GUIDED PATH   |
              | system block  |
              +-------+-------+
                      |
                      v
              +---------------+      +---------------+
              | _build_       +----->| stream LLM    |
              | context       |      | (existing)    |
              +---------------+      +-------+-------+
                                             |
                                             v
                                     +---------------+
                                     | persist reply |
                                     | + answer_guard|
                                     | + extractor   |
                                     | + step_check  |
                                     |   BKT write   |
                                     |  (all FF)     |
                                     +---------------+
```

System-message order in the prompt (top → bottom):

1. Persona (`tutor_v3.txt`)
2. Profile snippet (existing `_format_profile_snippet`)
3. Student progress (Phase 9A/9D)
4. Session state (Phase 9A)
5. Grounding L1 (problem-bank RAG) — **suppressed when guided active OR register is non-default**
6. Grounding L2 (OpenStax) — **kept when guided active; suppressed only on register above/below**
7. Grounding L3 (annotations) — **suppressed when guided active OR register is non-default**
8. **GUIDED PATH block** (NEW — between grounding and directives)
9. STYLE DIRECTIVES + inline recipe (Phase 9; LAST system message — preserves recency)
10. Recent history
11. New user turn

Why GUIDED PATH between grounding and directives: the guided block carries the most action-relevant *substance* (which step, what's expected) but STYLE DIRECTIVES still constrain *how* the model speaks. Directives must remain the last system message — Phase 9 fought hard for that ordering and Phase 10 must not regress it.

---

## Schema sketch (for migration 007)

```sql
-- =============================================================================
-- StudAI migration 007: solution graphs (Phase 10)
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.
--
-- Phase 10 — Authenticated Solution Graphs. See
-- docs/phase10_solution_graphs.md for the full design rationale.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- profiles: role column for /admin gating (Phase 10 + 12 + 13 use this)
-- -----------------------------------------------------------------------------
alter table public.profiles
  add column if not exists role text not null default 'student'
    check (role in ('student','parent','teacher','admin'));

-- -----------------------------------------------------------------------------
-- solution_paths: a named approach to solving one problem
-- -----------------------------------------------------------------------------
create table if not exists public.solution_paths (
  id          uuid primary key default gen_random_uuid(),
  problem_id  uuid not null references public.problems(id) on delete cascade,
  name        text not null,                      -- "factoring", "quadratic_formula"
  rationale   text,                                -- "use when a, b, c are integers"
  preferred   boolean not null default false,     -- the silent-default path
  language    text not null default 'en'
              check (language in ('en','hu')),
  verified    boolean not null default false,
  verified_by uuid references public.profiles(id),
  verified_at timestamptz,
  model       text,                                -- provenance (e.g. 'gpt-5-mini')
  critic_score numeric,                            -- 1-5 from LLM-as-judge pre-filter
  created_at  timestamptz not null default now(),
  unique (problem_id, name, language)
);

create index if not exists solution_paths_problem_idx
  on public.solution_paths (problem_id);

create index if not exists solution_paths_verified_idx
  on public.solution_paths (problem_id, verified) where verified = true;

-- RLS: paths are PUBLIC content (readable by all authenticated users).
-- Inserts/updates happen via service_role from the generation script and
-- the /admin route (which calls a backend endpoint, not direct Supabase
-- writes from the browser).
alter table public.solution_paths enable row level security;

drop policy if exists "solution_paths_select_authenticated" on public.solution_paths;
create policy "solution_paths_select_authenticated"
  on public.solution_paths for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- solution_steps: ordered steps within a path
-- -----------------------------------------------------------------------------
create table if not exists public.solution_steps (
  id              uuid primary key default gen_random_uuid(),
  path_id         uuid not null references public.solution_paths(id) on delete cascade,
  step_index      int  not null check (step_index >= 1),
  goal            text not null,                   -- "isolate x"
  expected_action text,                             -- "subtract 3 from both sides"
  expected_state  text,                             -- "2x = 4" (post-step canonical state)
  is_terminal     boolean not null default false,
  created_at      timestamptz not null default now(),
  unique (path_id, step_index)
);

-- Length caps so the GUIDED PATH system block stays bounded.
alter table public.solution_steps
  drop constraint if exists solution_steps_goal_len_chk;
alter table public.solution_steps
  add constraint solution_steps_goal_len_chk
  check (char_length(goal) between 1 and 500);

alter table public.solution_steps
  drop constraint if exists solution_steps_expected_action_len_chk;
alter table public.solution_steps
  add constraint solution_steps_expected_action_len_chk
  check (expected_action is null or char_length(expected_action) <= 500);

alter table public.solution_steps
  drop constraint if exists solution_steps_expected_state_len_chk;
alter table public.solution_steps
  add constraint solution_steps_expected_state_len_chk
  check (expected_state is null or char_length(expected_state) <= 500);

create index if not exists solution_steps_path_idx
  on public.solution_steps (path_id, step_index);

alter table public.solution_steps enable row level security;

drop policy if exists "solution_steps_select_authenticated" on public.solution_steps;
create policy "solution_steps_select_authenticated"
  on public.solution_steps for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- step_hints: graduated hints per step (1=gentle, 2=stronger, 3=last-before-method)
-- -----------------------------------------------------------------------------
create table if not exists public.step_hints (
  id          uuid primary key default gen_random_uuid(),
  step_id     uuid not null references public.solution_steps(id) on delete cascade,
  hint_index  int not null check (hint_index between 1 and 3),
  body        text not null,
  created_at  timestamptz not null default now(),
  unique (step_id, hint_index)
);

alter table public.step_hints
  drop constraint if exists step_hints_body_len_chk;
alter table public.step_hints
  add constraint step_hints_body_len_chk
  check (char_length(body) between 1 and 600);

alter table public.step_hints enable row level security;

drop policy if exists "step_hints_select_authenticated" on public.step_hints;
create policy "step_hints_select_authenticated"
  on public.step_hints for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- common_mistakes: pedagogically-actionable mistake patterns
-- Either step-scoped (preferred — more actionable) or problem-scoped.
-- -----------------------------------------------------------------------------
create table if not exists public.common_mistakes (
  id                uuid primary key default gen_random_uuid(),
  problem_id        uuid references public.problems(id) on delete cascade,
  step_id           uuid references public.solution_steps(id) on delete cascade,
  pattern           text not null,                 -- "forgot to flip inequality when multiplying by -1"
  detection_hint    text,                           -- a phrase/pattern the evaluator may recognize
  pedagogical_hint  text not null,                  -- the response (NOT the answer)
  remediation_topic text,                           -- Phase 11 link target
  created_at        timestamptz not null default now(),
  check (problem_id is not null or step_id is not null)
);

alter table public.common_mistakes
  drop constraint if exists common_mistakes_pattern_len_chk;
alter table public.common_mistakes
  add constraint common_mistakes_pattern_len_chk
  check (char_length(pattern) between 1 and 400);

alter table public.common_mistakes
  drop constraint if exists common_mistakes_pedagogical_hint_len_chk;
alter table public.common_mistakes
  add constraint common_mistakes_pedagogical_hint_len_chk
  check (char_length(pedagogical_hint) between 1 and 800);

create index if not exists common_mistakes_problem_idx
  on public.common_mistakes (problem_id) where problem_id is not null;

create index if not exists common_mistakes_step_idx
  on public.common_mistakes (step_id) where step_id is not null;

alter table public.common_mistakes enable row level security;

drop policy if exists "common_mistakes_select_authenticated" on public.common_mistakes;
create policy "common_mistakes_select_authenticated"
  on public.common_mistakes for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- guided_problem_sessions: per-(session, problem) runtime state for guided mode
-- -----------------------------------------------------------------------------
create table if not exists public.guided_problem_sessions (
  id                     uuid primary key default gen_random_uuid(),
  session_id             uuid not null
                         references public.tutor_sessions(id) on delete cascade,
  problem_id             uuid not null references public.problems(id),
  active_path_id         uuid references public.solution_paths(id),
  current_step_index     int  not null default 1,
  attempts_on_step       int  not null default 0,
  hints_consumed_on_step int  not null default 0,
  off_path_count         int  not null default 0,
  status                 text not null default 'active'
                         check (status in ('active','completed','abandoned')),
  started_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  unique (session_id, problem_id)
);

create index if not exists guided_problem_sessions_session_idx
  on public.guided_problem_sessions (session_id, status);

alter table public.guided_problem_sessions enable row level security;

-- RLS: own sessions only (join via tutor_sessions.user_id, like session_state)
drop policy if exists "guided_problem_sessions_select_own" on public.guided_problem_sessions;
create policy "guided_problem_sessions_select_own"
  on public.guided_problem_sessions for select
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "guided_problem_sessions_insert_own" on public.guided_problem_sessions;
create policy "guided_problem_sessions_insert_own"
  on public.guided_problem_sessions for insert
  with check (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "guided_problem_sessions_update_own" on public.guided_problem_sessions;
create policy "guided_problem_sessions_update_own"
  on public.guided_problem_sessions for update
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );
```

Final shape gets locked when 10A is implemented; this is the working
sketch. Length caps and indexes may move when we see real data.

---

## Pre-LLM lifecycle: the step evaluator

The single biggest new piece. Lives in `agents/step_evaluator.py`.

### Inputs

- `problem_id` (from `guided_problem_sessions.problem_id`)
- `current_step` (the row at `current_step_index`)
- `next_step` (the row at `current_step_index + 1`, if any)
- `step_hints` for the current step (already loaded)
- `step_common_mistakes` for the current step (already loaded)
- `attempts_on_step`, `hints_consumed_on_step`, `off_path_count` (counters)
- `student_message` (the latest user message)
- `style_directives.step_size` (Phase 9 directive — gates granularity)

### Output (one of)

```
on_path_correct        -- student executed expected_action correctly
on_path_partial        -- student is heading the right way but not done
off_path_valid         -- different valid step (matches an alternative path's step)
off_path_invalid       -- wrong direction
matched_mistake_<id>   -- matches a common_mistake row
stuck_offer_alt_path   -- attempts >= 2 + invalid/mistake AND alt verified path exists
no_step_yet            -- student is asking a question or just chatting
```

Plus an optional `confidence` float and an optional `evaluator_notes`
short string for the prompt block.

### Prompt sketch

The evaluator is a single-purpose LLM call, similar to `answer_judge.py`:

```
You are a step evaluator for a math tutoring system. Given the
problem, the expected next step, and the student's latest message,
classify the student's move into ONE category. Output strict JSON:
  {"signal": "<category>", "confidence": 0.0-1.0,
   "matched_mistake_id": "<uuid or null>",
   "notes": "<short explanation, <100 chars>"}

Categories: <list above>

Examples:
  PROBLEM: solve 2x + 3 = 7 for x
  EXPECTED ACTION: subtract 3 from both sides
  STUDENT: "I think I should subtract 3 from both sides to get 2x = 4"
  -> {"signal":"on_path_correct","confidence":0.95,
      "matched_mistake_id":null,"notes":"correctly stated and computed"}

  STUDENT: "Let me try dividing by 2 first"
  -> {"signal":"off_path_invalid","confidence":0.85,
      "matched_mistake_id":null,
      "notes":"divided before isolating; no /3 known yet"}
  
  STUDENT: "subtract 3 from both sides... 2x = 5"
  -> (matches mistake "off-by-one in subtraction")
```

### Latency, cost, fallbacks

- **Model:** `step_evaluator_model` config knob, default `gpt-4o-mini`. Same precedent as `placement_judge_model`.
- **Hard timeout:** 600ms. On timeout → return `{"signal": "no_step_yet"}` and the main turn proceeds without the evaluator's signal (graceful degradation).
- **Caching:** key = `(step_id, sha256(student_message[:1000]))`. Same retry → free.
- **Skip:** evaluator is bypassed entirely on the FIRST turn of a new guided session (Phase 9's PHASE 1 diagnostic owns turn 1; the student has nothing to evaluate yet) and when `register != at_level/remedial`.
- **Cost:** ~$0.0003/turn at gpt-4o-mini.

---

## The GUIDED PATH system block

Format and content. Slots between grounding and STYLE DIRECTIVES.

```
GUIDED PROBLEM PATH (private — strict guidance, do not recite to student):
problem_id: <uuid>
path: factoring
rationale: use when a, b, c are integers
total_steps: 4
current_step: 2

CURRENT STEP:
  goal: isolate x by subtracting 3 from both sides
  expected_action: subtract 3 from both sides
  expected_state_after: 2x = 4

EVALUATOR SIGNAL (this turn): on_path_partial
attempts_on_this_step: 2
hints_consumed: 1

NEXT STEP (only after current passes):
  goal: divide both sides by 2
  expected_state: x = 2

[ALTERNATIVE PATHS AVAILABLE — only printed when stuck_offer_alt_path fires]
  - quadratic_formula (rationale: "use when the equation doesn't factor nicely")

INSTRUCTIONS FOR THIS REPLY:
  - Acknowledge specifically what the student just did right.
  - Ask ONE Socratic question that nudges them from
    (current state) to (expected_state_after).
  - Do not reveal expected_state_after; do not give the numerical answer.
  - Use STYLE DIRECTIVES.step_size to decide question granularity:
      * micro: ask about HALF the expected_action
      * normal: one transformation per turn
      * leap-allowed: combined-step nudges OK
  - If the EVALUATOR SIGNAL is matched_mistake_<id>, use the row's
    pedagogical_hint verbatim in spirit (rephrase, don't recite).
  - If the EVALUATOR SIGNAL is stuck_offer_alt_path, you may offer the
    alternative path in your tutor's voice. Suggested phrasing:
      "Hmm, this approach is feeling tricky — want to try it a
       different way? We could use [alternative path name] instead,
       which works nicely when [alternative path rationale]."
    Do not force the switch; let the student choose. If they say yes,
    next turn the system will swap active_path_id and reset.
```

Length budget: ~600 tokens worst-case. Acceptable on top of Phase 9's
~22-25k char system stack.

---

## Phase 9 integration — the seams

### Style directives interaction

| Directive | Phase 10 behavior |
|-----------|-------------------|
| `step_size: micro` | Evaluator accepts "half of expected_action" as `on_path_partial`; GUIDED PATH block instructs micro-grained questioning. |
| `step_size: normal` | Evaluator's default mode. One transformation per turn. |
| `step_size: leap-allowed` | Evaluator accepts "skipped expected_action and went straight to next step's expected_state" as `on_path_correct`; bumps `current_step_index` by 2. |
| `register: at_level` | Guided mode runs normally. |
| `register: remedial` | Guided mode runs (the student SHOULD be learning this — slow down, more hints). |
| `register: above_level_exploration` | Guided mode is **completely off**, even if a verified path exists. The student asked about the topic curiously; we don't surprise them with a problem. |
| `register: below_level_warmup` | Guided mode off; topic is review for them. |
| `affect: anxious-needs-reassurance` | Phase 9 already shifts `step_size` down and `hint_timing` earlier — Phase 10 inherits these for free. Anxious students get more granular checks and earlier hints automatically. |
| `vocabulary_level: concrete-everyday` | Phase 9's inline recipe already bans letter variables; the GUIDED PATH block's `expected_state` may contain notation (e.g. `2x = 4`), and the LLM must restate in plain English when speaking to the student. The block is private — never recited. |

### Mode + struggling_on coordination (decision J)

Concrete contract:

- `state_updater._call_extractor` continues to run after every turn.
- Before `state_updater` upserts to `session_state`, it checks:
  ```python
  guided = repo.get_active_guided_session(session_id)
  if guided is not None:
      update.mode = None              # don't overwrite
      update.struggling_on = None     # don't overwrite
  ```
- The guided system writes those two fields directly via a new
  `repo.upsert_session_state(... mode='problem', struggling_on=step.goal)`
  call inside `agents/guided_mode.py::activate_or_resume`.
- `summary_delta`, `mood_signals`, `mastery_signals` continue to flow
  from the extractor as today.

This keeps the two background workers from racing.

### `evidence_source = 'step_check'` writes (decision E)

Concrete contract for per-step BKT signal:

- After every turn where the evaluator returns `on_path_correct`:
  ```python
  asyncio.create_task(
      asyncio.to_thread(
          mastery.apply_graded_update,
          user_id=user_id,
          topic=problem.type,                # canonical free-text for now (Phase 11 → FK)
          correct=True,
          difficulty=problem.difficulty,     # uses _difficulty_to_b mapping
          evidence_source='step_check',      # full BKT weight (1.0)
      )
  )
  ```
- `on_path_partial`: no write yet (the step isn't done). When it
  flips to `on_path_correct` next turn, that's when we credit.
- `off_path_invalid` after `attempts_on_step >= 2`: write a `correct=False`
  step_check (the student demonstrably didn't get this step).
- `matched_mistake_<id>`: write a `correct=False` step_check AND
  optionally bump `student_progress` for `mistake.remediation_topic` if
  Phase 11 has provided one.

Topic resolution: `problems.type` is the free-text bucket for Phase 9.
When Phase 11 lands the canonical `topics` table, we'll add a join
column and migrate. The `evidence_source='step_check'` rows become
the highest-quality data we have.

### RAG suppression (decision K)

`style_policy.should_suppress_grounding(directives)` is too coarse for
Phase 10's needs. Refactor into:

```python
@dataclass(frozen=True)
class GroundingSuppression:
    suppress_l1_problem_rag: bool
    suppress_l2_openstax: bool
    suppress_l3_annotations: bool
    suppress_guided_path: bool   # new

def grounding_suppression(
    directives: StyleDirectives,
    *,
    guided_active: bool,
) -> GroundingSuppression:
    if directives.register in ("above_level_exploration", "below_level_warmup"):
        return GroundingSuppression(True, True, True, True)  # suppress everything including guided
    if guided_active:
        return GroundingSuppression(True, False, True, False)  # keep L2 + guided
    return GroundingSuppression(False, False, False, False)  # keep all
```

The old boolean stays as a thin wrapper for back-compat:

```python
def should_suppress_grounding(directives: StyleDirectives) -> bool:
    return grounding_suppression(directives, guided_active=False).suppress_l1_problem_rag
```

### Topic classifier (latency win)

When `guided_problem_sessions` is active for the session, we already
know the canonical topic (`problems.type`). Skip the `classify_topic`
call entirely — saves ~30ms + one embedding call.

When not active, classifier runs as today (we still need the live
register decision before guided mode could activate).

---

## Generation pipeline

### `scripts/generate_solution_paths.py`

Same shape as `scripts/annotate_problems.py`:

- **Input source** (priority order):
  1. The 205 problems with existing `problem_annotations` rows. Use
     the existing JSON payload as additional context to the prompt:
     "here's an L3 annotation already done, use it as scaffolding."
  2. Curriculum-led ~300-500 problems: enumerate
     `grade_priors.json`'s topics, pick 10-20 representative problems
     per topic from the right corpus subset (gsm8k for K-8 topics,
     openstax/hendrycks Levels 1-2 for 9-12).
- **Prompt:** new `prompts/path_gen_v1.txt` — outputs strict JSON:
  ```json
  {
    "paths": [
      {
        "name": "factoring",
        "rationale": "use when ...",
        "preferred": true,
        "steps": [
          {
            "goal": "isolate x",
            "expected_action": "subtract 3 from both sides",
            "expected_state": "2x = 4",
            "hints": ["gentle ...", "stronger ...", "last hint ..."],
            "common_mistakes": [
              {
                "pattern": "...",
                "detection_hint": "...",
                "pedagogical_hint": "...",
                "remediation_topic": "..."
              }
            ]
          }
        ]
      }
    ]
  }
  ```
- **Validation:** Pydantic. Retry once on bad JSON. On second failure,
  log + skip + continue.
- **Concurrency:** 2-3, same as annotation. Mind the 60s OpenAI timeout.
- **Idempotency:** `unique(problem_id, name, language)` allows
  re-runs to overwrite paths with the same name + bumped `model` column.
- **Cost:** ~$0.02/problem at gpt-5-mini → ~$10-20 for the 500-700 set.

### LLM-as-judge pre-filter (decision N)

Right after generation, before insertion:

- New `scripts/critique_paths.py` (or a stage inside the generator).
- Send each generated path to a stronger model (default `gpt-5` or
  Claude — TBD when we test, env knob `path_critic_model`).
- Critic prompt: rate each path on (correctness 1-5, hint quality
  1-5, mistake plausibility 1-5, step granularity 1-5). Output one
  composite `critic_score` between 1-5.
- **Auto-reject** paths with `critic_score < 2.5`. Their rows still
  get inserted (audit trail) but with `verified = false` and a
  separate `critic_rejected = true` column? Or just skip insert
  entirely? — **Defer this micro-decision to 10A; lean toward "insert
  with critic_score, leave verified=false, /admin can still surface
  them for human override".**
- Cost: ~$0.01/path → ~$5-10 for the 500-700 set. Worth it.

### Cross-model agreement (deferred to 10E)

Generate paths with two different models for the same problem; if
they substantially agree on step structure, mark as *higher-confidence*
in the verification queue (sort to the top of `/admin`). Disagreements
are exactly what's worth your spot-check. ~2× generation cost, so
deferred until we have empirical signal that single-model generation
is a meaningful quality risk.

---

## Verification UX — `/admin/paths`

### Route + auth

- `app/[locale]/admin/paths/page.tsx`
- Server-side check: load profile, redirect if `role != 'admin'`.
- Backend endpoint set: `/admin/paths/list`, `/admin/paths/{id}/verify`,
  `/admin/paths/{id}/reject`. Service-role writes; the frontend never
  hits Supabase directly for these.

### MVP UI

```
+-------------------------------------------------------+
| StudAI Admin / Paths                                  |
| Filter: [unverified ▼] Source: [all ▼] Sort: [newest ▼] |
+-------------------------------------------------------+
| 23 unverified paths                                   |
+----------------------------+--------------------------+
| Problem (left half)        | Path (right half)        |
|                            | name: factoring          |
| Solve x^2 - 5x + 6 = 0     | rationale: ...           |
| for x.                     | critic_score: 4.2/5      |
|                            |                          |
| (worked solution toggle)   | step 1: identify a, b, c |
|                            |   action: ...            |
|                            |   state: ...             |
|                            |   hints (3)              |
|                            |   mistakes (2)           |
|                            |                          |
|                            | step 2: factor           |
|                            |   ...                    |
+----------------------------+--------------------------+
|                                                       |
| [Approve & next] [Reject & next] [Skip]               |
+-------------------------------------------------------+
```

No edit in the MVP. Approve sets `verified=true, verified_by, verified_at`.
Reject sets `verified=false` and increments a `rejection_count` column?
— **Defer; lean toward "rejected = soft-delete"**, the row stays for audit.

Edit comes after first 100 verifications when we know what people
actually want to fix in-flight.

### Throughput target

20 seconds per decision = 180/hour. The 500-700 set is ~3-4 hours of
focused work. One weekend.

---

## Mistake handling + dynamic path-switching (10D)

### Common mistakes wiring

- The evaluator's `matched_mistake_<id>` output points at a specific
  `common_mistakes` row.
- The GUIDED PATH block then includes:
  ```
  EVALUATOR SIGNAL: matched_mistake_<id>
  MISTAKE PATTERN: <pattern>
  PEDAGOGICAL HINT (use in spirit, rephrase don't recite):
    <pedagogical_hint>
  ```
- The model writes a Socratic reply that follows the pedagogical
  hint without copying it word-for-word.
- We log the match for analytics: which mistakes are most common per
  problem? This drives generation prompt improvements.

### Dynamic path-switching (the dad-as-tutor feature, decision L)

Trigger: `attempts_on_step >= 2` AND
`evaluator_signal in (off_path_invalid, matched_mistake_*)`
AND there's a verified alternative path on the same problem.

When triggered, the evaluator emits `stuck_offer_alt_path` and the
GUIDED PATH block adds the `ALTERNATIVE PATHS AVAILABLE` section
plus the offer-phrasing instruction.

Student response detection (next turn):

- If the next user message reads as "yes, let's try that" / "ok" /
  "igen" / "rendben", swap `active_path_id` to the offered alt,
  reset `current_step_index = 1`, reset `attempts_on_step` and
  `hints_consumed_on_step` to 0, increment `off_path_count`.
- If "no, I want to keep trying" / "nem", don't swap; let them
  continue. We may quietly bump `hint_timing` earlier in the
  directives for the next 2 turns to give them more help on the
  current path.
- Detection: a one-shot small LLM call (could reuse the evaluator
  with a different prompt mode) — too brittle for keyword matching
  alone given multilingual.

This is the key human-tutor moment. The founder's framing (his dad
giving alternative-approach insight when stuck) goes into the prompt
generator's training material so future iterations of `path_gen_v1.txt`
can produce alternative paths with *complementary* approaches, not
just textbook variants.

---

## Slices and shipping order

Each slice is independently shippable.

### 10A — Schema + repo + generation pipeline (~5 days)

- `sql/007_solution_graphs.sql` (idempotent, RLS, including new `profiles.role`).
- `db/schemas.py`: new Pydantic models `SolutionPath`, `SolutionStep`,
  `StepHint`, `CommonMistake`, `GuidedProblemSession`.
- `db/repositories.py`: CRUD for all new tables plus
  `get_active_guided_session(session_id)`,
  `start_guided_session(...)`,
  `advance_guided_session(...)`,
  `list_unverified_paths(...)`,
  `verify_path(...)`,
  `get_paths_for_problem(problem_id, *, language='en', verified_only=True)`,
  `get_steps_for_path(path_id)`,
  `get_hints_for_step(step_id)`,
  `get_mistakes_for_step(step_id)`,
  `get_mistakes_for_problem(problem_id)`.
- `prompts/path_gen_v1.txt` — strict JSON output spec.
- `scripts/generate_solution_paths.py` — generator with optional
  `--from-existing-annotations` flag for the 205 backfill set,
  `--curriculum-led` for the topic-balanced ~300-500 set,
  `--limit` for batch control.
- LLM-as-judge pre-filter (in-line stage in the generator).
- Backfill mapper: read `problem_annotations.payload`, write a single
  `solution_paths` row (`name='from_annotation'`, `verified=false`)
  with one step per `solution_outline` entry, hints from `hint_ladder`,
  mistakes from `common_mistakes`.
- New env knobs: `path_gen_model` (default `gpt-5-mini`),
  `path_critic_model` (default `gpt-5`),
  `step_evaluator_model` (default `gpt-4o-mini`),
  `step_evaluator_timeout_ms` (default `600`).
- **Demo difference:** none yet. Internal foundation.

### 10B — Step evaluator + GUIDED PATH block + tutor wiring (~5-7 days)

- `agents/step_evaluator.py` — pure LLM call, JSON output, hard timeout, cache.
- `agents/guided_mode.py` — orchestration:
  - `should_activate(top_rag_hit, similarity)` → bool
  - `activate(session_id, problem_id, paths)` → creates row
  - `evaluate_and_advance(session_id, student_message)` → state machine
  - `format_guided_path_block(...)` → system message
- Extend `agents/tutor.py::_build_context`:
  - load active guided session (parallel with profile/state/progress)
  - if active, run evaluator BEFORE main LLM (blocking, 600ms cap)
  - inject GUIDED PATH block in correct position (between L3 and directives)
  - skip topic classifier when guided active
- Refactor `style_policy.should_suppress_grounding` into
  `grounding_suppression(directives, *, guided_active) -> GroundingSuppression`.
- v3 prompt update: add a "GUIDED PROBLEM PATH" entry to the
  "PRIVATE CONTEXT BLOCKS YOU MAY RECEIVE" section.
- Frontend: small "guided mode" check-icon + tooltip on assistant
  messages where the system header says guided was active. New i18n
  keys `chat.guidedMode`, `chat.guidedModeTooltip` in `messages/{en,hu}.json`.
  - Surface guided-active flag from backend: extend SSE to emit a
    one-shot `event: guided_active` frame at turn start, OR poll session
    metadata. **Lean toward the SSE event** — already on the wire.
- **Demo difference:** for verified problems, the tutor visibly stops
  asking generic questions and starts asking step-precise questions.
  Internal-only at first since few problems are verified.

### 10C — `/admin/paths` verification UI (~3-5 days)

- `app/[locale]/admin/paths/page.tsx` — list view + detail view.
- Backend endpoints: `/admin/paths/list`, `/admin/paths/{id}/verify`,
  `/admin/paths/{id}/reject`.
- Role gate: `profiles.role == 'admin'` (default 'student'); first
  admin user gets set manually in Supabase.
- shadcn UI components reused for layout (`Card`, `Button`, `Tabs`).
- Mobile-friendly is NOT a requirement here (admin = desktop work).
- **Demo difference:** none for students, but unblocks the throughput
  for 10D's quality story.

### 10D — Mistake handling + step_check writes + dynamic path-switching (~4-5 days)

- Wire `common_mistakes` into the evaluator output: add
  `matched_mistake_id` field to evaluator JSON, add MISTAKE block to
  GUIDED PATH formatter.
- `mastery.apply_graded_update(... evidence_source='step_check')`
  background-task call from `tutor.run_tutor_turn` after the stream
  closes, conditional on the evaluator's signal.
- Coordination patch in `state_updater`: skip writing `mode` and
  `struggling_on` when guided active.
- `stuck_offer_alt_path` evaluator signal: add detection logic
  (counters + alt-path existence check), add "ALTERNATIVE PATHS
  AVAILABLE" section to the GUIDED PATH block, add the offer-phrasing
  instruction.
- Path-switch detection on next turn: small LLM call (yes/no
  classifier on student response), reuse evaluator infrastructure.
- **Demo difference:** student makes a textbook mistake → tutor
  catches it specifically (not generically); student gets stuck →
  tutor offers an alternative approach in the dad-as-tutor voice;
  mastery numbers move on real per-step signal.

### 10E — Curriculum-led generation + eval fixtures + RAG-hit logging (~3-5 days)

- Generate paths for the curriculum-led ~300-500 set (generator already
  exists from 10A; this is the bulk content run + verification time).
- Eval fixture battery in `backend/evals/`:
  - ~30 hand-curated (problem, step, student_message) triples with
    known correct evaluator labels — regression tests for the evaluator.
  - ~10 (problem, expected_path_skeleton) pairs — regression tests
    for the generator (when prompt changes, did quality slip?).
- Lightweight RAG-hit logging: add `problems.rag_hit_count int default 0`
  + a `bump_rag_hit_count(problem_id)` repo call from
  `agents/retrieval.find_relevant_problems`. Lets us observe future
  traffic and prioritize generation runs accordingly.
- Cross-model agreement option (deferred from 10A): `--critic-model`
  flag for `generate_solution_paths.py` that runs a second model on
  the same problem and surfaces disagreements in the verification queue.
- **Demo difference:** subtle. Quality-of-life and ongoing-work tooling.

**Total Phase 10 effort:** ~3-4 weeks for 10A-10D; +1 week for 10E.

---

## Deferred items — and when they come back

| Deferred | Comes back in | Why deferred |
|----------|---------------|--------------|
| Path versioning (multiple `model` versions of `factoring` for the same problem) | After Phase 12 has 100+ ratings per path | Premature complexity; unique-on-(problem, name, language) + overwrite-with-bumped-model is enough for now. |
| Hungarian translations of paths/hints/mistakes | When HU usage signals demand | Same call as the corpus; consistent with Phase 9's deferred Hungarian content gap. |
| Path edit in `/admin/paths` (vs only approve/reject) | After first ~100 verifications | We don't yet know what kinds of in-flight fixes are common. |
| Student-facing path picker (vs silent `preferred=true`) | Possibly never; revisit when telemetry shows the dad-style auto-switch isn't catching enough cases | The auto-switch on stuck handles the main use case more naturally. |
| Whiteboard `expected_state_drawing` column for tldraw JSON | **Phase 16** | Phase 16 will add the column; the schema leaves room. |
| Continuous IRT calibration (per-step `b` value fit from outcome data) | After Phase 12 has 1k+ step_check signals per topic | `_difficulty_to_b` mapping is enough until we have real outcome data. |
| Cross-model generation agreement | 10E or later | Defer until single-model generation is empirically problematic. |
| LLM-as-judge during the runtime (post-reply quality check) | **Phase 12** | Phase 12 owns post-reply quality measurement. Phase 10's critic is generation-time only. |
| Auto-generation of hints in HU at runtime via translation | When HU usage demands | Same deferred call. |

---

## Future integration points (so Phases 11+ don't surprise us)

This section is the contract: any phase below WILL read or write the
fields named here, so we can't quietly rename them.

- **Phase 11 (lesson mode):**
  - reads `solution_paths` to surface "verified problems for this lesson"
  - canonical `topics` table arrives — `common_mistakes.remediation_topic`
    and `solution_paths.problem_id → problems.type` get FK migrations
  - `session_state.mode = 'lesson'` becomes a new gate alongside guided mode
- **Phase 12 (quality loop):**
  - new ratings table will FK to `guided_problem_sessions.id` AND
    `solution_paths.id` so we can compute per-path 👍/👎 rates
  - A/B testing framework will gate `solution_paths` selection on
    `(problem_id, name)` — versioning becomes interesting here
  - low-rated paths surface in `/admin/paths` for re-generation
- **Phase 13 (parent view):**
  - reads aggregated `student_progress` rows where `evidence_source = 'step_check'`
    — these are the cleanest mastery signals to show parents
  - reads `guided_problem_sessions` history for "what problems was
    she working on this week?" view
  - reuses `/admin` chrome (sidebar, role guard)
- **Phase 14 (voice):**
  - step evaluator runs on text — voice flow is STT → text → existing
    chat brain → existing evaluator. Unchanged.
  - hints stored as `body` text → TTS at delivery. No schema impact.
- **Phase 15 (emotion):**
  - `affect = anxious-needs-reassurance` already shifts `step_size`
    and `hint_timing` (Phase 9). Phase 10 inherits free.
- **Phase 16 (whiteboard):**
  - new column `solution_steps.expected_state_drawing` (TLDraw JSON)
    for "let me sketch the expected next step"
  - `step_hints` may gain a parallel `body_drawing` column

---

## Risks / things to watch

- **Path mismatch from RAG.** Top RAG hit at similarity 0.85 might
  not be the *exact same problem* the student typed. Higher threshold
  helps but doesn't eliminate. Mitigation: log every guided activation
  with (student_message, matched_problem_id, similarity) and review
  weekly during 10B/10D ramp.
- **Step granularity drift.** Different generation runs may segment
  "subtract 3 then divide by 2" into 1 vs 2 steps. Lock down with a
  style guide in `path_gen_v1.txt`: "one transformation per step;
  arithmetic operations on each side of an equation = one step;
  algebraic identities = one step". The LLM critic should flag
  granularity violations.
- **Evaluator confusion.** Long student messages with multiple steps
  in one go may confuse the evaluator. Mitigation: evaluator prompt
  explicitly says "the student may have done MULTIPLE steps; identify
  the LATEST step and classify that one"; if student is way ahead,
  evaluator returns `on_path_correct` and we bump
  `current_step_index` by however many were jumped.
- **Verification queue throughput.** If you can't keep up with
  generation, unverified content sits idle (not used by the runtime
  per the verified gate). Don't generate beyond your verification
  bandwidth. Default to "generate 100, verify, generate next 100".
- **Gaming.** Student copy-pastes the canonical answer from somewhere.
  Mitigation: detect "instant final answer with no work shown" pattern
  in the evaluator (`signal: on_path_correct + step_index jumped to
  terminal`); when detected, the tutor's reply asks the student to
  walk through their reasoning before crediting. Subtle; doesn't
  accuse.
- **Memory/context bloat.** GUIDED PATH block adds ~600 tokens worst
  case. Phase 9 was already at ~22-25k chars system stack. Total
  with guided ~24-28k chars on gpt-5-mini's 128k window — plenty of
  headroom but worth watching as Phase 11 lesson context lands.
- **Eval the evaluator.** Bad evaluator → bad GUIDED PATH signal →
  bad reply. Build the 10E fixture battery early and run it on
  every prompt change.
- **Latency tail.** 600ms hard timeout = the 95th-percentile latency
  hit is bounded, but a slow OpenAI day will still feel sluggish to
  the student. Mitigation: the `_with_heartbeat` SSE wrapper from
  Phase 9 keeps the connection alive during the wait so the user
  sees the typing indicator the whole time.

---

## Open questions (decide when you reach the relevant slice)

- During 10A: should the LLM critic auto-reject (skip insert) or
  always-insert with `critic_score`? **Lean: always-insert**, let
  `/admin` triage. Easier audit trail.
- During 10B: should the GUIDED PATH block include the `WORKED SOLUTION`
  text from `problems.solution_en` for the model to verify against,
  or is "expected_state per step" enough? **Lean: per-step is enough**;
  worked solution is redundant once the path is structured. Reduces
  context size.
- During 10C: edit-in-place in `/admin/paths`, or just approve/reject
  in MVP? **Lean: approve/reject only**. Edit comes after first ~100
  verifications when we know the friction.
- During 10D: when student gets `matched_mistake` but the mistake row
  has no `pedagogical_hint` (LLM generated junk for that field), fall
  back to a generic "I see an error in your work" prompt or skip the
  mistake signal? **Lean: skip**; generic mistake messages are anti-pedagogy.
- During 10D: how to detect "yes, switch paths" reliably across
  EN/HU/short replies? Single LLM call with strict YES/NO output, or
  embed-and-classify? **Lean: small LLM call**, same pattern as
  `placement_judge`.
- During 10E: when generating the curriculum-led set, do we need a
  HUMAN to pick the 10-20 representative problems per topic, or can
  we automate selection (e.g. pick by difficulty distribution)?
  **Lean: automate first**, human reviews via `/admin`. Cheap to redo.

---

## Citations to keep

- Corbett, A. T., & Anderson, J. R. (1994). *Knowledge tracing.*
  (Same as Phase 9; Phase 10 generates the per-step BKT signal.)
- Pardos, Z. A., & Heffernan, N. T. (2011). *KT-IDEM.* (Same; Phase
  10 supplies the difficulty-modulated step_check evidence.)
- Anderson, J. R., Corbett, A. T., Koedinger, K. R., & Pelletier, R.
  (1995). *Cognitive tutors: Lessons learned.* — model-tracing
  paradigm, foundational for the per-step evaluator design.
- VanLehn, K. (2006). *The behavior of tutoring systems.* —
  step-loop vs. task-loop tutoring; Phase 10 is the step-loop
  realization for StudAI.
- Heffernan, N. T., & Heffernan, C. L. (2014). *The ASSISTments
  ecosystem.* — at-scale evidence that per-step pedagogy + mastery
  tracking outperforms problem-only tutoring.

---

## How this doc is meant to be used

- **New session picking up Phase 10?** Read TL;DR + Decisions table +
  the slice you're working on. Skim the rest. Then check
  `docs/phase9_personalization.md` "Future integration points" for
  Phase 9's contract with you.
- **Completed a slice?** Update its status (mark done) and any
  decisions you made on the open questions for that slice. Move
  finalized choices into the Decisions table at the top.
- **Starting Phase 11?** Read "Future integration points" first to
  see what Phase 10 promised you, especially the canonical-topics
  migration path.
- **Disagreeing with a locked decision?** Flag explicitly in chat,
  decide deliberately, then update this doc rather than just the
  code.
- **Iteration logs after first-pass build:** follow the Phase 9 doc
  pattern — add an "Onboarding iteration #N" section per round of
  fixes after testing. Keep the source-of-truth top-section
  authoritative; iteration logs accumulate at the bottom.

---

*Last updated: Phase 10 design lock-in (initial commit). No code
written yet; all decisions are pre-implementation.*
