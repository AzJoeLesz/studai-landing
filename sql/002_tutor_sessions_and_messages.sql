-- =============================================================================
-- StudAI migration 002: tutor_sessions + messages
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard → SQL Editor → New query).
--
-- What this sets up:
--   * `public.tutor_sessions` — one row per chat/tutoring conversation.
--   * `public.messages` — one row per user/assistant turn inside a session.
--   * Row Level Security policies so a user can only see their own data
--     (defense in depth — our backend uses the service_role key and bypasses
--     RLS, but these policies protect against accidental anon-key queries).
--   * A trigger that bumps `tutor_sessions.updated_at` whenever a new message
--     is inserted, so session lists can sort by recency cheaply.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- tutor_sessions
-- -----------------------------------------------------------------------------
create table if not exists public.tutor_sessions (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.profiles(id) on delete cascade,
  title      text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists tutor_sessions_user_updated_idx
  on public.tutor_sessions (user_id, updated_at desc);

alter table public.tutor_sessions enable row level security;

drop policy if exists "tutor_sessions_select_own" on public.tutor_sessions;
create policy "tutor_sessions_select_own"
  on public.tutor_sessions for select
  using (auth.uid() = user_id);

drop policy if exists "tutor_sessions_insert_own" on public.tutor_sessions;
create policy "tutor_sessions_insert_own"
  on public.tutor_sessions for insert
  with check (auth.uid() = user_id);

drop policy if exists "tutor_sessions_update_own" on public.tutor_sessions;
create policy "tutor_sessions_update_own"
  on public.tutor_sessions for update
  using (auth.uid() = user_id);

drop policy if exists "tutor_sessions_delete_own" on public.tutor_sessions;
create policy "tutor_sessions_delete_own"
  on public.tutor_sessions for delete
  using (auth.uid() = user_id);

-- -----------------------------------------------------------------------------
-- messages
-- -----------------------------------------------------------------------------
create table if not exists public.messages (
  id         uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.tutor_sessions(id) on delete cascade,
  role       text not null check (role in ('user', 'assistant', 'system', 'tool')),
  content    text not null,
  created_at timestamptz not null default now()
);

create index if not exists messages_session_created_idx
  on public.messages (session_id, created_at);

alter table public.messages enable row level security;

drop policy if exists "messages_select_own" on public.messages;
create policy "messages_select_own"
  on public.messages for select
  using (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = messages.session_id
        and s.user_id = auth.uid()
    )
  );

drop policy if exists "messages_insert_own" on public.messages;
create policy "messages_insert_own"
  on public.messages for insert
  with check (
    exists (
      select 1 from public.tutor_sessions s
      where s.id = messages.session_id
        and s.user_id = auth.uid()
    )
  );

-- -----------------------------------------------------------------------------
-- Trigger: keep tutor_sessions.updated_at fresh when messages arrive.
-- -----------------------------------------------------------------------------
create or replace function public.touch_tutor_session_updated_at()
returns trigger
language plpgsql
security definer
as $$
begin
  update public.tutor_sessions
     set updated_at = now()
   where id = new.session_id;
  return new;
end;
$$;

drop trigger if exists on_message_inserted on public.messages;
create trigger on_message_inserted
  after insert on public.messages
  for each row execute function public.touch_tutor_session_updated_at();
