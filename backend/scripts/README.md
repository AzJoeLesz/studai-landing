# StudAI scripts

One-shot CLIs for content engineering. None of these are called by the
running app — they're for you to run from a developer machine when you
want to ingest data, translate it, or probe internals.

All scripts assume you have:

- The `backend/` virtualenv active.
- A working `.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
  `OPENAI_API_KEY`).
- Run them as modules from the `backend/` folder
  (`python -m scripts.<name>`), not as `python scripts/<name>.py`,
  so the `app.*` imports resolve.

## ingest_problems.py

Imports the JSONL math problem datasets in `math_problem_example/`
into the `public.problems` table.

### Quick start

```powershell
# Sanity-check parsing only — no DB writes, no API calls. Safe.
python -m scripts.ingest_problems --dry-run

# Ingest English text only (free)
python -m scripts.ingest_problems

# Ingest + embed (~EUR 10 for the full ~18k corpus)
python -m scripts.ingest_problems --embed
```

### Useful flags

- `--source gsm8k` — only ingest from one dataset.
- `--type Algebra` — only ingest one problem category.
- `--limit 50` — cap rows (use it with `--embed` to test the pipeline
  end-to-end for cents).
- `--input <path>` — point at a different dataset folder.
- `--embed-concurrency 8` — more parallel embedding requests
  (defaults to 4; OpenAI tolerates more if your tier allows).

### Idempotency

The script is safe to re-run. `public.problems` has a unique constraint
on `(source, source_id)`, and `--embed` only embeds problems that don't
already have an English embedding row.

## translate_problems.py

Reads the English problems out of `public.problems`, translates them
into the requested language with `gpt-4o-mini`, and writes the result to
`public.problem_translations`. Optionally also embeds the translation.

### Cost (gpt-4o-mini, April 2026)

| Step                               | Per problem | Full ~18k corpus |
|------------------------------------|-------------|-------------------|
| Translation (problem + solution)   | ~EUR 0.03   | ~EUR 540          |
| Embedding the translation          | ~EUR 0.0006 | ~EUR 11           |

Translate a slice first to sanity-check the quality before committing
to the full €540:

```powershell
# 5 random rows, ~EUR 0.15
python -m scripts.translate_problems --language hu --limit 5 --embed

# All Hungarian Algebra problems (~700 rows, ~EUR 21)
python -m scripts.translate_problems --language hu --type Algebra --embed

# Full corpus (~18k rows, ~EUR 550 incl. embedding)
python -m scripts.translate_problems --language hu --embed
```

Same flags as ingestion: `--source`, `--type`, `--limit`, `--concurrency`,
`--dry-run`.

### Quality

The translator preserves all LaTeX (`$...$`, `\boxed{}`, `\frac`, etc.)
verbatim and translates only the surrounding prose. People-name
translation is allowed (e.g. *Natalia* stays *Natalia*; *John* may
become *János*).

After a translation pass, spot-check ~20 rows manually before kicking
off the next big batch. The model occasionally translates a number
in prose into Hungarian words — usually OK, but worth confirming for
your audience.

## probe_jwks.py

Diagnostic: pulls Supabase's public JWT verification keys from the
`/auth/v1/.well-known/jwks.json` endpoint and prints them. Useful when
auth is failing in production and you want to confirm the live key
material the backend is verifying against.

```powershell
python -m scripts.probe_jwks
```
