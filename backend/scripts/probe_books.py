"""Probe every book under books/ to learn:

  * total page count
  * average text length per sampled page
  * whether the file is "text-extractable" (born-digital) or "scan-only"
  * estimated MB-per-page (a proxy for image weight)

Output is a small report you can copy/paste into a planning doc.
Use it to decide which books need OCR vs which can be ingested directly.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import fitz  # PyMuPDF


BOOKS_ROOT = Path(__file__).resolve().parent.parent.parent / "books"
SAMPLE_PAGES = 5  # we sample roughly evenly across the document
MIN_TEXT_PER_PAGE_FOR_DIGITAL = 200  # chars; below this we suspect scan


def sample_indices(total: int, n: int) -> list[int]:
    if total <= n:
        return list(range(total))
    # spread n picks across the doc, but skip the very first page (cover/title)
    # and the very last (often blank or back-cover).
    step = max(1, (total - 2) // n)
    return [min(total - 1, 1 + i * step) for i in range(n)]


def probe(path: Path) -> dict:
    size_mb = path.stat().st_size / (1024 * 1024)
    try:
        with fitz.open(path) as doc:
            n_pages = len(doc)
            sampled_lens: list[int] = []
            for i in sample_indices(n_pages, SAMPLE_PAGES):
                try:
                    text = doc[i].get_text("text") or ""
                except Exception:
                    text = ""
                sampled_lens.append(len(text.strip()))
        median = int(statistics.median(sampled_lens)) if sampled_lens else 0
        verdict = (
            "DIGITAL" if median >= MIN_TEXT_PER_PAGE_FOR_DIGITAL else "SCAN"
        )
        return {
            "path": path,
            "size_mb": size_mb,
            "pages": n_pages,
            "median_text_chars": median,
            "verdict": verdict,
            "mb_per_page": size_mb / max(1, n_pages),
            "error": None,
        }
    except Exception as exc:
        return {
            "path": path,
            "size_mb": size_mb,
            "pages": 0,
            "median_text_chars": 0,
            "verdict": "ERROR",
            "mb_per_page": 0.0,
            "error": str(exc),
        }


def main() -> int:
    if not BOOKS_ROOT.is_dir():
        print(f"books/ not found at {BOOKS_ROOT}", file=sys.stderr)
        return 1

    rows = []
    for pdf in sorted(BOOKS_ROOT.rglob("*.pdf")):
        rows.append(probe(pdf))

    rows.sort(key=lambda r: (r["verdict"], -r["size_mb"]))

    digital = [r for r in rows if r["verdict"] == "DIGITAL"]
    scans = [r for r in rows if r["verdict"] == "SCAN"]
    errors = [r for r in rows if r["verdict"] == "ERROR"]

    print()
    print(f"Probed {len(rows)} PDFs under {BOOKS_ROOT}")
    print(
        f"  -> {len(digital)} digital (text-extractable),"
        f" {len(scans)} scan-only,"
        f" {len(errors)} error"
    )
    print()

    print(
        "{:<7} {:>7} {:>7} {:>11} {:>7}  {}".format(
            "verdict", "size_MB", "pages", "MB/page", "txt/pg", "file"
        )
    )
    print("-" * 100)
    for r in rows:
        rel = r["path"].relative_to(BOOKS_ROOT)
        size = f"{r['size_mb']:.1f}"
        pages = str(r["pages"])
        mbpp = f"{r['mb_per_page']:.2f}"
        txt = str(r["median_text_chars"])
        line = f"{r['verdict']:<7} {size:>7} {pages:>7} {mbpp:>11} {txt:>7}  {rel}"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))

    if errors:
        print()
        print("Errors:")
        for r in errors:
            print(f"  {r['path'].name}: {r['error']}")

    # Per-bucket totals
    print()
    print("Totals by verdict:")
    for label, group in [("DIGITAL", digital), ("SCAN", scans)]:
        if not group:
            continue
        tot_mb = sum(r["size_mb"] for r in group)
        tot_pages = sum(r["pages"] for r in group)
        print(
            f"  {label:<7} {len(group):>2} files,"
            f" {tot_mb:>7.0f} MB, {tot_pages:>6} pages"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
