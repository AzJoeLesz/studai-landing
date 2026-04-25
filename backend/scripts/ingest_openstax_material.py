"""Chunk + embed OpenStax text from `books_extracted/` into Supabase.

Prerequisites:
  1) Run `sql/005_teaching_material_and_annotations.sql` in Supabase.
  2) Run `python -m scripts.extract_books` so `books_extracted/en/openstax_algebra/`
     contains per-page `.md` files.

Usage (from `backend/`, venv active):

    # Ingest all extracted books under openstax_algebra
    python -m scripts.ingest_openstax_material

    # One book (substring of folder name, e.g. "prealgebra")
    python -m scripts.ingest_openstax_material --book prealgebra --rebuild

    # Dry run: print chunk counts only
    python -m scripts.ingest_openstax_material --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import repositories as repo
from app.embeddings.openai_embeddings import OpenAIEmbeddingsClient

EXTRACT_ROOT = REPO_ROOT / "books_extracted" / "en" / "openstax_algebra"
SOURCE = "openstax"
CHUNK_CHARS = 2800
EMBED_BATCH = 200


def _sorted_page_files(pages_dir: Path) -> list[Path]:
    return sorted(pages_dir.glob("*.md"), key=lambda p: p.name)


def _page_num(path: Path) -> int:
    m = re.match(r"^(\d+)\.md$", path.name)
    return int(m.group(1)) if m else 0


def chunk_book(pages_dir: Path) -> list[dict]:
    """Build chunk dicts: book_slug, chunk_index, page_start, page_end, body."""
    book_slug = pages_dir.parent.name
    files = _sorted_page_files(pages_dir)
    if not files:
        return []

    pieces: list[tuple[int, str]] = []
    for fp in files:
        text = fp.read_text(encoding="utf-8").strip()
        if not text:
            continue
        pieces.append((_page_num(fp), text))

    if not pieces:
        return []

    chunks: list[dict] = []
    buf: list[str] = []
    page_start = pieces[0][0]
    page_end = pieces[0][0]

    def flush(idx: int) -> None:
        nonlocal buf, page_start, page_end
        body = "\n\n".join(buf).strip()
        buf = []
        if not body:
            return
        chunks.append(
            {
                "source": SOURCE,
                "book_slug": book_slug,
                "chunk_index": idx,
                "page_start": page_start,
                "page_end": page_end,
                "body": body,
            }
        )

    chunk_index = 0
    for pn, text in pieces:
        if not buf:
            page_start = pn

        if len(text) > CHUNK_CHARS:
            if buf:
                flush(chunk_index)
                chunk_index += 1
            chunks.append(
                {
                    "source": SOURCE,
                    "book_slug": book_slug,
                    "chunk_index": chunk_index,
                    "page_start": pn,
                    "page_end": pn,
                    "body": text,
                }
            )
            chunk_index += 1
            buf = []
            continue

        candidate = "\n\n".join([*buf, text]) if buf else text
        if buf and len(candidate) > CHUNK_CHARS:
            flush(chunk_index)
            chunk_index += 1
            buf = [text]
            page_start = pn
            page_end = pn
        else:
            buf.append(text)
            page_end = pn

    if buf:
        flush(chunk_index)

    return chunks


async def embed_missing_for_book(
    client: OpenAIEmbeddingsClient, book_slug: str | None
) -> int:
    total = 0
    while True:
        rows = repo.list_teaching_chunks_missing_embedding(book_slug, EMBED_BATCH)
        if not rows:
            break
        bodies = [r["body"] for r in rows]
        vecs = await client.embed_concurrent(bodies, concurrency=4)
        pairs: list[tuple[UUID, list[float]]] = []
        for r, v in zip(rows, vecs, strict=True):
            pairs.append((UUID(r["id"]), v))
        n = repo.insert_teaching_embeddings(pairs, chunk_size=100)
        total += n
    return total


def discover_books() -> list[Path]:
    if not EXTRACT_ROOT.is_dir():
        return []
    return sorted(
        [p for p in EXTRACT_ROOT.iterdir() if p.is_dir() and (p / "pages").is_dir()],
        key=lambda x: x.name.lower(),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--book",
        default=None,
        help="Substring of book folder name; default = all under openstax_algebra.",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete existing rows for matching book_slug before insert.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show chunk counts only, no database or API calls.",
    )
    args = ap.parse_args()

    books = discover_books()
    if args.book:
        low = args.book.lower()
        books = [b for b in books if low in b.name.lower()]

    if not books:
        print(f"No extracted books under {EXTRACT_ROOT} (run extract_books first).")
        return 2

    if args.dry_run:
        for book_dir in books:
            print(f"  chunking {book_dir.name} ...", flush=True)
            n = len(chunk_book(book_dir / "pages"))
            print(f"  {book_dir.name}: {n} chunks (target <= {CHUNK_CHARS} chars)", flush=True)
        return 0

    client = OpenAIEmbeddingsClient()
    for book_dir in books:
        book_slug = book_dir.name
        pages_dir = book_dir / "pages"
        chunk_rows = chunk_book(pages_dir)
        print(f"{book_slug}: {len(chunk_rows)} chunks")
        if not chunk_rows:
            print(f"  skip (empty): {book_slug}")
            continue

        if args.rebuild:
            print(f"  delete existing chunks: {book_slug}")
            repo.delete_teaching_chunks_for_book(SOURCE, book_slug)

        # Upsert in batches (Supabase payload size)
        batch_size = 100
        for i in range(0, len(chunk_rows), batch_size):
            batch = chunk_rows[i : i + batch_size]
            repo.upsert_teaching_chunks([dict(x) for x in batch])

        print(f"  embedded rows -> {book_slug} ...", flush=True)
        n = asyncio.run(embed_missing_for_book(client, book_slug))
        print(f"  done embeddings for {book_slug} (+{n} attempts)")

    print("Final pass: embed any stragglers (all books)...", flush=True)
    n2 = asyncio.run(embed_missing_for_book(client, None))
    print(f"  done (+{n2} attempts)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
