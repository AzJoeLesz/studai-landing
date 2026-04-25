# StudAI

StudAI is an **AI math tutor** product: a Next.js frontend (`app/`) and a Python FastAPI backend (`backend/`) backed by **Supabase (Postgres + Auth)**, with **SSE streaming** chat, **multilingual UI** (English / Hungarian via `next-intl`), and a **scalable content + RAG pipeline** for problems and OpenStax textbook material.

This document is the **handoff + roadmap** for new sessions: architecture, **three tutor grounding layers** (and how they differ), what is implemented, legal/content strategy, and the **numbered product roadmap (Phases 6–16)** below.

---

## How the tutor is grounded (three layers — all matter)

Each chat turn can add up to **three optional system blocks** (see `backend/app/agents/retrieval.py` → `build_grounding_context`, consumed in `tutor.py`). They are **complementary**, not redundant.

| Layer | What it is | When it helps | How it gets into the system |
|--------|------------|----------------|-----------------------------|
| **1. Problem bank RAG** | Similar **exercises** from your ingested corpus (~18k+), with **worked solutions** | Student message is close to a known problem type; gives the model private “ground truth” for checking math / answer direction **without** revealing the answer to the student | Embed user message → `match_problems` (pgvector on `problem_embeddings`) |
| **2. OpenStax textbook RAG** | **Short chunks** from CC-BY OpenStax books (definitions, standard methods, narrative) | Keeps explanations **aligned with standard curriculum** and terminology | Embed same user message (single embed reused with layer 1) → `match_teaching_material` on `teaching_material_embeddings` |
| **3. Precomputed problem annotations** | **Structured pedagogy** per corpus problem: hint ladder, common mistakes, solution outline, concepts, etc. (JSON) | Calibrates **Socratic hints** and mistake handling for **similar** problems when a row exists | Loaded **only for problem-bank hits** that have a row in `problem_annotations` (from the offline `annotate_problems` job) |

**Why book embeddings do not replace annotations**

- **OpenStax chunks** answer: “What does the textbook say about this idea?” (general reference text at query time).
- **Problem annotations** answer: “How should we *teach this specific exercise pattern*—hints, typical errors, steps—when the bank matches?” (curated, problem-centric metadata generated once and keyed by `problem_id`).

Both are used when present; the tutor prompt instructs the model to treat all as private context. If you **only** run book ingestion, layers 1 + 2 work; layer 3 appears after you run **`python -m scripts.annotate_problems`** (and only for problems that have rows).

Tunable env (see `backend/app/core/config.py`): `rag_*`, `material_rag_*`, `annotation_injection_enabled`.

---

## Tech stack (current)

| Area | Choice |
|------|--------|
| Frontend | Next.js 14 App Router, TypeScript, Tailwind, shadcn/ui, Framer Motion, `next-intl`, KaTeX via `rehype-katex` / `react-markdown` |
| Backend | Python 3.12+, FastAPI, Uvicorn, Pydantic Settings, OpenAI SDK, Supabase client (service role server-side) |
| Database | Supabase / Postgres, **pgvector**, RLS on user-facing tables; service role for ingestion & backend writes |
| Auth | Supabase JWT (ES256) verified in backend |
| Chat transport | **SSE** streaming for assistant tokens |
| Embeddings | `text-embedding-3-small` (1536 dims) — problem bank + teaching material |
| LLM | Configurable; production defaults include `gpt-4o`-class for chat; evals and scripts may use the same or stronger models for judges |

**Deployment (typical):** frontend **Vercel**, backend **Railway**, DB + auth **Supabase**. Root directory for Railway is `backend/`.

---

## Repository layout (short)

- `app/` — marketing + dashboard + chat UI by locale (`[locale]/…`)
- `components/` — shared UI, chat, brand
- `messages/` — i18n JSON (en, hu)
- `backend/` — API, agents, DB, LLM, embeddings, prompts, **evals** (`backend/evals/`, see `backend/evals/README.md`)
- `sql/` — migrations to run in Supabase SQL editor (including problem bank, profile extensions, **teaching material + annotations**)
- `books/` — local OpenStax PDFs (gitignored)
- `books_extracted/` — per-page text from PDFs (gitignored), produced by `backend/scripts/extract_books.py`

---

## Content & licensing (agreed direction)

- **Reference textbooks:** use **only** materials you have the right to use. The pipeline is designed around **OpenStax (CC-BY)** PDFs: extract text, chunk, embed, retrieve at runtime.
- **Per-problem and topic content** (annotations, topic docs) should be **AI-generated original pedagogy**, optionally **grounded** on OpenStax excerpts — not a verbatim dump of third-party non-open books.
- **Hungarian:** generate from English with controlled translation and review, and/or **licensed** sources; avoid scraping NC-licensed or “no AI” sites.
- Ingested **problem bank** (Hendrycks, GSM8K, etc.) follows dataset licensing; keep attributions in product/legal docs as required.

Do **not** commit large PDFs or extracted trees; `.gitignore` already excludes `books/` and `books_extracted/`.

---

## Offline / CLI pipelines

Run from `backend/` with a filled `backend/.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY` — **service role** for writes).

1. **Extract PDFs → markdown per page**  
   `python -m scripts.extract_books`  
   Output: `books_extracted/en/openstax_algebra/<slug>/pages/*.md`

2. **Ingest problem bank (JSONL)**  
   `python -m scripts.ingest_problems` (optional `--embed`)

3. **Ingest OpenStax chunks + embeddings** (after SQL migration for teaching tables)  
   `python -m scripts.ingest_openstax_material`  
   Fills `teaching_material_chunks` + `teaching_material_embeddings`

4. **Precompute problem annotations (optional but recommended for layer 3)**  
   `python -m scripts.annotate_problems --limit <n>`  
   Fills `problem_annotations` (uses OpenStax RAG in the *annotation* prompt, not a substitute for live tutor RAG)  
   - If this table is **empty in Supabase**, layer 3 is **off** until you run the job; layers 1–2 still work.  
   - **Smoke test (no DB writes):** `python -m scripts.annotate_problems --limit 1 --dry-run` (still calls the LLM).  
   - **Write a few rows:** `python -m scripts.annotate_problems --limit 5`, then check the table.  
   - **Scale up:** increase `--limit` in steps (e.g. 50, 200); optional `--concurrency` (default 2). You do not need the full problem bank on day one.  
   - **Production:** deploy the backend (e.g. Railway) so the live tutor uses the same Supabase project you ingested and annotated.
   - **Verify layers:** `python -m scripts.smoke_tutor_grounding` (optional: set `SMOKE_BACKEND_URL` to also GET `/health`).

5. **Translations (scaffold)**  
   `python -m scripts.translate_problems` (as implemented)

**Supabase `SUPABASE_URL`** must match the project ref in your JWT (Settings → API → Project URL). A typo causes DNS (`getaddrinfo`) or auth errors.

---

## Quality & prompt engineering

- **System prompts:** e.g. `backend/app/prompts/tutor_v2.txt` (Socratic, one move per reply, mode awareness, no answer leak).
- **Answer-leak guard:** optional post-reply check in `tutor.py` (config flag).
- **Evaluations:** rubric-based **LLM-as-judge** harness under `backend/evals/`; see `backend/evals/README.md` for baselines, reports, and iteration.

---

## Product roadmap: Phases 6–16

Numbered phases for continuity across sessions. **Effort** assumes solo founder pace working with a collaborator, not a team. **Phases 14+** each take longer than the entire previous stack combined; they are last on purpose. Repo reality may be ahead or behind any phase — verify in code and migrations.

### Conventions

- Each phase is a **single shippable, testable** increment. No phase ends in a “nothing visible yet” state.
- **“Ships visibly”** = a real student opening the site that day would notice the difference.

### Phase 6 — Math rendering (KaTeX + markdown)

**Why now:** The tutor is unreadable without it. Single biggest UX bug today.

**Scope:**

- Add `react-markdown` + `remark-math` + `rehype-katex` + KaTeX CSS
- Replace plain-text `MessageBubble` with markdown rendering: bold/italic/lists/code/inline + display math
- Style the markdown to match the Claude-ish identity (subtle, not over-styled)
- Confirm the existing tutor system prompt’s “use LaTeX” instruction actually shows as math

**Effort:** 2–3 days — **Ships visibly:** Yes — every reply looks like a real math tutor

### Phase 7 — Tutor brain v2: Socratic + mode awareness

**Why now:** Cheapest way to make the AI feel like a tutor instead of GPT-with-a-prompt.

**Scope:**

- New prompt version (e.g. `prompts/tutor_v2`) that: enforces “guide, don’t reveal” pedagogy; detects intent from the student’s message (problem-solving vs learning vs conversational); adapts tone accordingly
- A lightweight “second pass” guard: cheap LLM check for “did the previous reply leak the answer?” — only in problem-solving mode
- Bump `CURRENT_TUTOR_PROMPT` to v2

**Effort:** 3–5 days (mostly prompt iteration on real conversations) — **Ships visibly:** Yes — the AI asks questions instead of dumping answers

### Phase 8 — Problem bank ingestion + RAG

**Why now:** Foundation for Phases 10 + 11. Massive content lift for one phase.

**Scope:**

- New tables: `problems`, `problem_solutions`, `problem_tags`
- Ingestion script (`backend/scripts/ingest_problems.py`): JSONL → translate problem + solution to Hungarian via gpt-4o-mini; store EN + HU; embeddings with `text-embedding-3-small`; enable pgvector in Supabase
- Backend: `/problems/search?q=...` — semantic + filter search
- Tag each problem with topic via AI classifier (1 call per problem) → human-correctable later
- **Cost (order of magnitude):** ~€700 one-time translation, ~€10 embeddings

**Effort:** 1.5–2 weeks (mostly scripts + ingestion time) — **Ships visibly:** Internally yes (admin can search); external chat connection is Phase 10

### Phase 9 — Student profile + session state

**Why now:** Every later phase reads from this.

**Scope:**

- Extend `profiles`: age, `grade_level`, interests (jsonb), goals, notes
- New: `student_progress` — topic-level mastery (fed by Phase 12 quality loop)
- New: `session_state` — per-session jsonb: current topic, mood signals, struggling-on-what, attempts so far
- Backend: every chat request loads profile + session state, summarizes, includes in prompt
- Frontend: settings page — one form (age, grade, interests)

**Effort:** ~1 week — **Ships visibly:** Yes — name, grade-level, age-appropriate examples

### Phase 10 — Authenticated solution graphs (the moat)

**Why now:** This is what makes it a tutor, not a chatbot. Foundation before this is table stakes.

**Scope:**

- New tables: `solution_paths`, `solution_steps`, `step_hints`, `common_mistakes`
- Per problem: AI-generate 2–3 named solution paths with hints + mistake mapping → you spot-check, mark verified
- Tutor: **guided problem mode** on verified problems — model knows the path, never reveals the answer; student steps checked against path; on-path → encourage + next question; off-path but valid → adjust path; wrong → `common_mistakes` → pedagogical hint, not the answer

**Effort:** 3–4 weeks (mostly content + AI-augmentation pipeline) — **Ships visibly:** Yes — genuine guidance, not explanation-only

### Phase 11 — Curated learning materials + lesson mode

**Why now:** Sessions split between “I have a problem” and “I want to learn X”.

**Scope:**

- New tables: `topics`, `topic_materials`, `topic_problem_links`
- Start with 5–10 topics in one tight slice (e.g. 9. évfolyam: másodfokú egyenletek és függvények) — summary, full lesson markdown, examples, problem links
- **Lesson mode** in the tutor: curated material as lesson script + Socratic checkpoints
- Cross-link: in problem mode, if a prerequisite is missing, offer “want to step back to the lesson?”

**Effort:** 2–3 weeks (engineering small, content medium) — **Ships visibly:** Yes — “I want to learn X” not just “help with this problem”

### Phase 12 — Quality measurement loop

**Why now:** From here, measure prompt changes — not vibes.

**Scope:**

- 👍/👎 on each AI reply; optional comment on 👎
- Internal admin at `/admin` (gated): recent sessions, sortable by rating; full transcripts; filter by topic / age / mode
- `prompt_runs`: `(message_id, prompt_version, rating)` (and A/B: config flag to assign v2 vs v3 for new sessions, compare ratings)

**Effort:** 1.5–2 weeks — **Ships visibly:** Subtle (thumbs); payoff is internal — “is the tutor getting better?”

### Phase 13 — Parent / teacher view (read-only)

**Why now:** Honest parent-facing story → monetize → fund Phase 14+.

**Scope:**

- Auth role on `profiles`: `student` | `parent` | `teacher`
- Parent ↔ student linking (parent enters child email; child confirms)
- `/dashboard/family`: linked students; per-student recent sessions, time spent, topics, mastery; read-only transcripts (with student consent in Settings)
- Weekly email summary cron (e.g. Resend)

**Effort:** 2–3 weeks — **Ships visibly:** Yes — parents see what they’d pay for

### Phase 14 — Voice (the pipeline)

**Why now:** Voice on a weak brain is expensive theatre; ship after the brain is good enough to deserve it.

**Scope:**

- Browser: mic → audio chunks to backend; streaming playback
- Backend: `/voice/turn` (WebSocket or SSE) — streaming STT → existing chat brain → streaming TTS
- STT: Whisper API vs Deepgram (test HU first); TTS: Cartesia vs Google Cloud vs ElevenLabs (test HU first)
- Barge-in: VAD in browser → stop TTS + cancel in-flight LLM stream
- UI: “Voice session” toggle; transcript still like text sessions
- **Cost gate:** voice as paid feature (Stripe here or defer to Phase 16)

**Effort:** 4–6 weeks — **Ships visibly:** Massive (WOW phase)

### Phase 15 — Emotion detection layer

**Why now:** With voice, audio is already flowing; extra signals are incremental.

**Scope:**

- `backend/app/audio/signals.py`
- **Layer 1:** OpenSMILE eGeMAPS — rate, pitch range, energy, pauses, fillers → JSON on each voice message
- **Layer 3 (after layer 1 is validated):** wav2vec2 audeering on Modal/Replicate — valence/arousal/dominance
- Tutor prompt v3: read `audio_signals` and adapt; per-user rolling baselines (relative, not absolute)

**Effort:** ~2 weeks — **Ships visibly:** Subtle — the tutor “gets” mood; retention more than flash

### Phase 16 — Whiteboard

**Why now:** Last because it is largest. Brain, voice, and content need to be solid or the whiteboard is decoration.

**Scope (MVP):**

- **tldraw** (MIT) — side panel in chat
- AI tools in the agent: `draw_text`, `draw_shape`, `render_equation`, `draw_arrow`, `highlight_region`, `clear_area`
- **16a:** AI read-only (“let me sketch this triangle”)
- **16b:** Collaborative — student draws; AI reacts (“I see you drew this — try extending the line here”)
- Sync: Yjs (P2P) or Liveblocks (managed) — start with simple state push from backend

**Effort:** 6–10 weeks for a polished MVP; longer for excellent — **Ships visibly:** Massive (final differentiator)

### Ongoing (not a numbered phase)

- **OpenStax / evals:** expand book coverage, reindex after large loads, regression eval sets (`backend/evals`)
- **SymPy (or similar)** symbolic check for student work — tool path, not required for phases above
- **Legal / compliance:** separation of reference vs generated content; PDPL/GDPR as the product matures; voice: consent, retention (audio vs transcript)

---

## Related docs

- `backend/README.md` — API layout, env, how a chat turn flows
- `backend/evals/README.md` — running evaluations and reports
- `sql/*.sql` — database migrations (run in Supabase in order, including `005_teaching_material_and_annotations.sql`)

---

## Clarification for “why annotations if we have books?”

**Books (layer 2):** curriculum grounding from **narrative/definitions** in chunks.  
**Annotations (layer 3):** **problem-specific pedagogy** tied to corpus items for **similar** hits.  
**Problem bank (layer 1):** **worked solutions** for similar items.

All three are intended to be used **together** when data exists; “optional” only meant the **batch job** for annotations is a separate step from **book ingest** — not that annotations are unimportant for the end state.

---

*Last updated: product roadmap Phases 6–16, grounding layers, licensing, and handoff links.*
