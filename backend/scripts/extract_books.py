"""Extract text from text-extractable (born-digital) PDFs in books/.

Output layout, per book:

    books_extracted/<lang>/<series>/<book-slug>/
      ├── full.md           one big markdown blob (whole book)
      ├── pages/0001.md     one file per page, lossless
      └── meta.json         page count, suspicious-page flags, source path

Why per-page files: chunking, embedding, and citation later become
trivial. We can always concatenate them, but we can't easily un-merge.

This script ONLY handles digital PDFs. Scan-only PDFs are skipped with
a clear log line; run `scripts/ocr_scans.py` for those.

Usage (from backend/, venv active):

    # extract three sample books (default sample list, ~100 MB output):
    python -m scripts.extract_books --sample

    # extract everything digital (~14k pages, ~150-300 MB of markdown):
    python -m scripts.extract_books

    # extract one specific book:
    python -m scripts.extract_books --only "Sokszinu Matematika 7"
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOKS_ROOT = REPO_ROOT / "books"
OUT_ROOT = REPO_ROOT / "books_extracted"

# A page is suspicious if:
#   * it has unusually few characters compared to the book's typical page, OR
#   * it has high "weird character" density (private-use Unicode, control chars).
SUSPICIOUS_LOW_CHAR_RATIO = 0.2  # < 20% of median page length
SUSPICIOUS_WEIRD_RATIO = 0.05  # > 5% weird chars

# A book is treated as scan-only if its median sampled-page char count is
# below this. We keep this lower than probe_books.py so we don't accidentally
# skip a digital book that's just light on text near our sample points.
DIGITAL_MIN_MEDIAN_CHARS = 80

# Sample books to extract first for quality spot-check (official OpenStax WEB PDFs).
DEFAULT_SAMPLES = [
    "prealgebra-2e_-_WEB.pdf",
    "algebra-1_-_WEB.pdf",
    "elementary-algebra-2e_-_WEB.pdf",
]


# --- Helpers ---------------------------------------------------------------


def slugify(text: str) -> str:
    """Make a folder/file-safe slug from a path component."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return cleaned or "untitled"


def book_slug(pdf: Path) -> str:
    """Slug derived from the file name minus z-library noise."""
    stem = pdf.stem
    stem = re.sub(r"\(z-library\.sk[^\)]*\)", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\(1lib[^\)]*\)", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\(z-lib[^\)]*\)", "", stem, flags=re.IGNORECASE)
    return slugify(stem)


def out_dir_for(pdf: Path) -> Path:
    """Mirror books/<lang>/<series>/<file>.pdf -> books_extracted/<lang>/<series>/<slug>/."""
    rel = pdf.relative_to(BOOKS_ROOT)
    parts = rel.parts  # e.g. ('hu', 'sokszinu_matematika', 'file.pdf')
    if len(parts) >= 3:
        lang, series, *_ = parts
    elif len(parts) == 2:
        lang, series = parts[0], "_root_"
    else:
        lang, series = "_unknown_", "_root_"
    return OUT_ROOT / lang / series / book_slug(pdf)


# --- Page text quality -----------------------------------------------------


_PRIVATE_USE_OR_CTRL = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ue000-\uf8ff]"
)


def weird_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = len(_PRIVATE_USE_OR_CTRL.findall(text))
    return bad / len(text)


@dataclass
class PageReport:
    index: int
    chars: int
    weird_ratio: float
    suspicious: bool
    reason: str | None


def evaluate_pages(pages_text: list[str]) -> tuple[list[PageReport], int]:
    """Decide which pages look suspicious. Returns reports + median char count."""
    char_counts = [len(t) for t in pages_text]
    nonzero = [c for c in char_counts if c > 0]
    median = int(statistics.median(nonzero)) if nonzero else 0
    threshold = max(20, int(median * SUSPICIOUS_LOW_CHAR_RATIO))

    reports: list[PageReport] = []
    for i, t in enumerate(pages_text):
        chars = len(t)
        weird = weird_char_ratio(t)
        reasons: list[str] = []
        if median > 0 and chars < threshold and chars > 0:
            reasons.append(f"low_chars({chars}<{threshold})")
        if weird > SUSPICIOUS_WEIRD_RATIO:
            reasons.append(f"weird_chars({weird:.0%})")
        reports.append(
            PageReport(
                index=i,
                chars=chars,
                weird_ratio=weird,
                suspicious=bool(reasons),
                reason=", ".join(reasons) if reasons else None,
            )
        )
    return reports, median


# --- Extraction ------------------------------------------------------------


def extract_book(pdf: Path) -> dict | None:
    """Extract one book. Returns a summary dict, or None if it's a scan."""
    print(f"\n[{pdf.relative_to(BOOKS_ROOT)}]")
    out = out_dir_for(pdf)

    try:
        doc = fitz.open(pdf)
    except Exception as exc:
        print(f"  ERROR: cannot open: {exc}")
        return None

    pages_text: list[str] = []
    try:
        for i in range(len(doc)):
            try:
                pages_text.append(doc[i].get_text("text") or "")
            except Exception:
                pages_text.append("")
    finally:
        doc.close()

    reports, median_chars = evaluate_pages(pages_text)

    if median_chars < DIGITAL_MIN_MEDIAN_CHARS:
        print(
            f"  SKIP: looks like a scan (median {median_chars} chars/page)."
            f" Run scripts/ocr_scans.py for this one."
        )
        return None

    out.mkdir(parents=True, exist_ok=True)
    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)

    suspicious_pages: list[dict] = []
    full_buf: list[str] = []
    for i, text in enumerate(pages_text):
        per = pages_dir / f"{i + 1:04d}.md"
        per.write_text(text, encoding="utf-8")
        if reports[i].suspicious:
            suspicious_pages.append(
                {"page": i + 1, "reason": reports[i].reason}
            )
        full_buf.append(f"<!-- page {i + 1} -->\n\n{text}\n")

    (out / "full.md").write_text("\n".join(full_buf), encoding="utf-8")
    meta = {
        "source_pdf": str(pdf.relative_to(REPO_ROOT)).replace("\\", "/"),
        "pages": len(pages_text),
        "median_chars_per_page": median_chars,
        "suspicious_pages": suspicious_pages,
    }
    (out / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    n_susp = len(suspicious_pages)
    susp_pct = (n_susp / max(1, len(pages_text))) * 100
    print(
        f"  OK: {len(pages_text)} pages,"
        f" median {median_chars} chars/page,"
        f" {n_susp} suspicious ({susp_pct:.1f}%)"
    )
    print(f"  -> {out.relative_to(REPO_ROOT)}")
    return meta


# --- CLI -------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract text from digital PDFs under books/."
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--sample",
        action="store_true",
        help="Only extract the 3 default sample books for quality check.",
    )
    g.add_argument(
        "--only",
        default=None,
        help="Substring of a book filename to extract just that one.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not BOOKS_ROOT.is_dir():
        print(f"books/ not found at {BOOKS_ROOT}", file=sys.stderr)
        return 2

    all_pdfs = sorted(BOOKS_ROOT.rglob("*.pdf"))
    if not all_pdfs:
        print("No PDFs found.", file=sys.stderr)
        return 2

    if args.sample:
        targets = [
            p for p in all_pdfs
            if any(s.lower() in p.name.lower() for s in DEFAULT_SAMPLES)
        ]
        if not targets:
            print("Sample list matched no files. Adjust DEFAULT_SAMPLES.")
            return 2
    elif args.only:
        targets = [p for p in all_pdfs if args.only.lower() in p.name.lower()]
        if not targets:
            print(f"No book name contains: {args.only!r}")
            return 2
    else:
        targets = all_pdfs

    print(f"Extracting {len(targets)} of {len(all_pdfs)} PDFs.")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    summaries = []
    for pdf in targets:
        meta = extract_book(pdf)
        if meta is not None:
            summaries.append(meta)

    print()
    print(f"Done: extracted {len(summaries)} books to {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
