# StudAI backend

FastAPI service that is the **brain** of StudAI: it verifies Supabase user
tokens, reads and writes `tutor_sessions` and `messages` in Postgres, and
streams LLM replies to the browser.

Deploys as a single service (currently Railway). The Next.js frontend on
Vercel calls it; it never serves HTML.

## Project layout

```
backend/
├── main.py                      FastAPI entry (`uvicorn main:app`)
├── requirements.txt             Python dependencies
├── runtime.txt                  Pins Python 3.12 for Railway's Nixpacks
├── railway.json                 Start command + healthcheck config
├── .env.example                 Copy to .env, fill in secrets
├── scripts/                     One-shot CLIs (ingestion, translation, etc.)
│   ├── ingest_problems.py       JSONL -> public.problems (+ optional embeddings)
│   └── translate_problems.py    English problems -> Hungarian (+ optional embeddings)
└── app/
    ├── api/                     Thin HTTP layer — only glue code
    │   ├── deps.py              Shared FastAPI dependencies
    │   ├── health.py            GET /health
    │   ├── sessions.py          CRUD for tutor_sessions
    │   ├── chat.py              POST /chat — SSE streaming
    │   └── problems.py          GET /problems/search — semantic search
    ├── core/
    │   ├── config.py            Pydantic-settings (env loader)
    │   └── security.py          Supabase JWT verification
    ├── db/
    │   ├── supabase.py          service_role client (bypasses RLS)
    │   ├── schemas.py           Pydantic row/payload models
    │   └── repositories.py      All DB access lives here
    ├── llm/
    │   ├── base.py              LLMClient abstract interface
    │   ├── openai_client.py     OpenAI implementation
    │   └── __init__.py          get_llm_client() factory
    ├── embeddings/
    │   ├── openai_embeddings.py text-embedding-3-small client (1536 dims)
    │   └── __init__.py          get_embeddings_client() factory
    ├── prompts/
    │   ├── tutor_v1.py          Frozen v1 system prompt
    │   ├── tutor_v2.py          v2 prompt (Socratic + mode awareness)
    │   └── __init__.py          CURRENT_TUTOR_PROMPT pointer
    └── agents/
        └── tutor.py             Orchestration — the tutor's "brain"
```

## How a chat turn flows

```
Browser                FastAPI                      Postgres           OpenAI
   │  POST /chat          │                            │                 │
   │ ───────────────────▶ │  verify JWT                │                 │
   │                      │  list_messages(session) ──▶│                 │
   │                      │  append user msg ─────────▶│                 │
   │                      │  stream_chat(context) ──────────────────────▶│
   │                      │ ◀──── tokens ──────────────────────────────── │
   │ ◀── SSE 'token' ──── │                            │                 │
   │ ◀── SSE 'token' ──── │                            │                 │
   │        ...           │                            │                 │
   │                      │  append assistant msg ────▶│                 │
   │                      │  (first turn?) title ────────────────────────▶│
   │                      │ ◀── title ──────────────────────────────────  │
   │ ◀── SSE 'title' ──── │  update_session_title ────▶│                 │
   │ ◀── SSE 'done'  ──── │                            │                 │
```

Key design rules:

- API routes never import from `llm/` or `prompts/`. They go through `agents/`.
- Database access always goes through `db/repositories.py`. No raw Supabase
  calls scattered around.
- Swapping LLM provider = changing the one line in `app/llm/__init__.py`.
- Bumping the tutor prompt = new file `prompts/tutor_v2.py`, change the
  `CURRENT_TUTOR_PROMPT` re-export.

## Local development

### 1. Create a virtualenv

```powershell
cd backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

(On macOS/Linux: `python3.12 -m venv .venv && source .venv/bin/activate`.)

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Create `.env`

Copy `.env.example` → `.env` and fill in:

- **`SUPABASE_URL`** — Supabase Dashboard → Project Settings → API.
- **`SUPABASE_SERVICE_ROLE_KEY`** — same page, "service_role" row. This key
  bypasses Row Level Security, so do not paste it anywhere public.
- **`OPENAI_API_KEY`** — platform.openai.com → API keys.

User tokens are verified by fetching Supabase's public keys from
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json` — no shared JWT secret
is needed. Supabase must be on asymmetric signing keys (ES256 or RS256),
which is the default for modern projects.

Leave the rest as-is unless you have a reason.

### 4. Run it

```powershell
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs for interactive OpenAPI docs.

### 5. Smoke test

```powershell
# Health
curl http://localhost:8000/health
# → {"status":"ok"}

# Authenticated endpoints need a Supabase access token. Easiest way to get
# one: log into studai.hu in the browser, open DevTools → Application →
# Local Storage → copy the access_token from the supabase auth entry.
# Or: temporarily print `data.session.access_token` in the frontend.

$token = "eyJ..."  # paste the access_token

# List your sessions (will be empty at first)
curl -H "Authorization: Bearer $token" http://localhost:8000/sessions

# Create a session
curl -X POST -H "Authorization: Bearer $token" `
     -H "Content-Type: application/json" `
     -d '{}' http://localhost:8000/sessions

# Chat (streaming — will print SSE frames)
curl -N -X POST -H "Authorization: Bearer $token" `
     -H "Content-Type: application/json" `
     -d '{"session_id":"<uuid-from-above>","message":"Hello, who are you?"}' `
     http://localhost:8000/chat
```

## Deploying to Railway

Railway builds from the monorepo using Nixpacks. The Next.js frontend stays
on Vercel; Railway only touches the `backend/` directory.

### 1. Create the service

1. Log into railway.com → **New Project** → **Deploy from GitHub repo** →
   pick this repo.
2. After it creates the service, open it → **Settings** → **Source** →
   set **Root Directory** to `backend`. This tells Nixpacks to only look at
   `backend/` and makes `uvicorn main:app` resolve correctly.
3. Railway reads `railway.json`, so the start command and healthcheck are
   already configured. You should see it attempt to build on first deploy.

### 2. Set environment variables

Open **Variables** tab and paste everything from your local `.env` file.
Railway will restart the service automatically.

### 3. Generate a public domain

**Settings** → **Networking** → **Generate Domain**. You'll get something
like `studai-backend-production.up.railway.app`. Verify:

```
curl https://studai-backend-production.up.railway.app/health
# → {"status":"ok"}
```

### 4. (Later) Point `api.studai.hu` at it

In your domain registrar, add a `CNAME` record:
`api` → `studai-backend-production.up.railway.app`.

Back in Railway **Networking**, add `api.studai.hu` as a custom domain.
Then update the frontend's API base URL env var and `CORS_ORIGINS` on the
backend.

## CORS

By default the backend trusts `https://studai.hu` and `http://localhost:3000`.
If you run the frontend on a different origin (preview deployments,
different port), add it to `CORS_ORIGINS` as a comma-separated list.

## Problem bank (Phase 8)

The backend ships ~18,000 worked math problems (MATH/Hendrycks +
GSM8K + ASDiv + SVAMP) into Supabase, with vector embeddings so the
tutor agent can find pedagogically relevant content instead of
inventing problems from scratch. See `scripts/README.md` for the full
ingestion + translation pipeline; the short version is:

```powershell
# 1) One-time DB migration in Supabase SQL editor
#    Run sql/003_problem_bank.sql

# 2) Validate the dataset parses (no DB writes, no API calls)
python -m scripts.ingest_problems --dry-run

# 3) Ingest English text into public.problems
python -m scripts.ingest_problems

# 4) Generate English embeddings (~EUR 10 for the full corpus)
python -m scripts.ingest_problems --embed

# 5) Try the search endpoint
curl -H "Authorization: Bearer $token" `
     "http://localhost:8000/problems/search?q=quadratic+equation&limit=5"
```

Hungarian translation is a separate, opt-in pass (~EUR 540 for the full
corpus, far less for slices). See `scripts/README.md`.

## What's NOT here (yet)

- Structured logging / error tracking — stdout only for now.
- Rate limiting — add once we see real traffic.
- Persistent session summaries — added when context windows get tight.
- Tools (SymPy, progress tracking) — added in the agents layer.
- Tutor agent does not yet *call* `/problems/search` — that's Phase 10.
