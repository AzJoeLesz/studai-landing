-- =============================================================================
-- StudAI migration 006: session_state, student_progress + profile additions
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.
--
-- Phase 9 — personalization & adaptation layer. See
-- docs/phase9_personalization.md for the full design rationale.
--
-- What this sets up:
--   * `public.session_state`         — per-session structured snapshot
--                                       written by the post-turn extractor
--                                       (current topic, mode, attempts,
--                                       struggling_on, mood_signals jsonb,
--                                       running summary).
--   * `public.student_progress`      — per-(user, topic) mastery score
--                                       updated by 9D's BKT-IDEM model from
--                                       multiple evidence sources.
--   * `public.profiles` extensions   — `share_progress_with_parents`
--                                       consent flag (Phase 13 will read it),
--                                       `preferences` jsonb for the 9C
--                                       personality micro-survey.
--   * RLS policies that mirror the rest of the schema.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- profiles: consent flag + personality preferences container
-- -----------------------------------------------------------------------------
alter table public.profiles
  add column if not exists share_progress_with_parents boolean
    not null default false;

alter table public.profiles
  add column if not exists preferences jsonb
    not null default '{}'::jsonb;

-- Defense-in-depth: keep preferences blob bounded so a buggy frontend can't
-- shove a megabyte of JSON into the prompt context.
alter table public.profiles
  drop constraint if exists profiles_preferences_size_chk;
alter table public.profiles
  add constraint profiles_preferences_size_chk
  check (octet_length(preferences::text) <= 8192);

-- -----------------------------------------------------------------------------
-- session_state
-- One row per session. Created lazily on first extractor write; the tutor
-- treats a missing row as "everything default / unknown".
-- -----------------------------------------------------------------------------
create table if not exists public.session_state (
  session_id      uuid primary key
                  references public.tutor_sessions(id) on delete cascade,
  current_topic   text,
  mode            text check (mode in (
                    'problem','concept','verification','conversational','lesson'
                  )),
  attempts_count  int  not null default 0,
  struggling_on   text,
  mood_signals    jsonb not null default '{}'::jsonb,
  summary         text,
  updated_at      timestamptz not null default now()
);

-- Length caps so the system prompt block stays sane.
alter table public.session_state
  drop constraint if exists session_state_current_topic_len_chk;
alter table public.session_state
  add constraint session_state_current_topic_len_chk
  check (current_topic is null or char_length(current_topic) <= 120);

alter table public.session_state
  drop constraint if exists session_state_struggling_on_len_chk;
alter table public.session_state
  add constraint session_state_struggling_on_len_chk
  check (struggling_on is null or char_length(struggling_on) <= 400);

alter table public.session_state
  drop constraint if exists session_state_summary_len_chk;
alter table public.session_state
  add constraint session_state_summary_len_chk
  check (summary is null or char_length(summary) <= 4000);

alter table public.session_state
  drop constraint if exists session_state_mood_signals_size_chk;
alter table public.session_state
  add constraint session_state_mood_signals_size_chk
  check (octet_length(mood_signals::text) <= 4096);

create index if not exists session_state_updated_at_idx
  on public.session_state (updated_at desc);

alter table public.session_state enable row level security;

drop policy if exists "session_state_select_own" on public.session_state;
create policy "session_state_select_own"
  on public.session_state for select
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = session_state.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "session_state_insert_own" on public.session_state;
create policy "session_state_insert_own"
  on public.session_state for insert
  with check (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = session_state.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "session_state_update_own" on public.session_state;
create policy "session_state_update_own"
  on public.session_state for update
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = session_state.session_id
        and s.user_id = auth.uid()
    )
  );

-- -----------------------------------------------------------------------------
-- student_progress
-- Per-(user, topic) mastery score. Driven by:
--   * grade priors (seeded once from backend/app/data/grade_priors.json)
--   * placement quiz outcomes (Phase 9E)
--   * post-turn extractor's noisy mastery_signals (Phase 9A, low weight)
--   * thumbs ratings (Phase 12)
--   * verified step-checks (Phase 10)
-- The `evidence_source` column distinguishes them so we can weight & audit.
-- -----------------------------------------------------------------------------
create table if not exists public.student_progress (
  user_id          uuid not null
                   references public.profiles(id) on delete cascade,
  topic            text not null,
  mastery_score    numeric not null default 0.5
                   check (mastery_score between 0 and 1),
  evidence_count   int     not null default 0,
  evidence_source  text    not null default 'prior'
                   check (evidence_source in (
                     'prior','placement','extractor','rating','step_check'
                   )),
  last_seen_at     timestamptz not null default now(),
  primary key (user_id, topic)
);

alter table public.student_progress
  drop constraint if exists student_progress_topic_len_chk;
alter table public.student_progress
  add constraint student_progress_topic_len_chk
  check (char_length(topic) between 1 and 120);

create index if not exists student_progress_user_seen_idx
  on public.student_progress (user_id, last_seen_at desc);

alter table public.student_progress enable row level security;

drop policy if exists "student_progress_select_own" on public.student_progress;
create policy "student_progress_select_own"
  on public.student_progress for select
  using (auth.uid() = user_id);

drop policy if exists "student_progress_insert_own" on public.student_progress;
create policy "student_progress_insert_own"
  on public.student_progress for insert
  with check (auth.uid() = user_id);

drop policy if exists "student_progress_update_own" on public.student_progress;
create policy "student_progress_update_own"
  on public.student_progress for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- -----------------------------------------------------------------------------
-- placement_attempts
-- One row per question shown in the optional onboarding placement quiz.
-- Decoupled from `messages` because (a) it isn't a tutor session and
-- (b) we want a clean evidence trail for the BKT update at evidence_source
-- = 'placement'.
-- -----------------------------------------------------------------------------
create table if not exists public.placement_attempts (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.profiles(id) on delete cascade,
  problem_id    uuid not null references public.problems(id) on delete cascade,
  topic         text not null,
  difficulty    text not null,
  correct       boolean not null,
  created_at    timestamptz not null default now()
);

create index if not exists placement_attempts_user_created_idx
  on public.placement_attempts (user_id, created_at desc);

alter table public.placement_attempts enable row level security;

drop policy if exists "placement_attempts_select_own" on public.placement_attempts;
create policy "placement_attempts_select_own"
  on public.placement_attempts for select
  using (auth.uid() = user_id);

drop policy if exists "placement_attempts_insert_own" on public.placement_attempts;
create policy "placement_attempts_insert_own"
  on public.placement_attempts for insert
  with check (auth.uid() = user_id);
