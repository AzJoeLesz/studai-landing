-- =============================================================================
-- StudAI migration 005: OpenStax teaching material (RAG) + problem annotations
-- =============================================================================
--
-- Run in Supabase SQL editor after 003/004.
--
--  * teaching_material_chunks  -- text slices from extracted OpenStax books
--  * teaching_material_embeddings  -- pgvector for semantic match
--  * problem_annotations  -- AI-generated pedagogy JSON per problem (optional)
--  * match_teaching_material()  -- RPC for vector search
--  * teaching_chunks_without_embedding()  -- efficient batch for embedding jobs
-- =============================================================================

-- -----------------------------------------------------------------------------
-- teaching_material_chunks
-- -----------------------------------------------------------------------------
create table if not exists public.teaching_material_chunks (
  id              uuid primary key default gen_random_uuid(),
  source          text not null default 'openstax',
  book_slug       text not null,
  chunk_index     int  not null,
  page_start      int  not null,
  page_end        int  not null,
  body            text not null,
  created_at      timestamptz not null default now(),
  unique (source, book_slug, chunk_index)
);

create index if not exists teaching_material_chunks_book_idx
  on public.teaching_material_chunks (source, book_slug);

alter table public.teaching_material_chunks enable row level security;

drop policy if exists "teaching_material_chunks_select_authenticated"
  on public.teaching_material_chunks;
create policy "teaching_material_chunks_select_authenticated"
  on public.teaching_material_chunks for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- teaching_material_embeddings  (1536-dim, same model as problem bank)
-- -----------------------------------------------------------------------------
create table if not exists public.teaching_material_embeddings (
  chunk_id    uuid not null references public.teaching_material_chunks(id) on delete cascade,
  embedding   vector(1536) not null,
  created_at  timestamptz not null default now(),
  primary key (chunk_id)
);

create index if not exists teaching_material_embeddings_cosine_idx
  on public.teaching_material_embeddings
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

alter table public.teaching_material_embeddings enable row level security;

drop policy if exists "teaching_material_embeddings_select_authenticated"
  on public.teaching_material_embeddings;
create policy "teaching_material_embeddings_select_authenticated"
  on public.teaching_material_embeddings for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- problem_annotations  -- JSON payload from offline AI pipeline
-- -----------------------------------------------------------------------------
create table if not exists public.problem_annotations (
  problem_id    uuid not null references public.problems(id) on delete cascade,
  payload       jsonb not null,
  model         text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  primary key (problem_id)
);

create index if not exists problem_annotations_payload_gin
  on public.problem_annotations using gin (payload jsonb_path_ops);

alter table public.problem_annotations enable row level security;

drop policy if exists "problem_annotations_select_authenticated"
  on public.problem_annotations;
create policy "problem_annotations_select_authenticated"
  on public.problem_annotations for select
  to authenticated
  using (true);

-- -----------------------------------------------------------------------------
-- match_teaching_material()
-- -----------------------------------------------------------------------------
create or replace function public.match_teaching_material(
  query_embedding vector(1536),
  match_count int default 8
)
returns table (
  id          uuid,
  source      text,
  book_slug   text,
  page_start  int,
  page_end    int,
  body        text,
  similarity  float
)
language sql stable
as $$
  with k as (
    select greatest(1, least(coalesce(match_count, 8), 50)) as n
  ),
  ranked as (
    select
      c.id,
      c.source,
      c.book_slug,
      c.page_start,
      c.page_end,
      c.body,
      1 - (e.embedding <=> query_embedding) as similarity
    from public.teaching_material_embeddings e
    join public.teaching_material_chunks c on c.id = e.chunk_id
    order by e.embedding <=> query_embedding
    limit (select n from k)
  )
  select * from ranked;
$$;

grant execute on function public.match_teaching_material(vector, int)
  to authenticated;

-- -----------------------------------------------------------------------------
-- teaching_chunks_without_embedding()  -- batch list for embedding ingestion
-- -----------------------------------------------------------------------------
create or replace function public.teaching_chunks_without_embedding(
  max_count int default 1000
)
returns setof public.teaching_material_chunks
language sql stable
as $$
  select c.*
  from public.teaching_material_chunks c
  left join public.teaching_material_embeddings e on e.chunk_id = c.id
  where e.chunk_id is null
  order by c.source, c.book_slug, c.chunk_index
  limit greatest(1, least(coalesce(max_count, 1000), 5000));
$$;

grant execute on function public.teaching_chunks_without_embedding(int)
  to authenticated;

-- Same as above but scoped to one book (faster after a single-book ingest).
create or replace function public.teaching_chunks_without_embedding_for_book(
  p_book_slug text,
  max_count int default 2000
)
returns setof public.teaching_material_chunks
language sql stable
as $$
  select c.*
  from public.teaching_material_chunks c
  left join public.teaching_material_embeddings e on e.chunk_id = c.id
  where e.chunk_id is null
    and c.book_slug = p_book_slug
  order by c.chunk_index
  limit greatest(1, least(coalesce(max_count, 2000), 5000));
$$;

grant execute on function public.teaching_chunks_without_embedding_for_book(text, int)
  to authenticated;

-- -----------------------------------------------------------------------------
-- problems_without_annotations()  -- batch for offline annotation jobs
-- -----------------------------------------------------------------------------
create or replace function public.problems_without_annotations(max_count int default 500)
returns setof public.problems
language sql stable
as $$
  select p.*
  from public.problems p
  left join public.problem_annotations a on a.problem_id = p.id
  where a.problem_id is null
  order by p.id
  limit greatest(1, least(coalesce(max_count, 500), 5000));
$$;

grant execute on function public.problems_without_annotations(int)
  to authenticated;
