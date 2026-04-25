-- =============================================================================
-- StudAI migration 004: profile extensions for the student model
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.
--
-- What this sets up:
--   * Adds five OPTIONAL columns to public.profiles so the tutor can
--     personalize: age, grade_level, interests, learning_goals, notes.
--   * Updates the row-level security policy so users can SELECT and UPDATE
--     their own profile (in case it wasn't already).
--
-- Column choices:
--   * `age` is a small int with a sanity-check range (kids and college kids).
--   * `grade_level` is text (not enum) because schooling varies wildly:
--     "9. evfolyam", "Year 11", "Grade 7", "University 2nd year".
--   * `interests`, `learning_goals`, `notes` are short free-form text.
--     The tutor LLM parses them; we don't need structured fields yet.
--   * Everything is NULLABLE so existing accounts keep working immediately.
-- =============================================================================

alter table public.profiles
  add column if not exists age            smallint check (age between 5 and 30),
  add column if not exists grade_level    text,
  add column if not exists interests      text,
  add column if not exists learning_goals text,
  add column if not exists notes          text;

-- Length caps -- defense in depth so a confused frontend can't shove a
-- 500 KB payload into the system prompt context.
alter table public.profiles
  drop constraint if exists profiles_grade_level_len_chk;
alter table public.profiles
  add constraint profiles_grade_level_len_chk
  check (grade_level    is null or char_length(grade_level)    <= 80);

alter table public.profiles
  drop constraint if exists profiles_interests_len_chk;
alter table public.profiles
  add constraint profiles_interests_len_chk
  check (interests      is null or char_length(interests)      <= 400);

alter table public.profiles
  drop constraint if exists profiles_learning_goals_len_chk;
alter table public.profiles
  add constraint profiles_learning_goals_len_chk
  check (learning_goals is null or char_length(learning_goals) <= 400);

alter table public.profiles
  drop constraint if exists profiles_notes_len_chk;
alter table public.profiles
  add constraint profiles_notes_len_chk
  check (notes          is null or char_length(notes)          <= 1000);

-- Make sure RLS lets a user manage their own profile. These statements are
-- idempotent (drop if exists + create) so re-running won't conflict with
-- whatever was in place before.
alter table public.profiles enable row level security;

drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own"
  on public.profiles for select
  using (auth.uid() = id);

drop policy if exists "profiles_update_own" on public.profiles;
create policy "profiles_update_own"
  on public.profiles for update
  using (auth.uid() = id)
  with check (auth.uid() = id);

drop policy if exists "profiles_insert_own" on public.profiles;
create policy "profiles_insert_own"
  on public.profiles for insert
  with check (auth.uid() = id);
