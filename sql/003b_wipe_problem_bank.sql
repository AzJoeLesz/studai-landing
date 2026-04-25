-- =============================================================================
-- StudAI migration 003b: WIPE the problem bank (use ONLY when intentional)
-- =============================================================================
--
-- Run this in the Supabase SQL editor when you need to start the problem
-- bank fresh -- e.g., after fixing the source_id bug that caused hendrycks
-- problems to overwrite each other.
--
-- This deletes:
--   * all rows in public.problems
--   * all rows in public.problem_embeddings (cascade)
--   * all rows in public.problem_translations (cascade)
--
-- Schema, indexes, RLS, and the match_problems()/problems_without_embedding()
-- functions are PRESERVED.
--
-- After running this, re-run `python -m scripts.ingest_problems --embed`
-- from backend/ to repopulate the corpus cleanly.
-- =============================================================================

truncate table public.problems cascade;

-- Sanity check -- should print 0, 0, 0.
select
  (select count(*) from public.problems)              as problems,
  (select count(*) from public.problem_embeddings)    as embeddings,
  (select count(*) from public.problem_translations)  as translations;
