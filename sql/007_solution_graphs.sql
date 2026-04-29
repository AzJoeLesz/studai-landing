-- =============================================================================
-- StudAI migration 007: solution graphs (Phase 10)
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.
--
-- Phase 10 — Authenticated Solution Graphs (the moat). See
-- docs/phase10_solution_graphs.md for the full design rationale and the
-- locked decisions table. Every column / constraint / RLS policy here
-- traces back to a numbered decision in that doc; do not change without
-- updating the doc first.
--
-- What this sets up:
--   * `public.profiles.role`         — gate for the new /admin/paths route
--                                       (and Phases 12 + 13's admin views).
--                                       Default 'student' so existing rows
--                                       are unaffected.
--   * `public.solution_paths`        — 1-3 named approaches per problem
--                                       (factoring, quadratic_formula, ...).
--                                       Verified=true is the runtime gate.
--   * `public.solution_steps`        — ordered steps inside a path with
--                                       expected action + canonical
--                                       post-step state.
--   * `public.step_hints`            — graduated hints per step
--                                       (1=gentle, 2=stronger, 3=last).
--   * `public.common_mistakes`       — pedagogically-actionable mistake
--                                       patterns (per-step preferred,
--                                       per-problem fallback).
--   * `public.guided_problem_sessions` — per-(session, problem) runtime
--                                       state for the guided-mode loop.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- profiles.role — admin gating for /admin/paths (Phase 10) and /admin/* (12+13)
-- -----------------------------------------------------------------------------
-- Decision G in docs/phase10_solution_graphs.md: verification UI is a Next.js
-- /admin route, not a CLI script. We need a server-side role gate; the
-- column lives on profiles so RLS join-checks stay simple.
alter table public.profiles
  add column if not exists role text not null default 'student'
    check (role in ('student','parent','teacher','admin'));

-- -----------------------------------------------------------------------------
-- solution_paths — a named approach to solving one problem
-- -----------------------------------------------------------------------------
-- Decision A: backfill the 205 existing problem_annotations rows as
-- path #1 with verified=false. Decision B: no path versioning yet —
-- unique-on-(problem, name, language) and overwrite with bumped `model`.
-- Decision H: gpt-5-mini default for generation; Decision N: critic_score
-- comes from the LLM-as-judge pre-filter.
create table if not exists public.solution_paths (
  id           uuid primary key default gen_random_uuid(),
  problem_id   uuid not null references public.problems(id) on delete cascade,
  name         text not null,                     -- "factoring", "quadratic_formula"
  rationale    text,                               -- "use when a, b, c are integers"
  preferred    boolean not null default false,    -- the silent-default path (Decision L)
  language     text not null default 'en'
               check (language in ('en','hu')),
  verified     boolean not null default false,
  verified_by  uuid references public.profiles(id),
  verified_at  timestamptz,
  model        text,                               -- provenance ('gpt-5-mini', etc)
  critic_score numeric,                            -- 1-5 from LLM-as-judge (Decision N)
  source       text,                               -- 'generator' | 'annotation_backfill'
  created_at   timestamptz not null default now(),
  unique (problem_id, name, language)
);

alter table public.solution_paths
  drop constraint if exists solution_paths_name_len_chk;
alter table public.solution_paths
  add constraint solution_paths_name_len_chk
  check (char_length(name) between 1 and 80);

alter table public.solution_paths
  drop constraint if exists solution_paths_rationale_len_chk;
alter table public.solution_paths
  add constraint solution_paths_rationale_len_chk
  check (rationale is null or char_length(rationale) <= 500);

alter table public.solution_paths
  drop constraint if exists solution_paths_critic_score_chk;
alter table public.solution_paths
  add constraint solution_paths_critic_score_chk
  check (critic_score is null or (critic_score >= 0 and critic_score <= 5));

create index if not exists solution_paths_problem_idx
  on public.solution_paths (problem_id);

-- Partial index for the runtime gate query: "verified paths for this problem".
create index if not exists solution_paths_verified_idx
  on public.solution_paths (problem_id) where verified = true;

-- Paths are PUBLIC content (read by all authenticated users); writes happen
-- via service_role from the generation script and the /admin endpoints.
alter table public.solution_paths enable row level security;

drop policy if exists "solution_paths_select_authenticated"
  on public.solution_paths;
create policy "solution_paths_select_authenticated"
  on public.solution_paths for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- solution_steps — ordered steps inside a path
-- -----------------------------------------------------------------------------
-- The step evaluator (10B) reads `goal`, `expected_action`, and
-- `expected_state` to classify the student's latest message. Length caps
-- keep the GUIDED PATH system block bounded.
create table if not exists public.solution_steps (
  id              uuid primary key default gen_random_uuid(),
  path_id         uuid not null references public.solution_paths(id) on delete cascade,
  step_index      int  not null check (step_index >= 1),
  goal            text not null,                   -- "isolate x"
  expected_action text,                             -- "subtract 3 from both sides"
  expected_state  text,                             -- "2x = 4" (post-step canonical)
  is_terminal     boolean not null default false,
  created_at      timestamptz not null default now(),
  unique (path_id, step_index)
);

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

drop policy if exists "solution_steps_select_authenticated"
  on public.solution_steps;
create policy "solution_steps_select_authenticated"
  on public.solution_steps for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- step_hints — graduated hints per step
-- -----------------------------------------------------------------------------
-- 1=gentle nudge, 2=stronger hint, 3=last hint before the method.
-- Mirrors annotation_v1.txt's hint_ladder shape but per-step.
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

create index if not exists step_hints_step_idx
  on public.step_hints (step_id, hint_index);

alter table public.step_hints enable row level security;

drop policy if exists "step_hints_select_authenticated"
  on public.step_hints;
create policy "step_hints_select_authenticated"
  on public.step_hints for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- common_mistakes — pedagogically-actionable mistake patterns
-- -----------------------------------------------------------------------------
-- Either step-scoped (preferred — more actionable) or problem-scoped.
-- The step evaluator's `matched_mistake_<id>` output points at one of
-- these rows; the GUIDED PATH block surfaces `pedagogical_hint` to the
-- model (used in spirit, never recited).
create table if not exists public.common_mistakes (
  id                uuid primary key default gen_random_uuid(),
  problem_id        uuid references public.problems(id) on delete cascade,
  step_id           uuid references public.solution_steps(id) on delete cascade,
  pattern           text not null,                 -- "forgot to flip inequality when x -1"
  detection_hint    text,                           -- a phrase the evaluator may key on
  pedagogical_hint  text not null,                  -- the response (NOT the answer)
  remediation_topic text,                           -- Phase 11 link target (free text for now)
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

alter table public.common_mistakes
  drop constraint if exists common_mistakes_detection_hint_len_chk;
alter table public.common_mistakes
  add constraint common_mistakes_detection_hint_len_chk
  check (detection_hint is null or char_length(detection_hint) <= 400);

alter table public.common_mistakes
  drop constraint if exists common_mistakes_remediation_topic_len_chk;
alter table public.common_mistakes
  add constraint common_mistakes_remediation_topic_len_chk
  check (remediation_topic is null or char_length(remediation_topic) <= 120);

create index if not exists common_mistakes_problem_idx
  on public.common_mistakes (problem_id) where problem_id is not null;

create index if not exists common_mistakes_step_idx
  on public.common_mistakes (step_id) where step_id is not null;

alter table public.common_mistakes enable row level security;

drop policy if exists "common_mistakes_select_authenticated"
  on public.common_mistakes;
create policy "common_mistakes_select_authenticated"
  on public.common_mistakes for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- guided_problem_sessions — per-(session, problem) runtime state
-- -----------------------------------------------------------------------------
-- Decision J: this row is the authoritative state-holder while guided
-- mode is active. The post-turn extractor (state_updater.py) defers to
-- it for `session_state.mode` and `session_state.struggling_on`.
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

create index if not exists guided_problem_sessions_active_idx
  on public.guided_problem_sessions (session_id) where status = 'active';

alter table public.guided_problem_sessions enable row level security;

-- RLS: own sessions only (join through tutor_sessions.user_id, mirrors
-- the session_state policies in migration 006).
drop policy if exists "guided_problem_sessions_select_own"
  on public.guided_problem_sessions;
create policy "guided_problem_sessions_select_own"
  on public.guided_problem_sessions for select
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "guided_problem_sessions_insert_own"
  on public.guided_problem_sessions;
create policy "guided_problem_sessions_insert_own"
  on public.guided_problem_sessions for insert
  with check (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "guided_problem_sessions_update_own"
  on public.guided_problem_sessions;
create policy "guided_problem_sessions_update_own"
  on public.guided_problem_sessions for update
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = guided_problem_sessions.session_id
        and s.user_id = auth.uid()
    )
  );

-- -----------------------------------------------------------------------------
-- problems_without_solution_paths(...) — RPC for the generator script
-- -----------------------------------------------------------------------------
-- Returns problem rows that don't yet have ANY solution_paths entry.
-- Scoped per-language so we can ingest EN first and add HU later
-- (Decision C). Avoids LEFT JOIN exclusions in the Python client which
-- explode with NOT IN(...) once we pass ~1000 covered rows.
create or replace function public.problems_without_solution_paths(
  target_language text default 'en',
  max_count       int  default 500
)
returns setof public.problems
language sql stable
as $$
  select p.*
  from public.problems p
  left join public.solution_paths sp
    on sp.problem_id = p.id and sp.language = target_language
  where sp.id is null
  order by p.created_at desc
  limit greatest(1, least(max_count, 5000));
$$;

grant execute on function public.problems_without_solution_paths(text, int)
  to authenticated;

-- -----------------------------------------------------------------------------
-- annotated_problems_without_solution_paths(...) — backfill RPC (10A)
-- -----------------------------------------------------------------------------
-- The 205 problems with existing problem_annotations rows but no
-- solution_paths. The generator's `--from-annotations` mode pulls from
-- here so we get richer input scaffolding (the annotation payload
-- becomes additional context for the path-gen prompt).
create or replace function public.annotated_problems_without_solution_paths(
  target_language text default 'en',
  max_count       int  default 500
)
returns setof public.problems
language sql stable
as $$
  select p.*
  from public.problems p
  inner join public.problem_annotations pa on pa.problem_id = p.id
  left join public.solution_paths sp
    on sp.problem_id = p.id and sp.language = target_language
  where sp.id is null
  order by p.created_at desc
  limit greatest(1, least(max_count, 5000));
$$;

grant execute on function public.annotated_problems_without_solution_paths(text, int)
  to authenticated;
