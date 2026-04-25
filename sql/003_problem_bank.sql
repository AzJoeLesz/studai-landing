-- =============================================================================
-- StudAI migration 003: problem bank + pgvector embeddings
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
--
-- What this sets up:
--   * `pgvector` extension (Postgres extension for vector similarity search).
--   * `public.problems` -- canonical math problem rows (English source of truth).
--   * `public.problem_translations` -- per-language translations (Hungarian, etc.).
--   * `public.problem_embeddings` -- vector embeddings, one row per (problem, language).
--   * Indexes for fast filtering and similarity search.
--   * RLS: problems are PUBLIC content (readable by all authenticated users),
--     but only the service_role key can insert/update/delete.
--   * `match_problems()` function that returns the top-k nearest problems by
--     embedding similarity, with optional filters.
-- =============================================================================

create extension if not exists vector;

-- -----------------------------------------------------------------------------
-- problems  (English source of truth)
-- -----------------------------------------------------------------------------
create table if not exists public.problems (
  id          uuid primary key default gen_random_uuid(),
  source      text not null,                -- 'hendrycks', 'gsm8k', 'asdiv', 'svamp'
  type        text not null,                -- 'Algebra', 'Geometry', 'word_problem', ...
  difficulty  text,                         -- 'Level 1'..'Level 5', 'easy', 'medium', 'easy_medium'
  problem_en  text not null,
  solution_en text not null,
  answer      text,                         -- final answer when explicit; null for hendrycks (boxed)
  source_id   text,                         -- optional original-id from the dataset (for re-ingestion idempotency)
  created_at  timestamptz not null default now(),
  unique (source, source_id)                -- safe to re-run ingestion
);

create index if not exists problems_type_difficulty_idx
  on public.problems (type, difficulty);

create index if not exists problems_source_idx
  on public.problems (source);

alter table public.problems enable row level security;

-- Authenticated users can READ all problems (this is shared content).
drop policy if exists "problems_select_authenticated" on public.problems;
create policy "problems_select_authenticated"
  on public.problems for select
  to authenticated
  using (true);

-- Inserts/updates/deletes are intentionally NOT exposed via RLS.
-- The backend uses the service_role key (which bypasses RLS), so only
-- our ingestion scripts can mutate this table. Keeping the policies absent
-- is the correct way to lock down writes.

-- -----------------------------------------------------------------------------
-- problem_translations  (one row per non-English language per problem)
-- -----------------------------------------------------------------------------
create table if not exists public.problem_translations (
  problem_id    uuid not null references public.problems(id) on delete cascade,
  language      text not null check (language in ('hu')),  -- extend with more codes later
  problem_text  text not null,
  solution_text text not null,
  created_at    timestamptz not null default now(),
  primary key (problem_id, language)
);

alter table public.problem_translations enable row level security;

drop policy if exists "problem_translations_select_authenticated" on public.problem_translations;
create policy "problem_translations_select_authenticated"
  on public.problem_translations for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- problem_embeddings  (one row per (problem, language) once embedded)
-- -----------------------------------------------------------------------------
-- 1536 dims = OpenAI `text-embedding-3-small` output.
-- If we ever switch model, drop the table and re-embed; the dim is fixed.
create table if not exists public.problem_embeddings (
  problem_id uuid not null references public.problems(id) on delete cascade,
  language   text not null check (language in ('en', 'hu')),
  embedding  vector(1536) not null,
  created_at timestamptz not null default now(),
  primary key (problem_id, language)
);

-- IVFFlat index for cosine similarity. `lists` chosen for ~18k rows;
-- rule of thumb is sqrt(n_rows) -> ~134, rounded to 100.
-- IMPORTANT: build this AFTER the table has data (Postgres can build it
-- on an empty table but the resulting index is suboptimal). The migration
-- creates a placeholder; we recommend running:
--   reindex index problem_embeddings_cosine_idx;
-- after the first big ingestion.
create index if not exists problem_embeddings_cosine_idx
  on public.problem_embeddings
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

alter table public.problem_embeddings enable row level security;

drop policy if exists "problem_embeddings_select_authenticated" on public.problem_embeddings;
create policy "problem_embeddings_select_authenticated"
  on public.problem_embeddings for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- match_problems()  -- vector similarity search with optional filters
-- -----------------------------------------------------------------------------
-- Returns top-k problems whose `language` embedding is closest to the
-- supplied query embedding (cosine distance).
--
-- `filter_type` and `filter_difficulty` are nullable -- pass null to skip.
-- `match_count` clamped to 1..50 inside the function.
-- Returns the problem in the requested language when available, falling
-- back to English when no translation exists.
-- -----------------------------------------------------------------------------
create or replace function public.match_problems(
  query_embedding vector(1536),
  match_language  text,
  match_count     int default 10,
  filter_type     text default null,
  filter_difficulty text default null
)
returns table (
  id          uuid,
  source      text,
  type        text,
  difficulty  text,
  problem     text,
  solution    text,
  answer      text,
  language    text,
  similarity  float
)
language sql stable
as $$
  with k as (
    select greatest(1, least(coalesce(match_count, 10), 50)) as n
  ),
  ranked as (
    select
      p.id,
      p.source,
      p.type,
      p.difficulty,
      p.answer,
      coalesce(t.problem_text,  p.problem_en)  as problem,
      coalesce(t.solution_text, p.solution_en) as solution,
      case when t.problem_text is not null then match_language else 'en' end as language,
      1 - (e.embedding <=> query_embedding) as similarity
    from public.problem_embeddings e
    join public.problems p on p.id = e.problem_id
    left join public.problem_translations t
      on t.problem_id = p.id and t.language = match_language
    where e.language = match_language
      and (filter_type is null or p.type = filter_type)
      and (filter_difficulty is null or p.difficulty = filter_difficulty)
    order by e.embedding <=> query_embedding
    limit (select n from k)
  )
  select * from ranked;
$$;

-- Allow authenticated users to call the search function. The function is
-- `stable` and only reads RLS-protected tables, so it's safe.
grant execute on function public.match_problems(vector, text, int, text, text)
  to authenticated;
