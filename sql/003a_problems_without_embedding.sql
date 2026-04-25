-- =============================================================================
-- StudAI migration 003a: problems_without_embedding() helper
-- =============================================================================
--
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Idempotent: safe to re-run.
--
-- Why this exists:
--
-- The Python ingestion script needs to "find me up to N problems that
-- don't yet have an embedding for language X". The naive client-side
-- approach is:
--   1. Pull every embedded problem_id (could be tens of thousands).
--   2. Send `id NOT IN (<all those ids>)` back to Postgres.
-- That second query stuffs all IDs into the URL and explodes past
-- Postgrest's URL length limit at ~1000 IDs ("Bad Request" / 414).
--
-- Doing the exclusion server-side as a single LEFT JOIN avoids the
-- whole round-trip and scales arbitrarily.
-- =============================================================================

create or replace function public.problems_without_embedding(
  target_language text,
  max_count       int default 1000
)
returns setof public.problems
language sql
stable
as $$
  select p.*
  from public.problems p
  left join public.problem_embeddings e
    on e.problem_id = p.id and e.language = target_language
  where e.problem_id is null
  order by p.created_at
  limit greatest(1, least(coalesce(max_count, 1000), 5000));
$$;

-- service_role already bypasses RLS, but we grant explicitly so a future
-- caller using the anon/authenticated key can still use it if needed.
grant execute on function public.problems_without_embedding(text, int)
  to authenticated, service_role;

-- Speed up the LEFT JOIN above. The primary key on
-- `problem_embeddings(problem_id, language)` is the wrong column order for
-- filtering by language first; this composite index fixes that and makes
-- the anti-join fast enough to comfortably finish under the 8s timeout
-- even at full corpus size.
create index if not exists problem_embeddings_lang_problem_idx
  on public.problem_embeddings (language, problem_id);
