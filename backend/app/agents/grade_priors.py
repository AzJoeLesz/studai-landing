"""Loader and helpers for `backend/app/data/grade_priors.json`.

One module so `style_policy`, `topic_classifier`, and the seed-priors API
endpoint share the same canonical mapping. The JSON is read once at
import time and cached.

What this exposes:
  * `resolve_grade_band(grade_level: str) -> (curriculum, band) | None`
  * `priors_for(curriculum, band) -> dict[topic, mastery]`
  * `canonicalize_topic(label: str) -> str`  -- folds HU labels and
    common spellings to canonical English topic keys
  * `expected_mastery(topic, curriculum, band) -> float`  -- 0.0 means
    "not in the curriculum at this band" (= above level)
  * `topic_universe() -> set[str]`  -- every canonical topic across all
    curricula and bands; used to seed the topic classifier centroids
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "grade_priors.json"
)


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    raw = _data().get("topic_aliases", {})
    return {k.lower().strip(): v for k, v in raw.items() if k != "comment"}


@lru_cache(maxsize=1)
def _resolver_patterns() -> list[dict]:
    raw = _data().get("grade_level_resolver", {}).get("patterns", [])
    # Resolver is order-sensitive; longer/more-specific matches first.
    # The JSON already orders them well; we just preserve order.
    return list(raw)


def canonicalize_topic(label: str | None) -> str | None:
    """Lowercase, collapse whitespace, alias-fold to canonical English."""
    if not label:
        return None
    cleaned = re.sub(r"\s+", " ", label).strip().lower()
    if not cleaned:
        return None
    return _aliases().get(cleaned, cleaned)[:120]


def resolve_grade_band(
    grade_level: str | None,
) -> tuple[str, str] | None:
    """Map a free-text grade_level to (curriculum, grade_band) or None.

    Examples:
      "9. évfolyam"            -> ("hu_nat", "9-10")
      "Grade 7"                 -> ("us_ccss", "6-8")
      "University 2nd year"     -> ("us_ccss", "university")
      "egyetem 2. év"          -> ("hu_nat", "university")
      "4th grade"              -> ("us_ccss", "3-5")
      "4 osztály"              -> ("hu_nat", "3-5")
      "4"                       -> ("us_ccss", "3-5")  -- generic numeric fallback
      None / "" / unrecognized  -> None
    """
    if not grade_level:
        return None
    needle = grade_level.strip().lower()
    if not needle:
        return None

    # Pass 1: regex extraction. We try this BEFORE the substring table
    # because the regexes capture both the number and the noun (more
    # specific), while substring matches on bare nouns like "osztály"
    # or "évfolyam" fall back to a single default band (less specific).
    #
    # Hungarian: number 1-12 followed by an osztály/évfolyam variant
    # (with or without an accent on é). The number is the source of
    # truth for the band; the noun just confirms the language.
    m = re.search(
        r"\b(\d{1,2})\b\s*\.?\s*(osztály|osztaly|évfolyam|evfolyam|oszt|évf|evf)\b",
        needle,
    )
    if m:
        n = int(m.group(1))
        return ("hu_nat", _band_for_grade_number(n))

    # English: "4th grade", "grade 4", "year 9", etc.
    m = re.search(r"\bgrade\s*(\d{1,2})\b", needle)
    if m:
        return ("us_ccss", _band_for_grade_number(int(m.group(1))))
    m = re.search(r"\byear\s*(\d{1,2})\b", needle)
    if m:
        return ("us_ccss", _band_for_grade_number(int(m.group(1))))
    m = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*grade\b", needle)
    if m:
        return ("us_ccss", _band_for_grade_number(int(m.group(1))))

    # Spelled-out English ordinals: "fourth grade".
    spelled = {
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
        "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
        "eleventh": 11, "twelfth": 12,
    }
    for word, n in spelled.items():
        if re.search(rf"\b{word}\b", needle):
            return ("us_ccss", _band_for_grade_number(n))

    # Pass 2: substring table. Catches the curated patterns
    # ("kindergarten", "phd", "egyetem", etc.) that the regex pass
    # didn't try to handle.
    for entry in _resolver_patterns():
        token = str(entry.get("match", "")).lower()
        if token and token in needle:
            return (entry["curriculum"], entry["band"])

    # Pass 3: bare integer 1-12 (or with "th"/"nd"/"rd"/"st" suffix).
    # Defaults to us_ccss because Hungarian users almost always include
    # a noun ("évfolyam", "osztály"), so a lone "9" is more likely to
    # be an English speaker dropping the noun.
    m = re.fullmatch(r"\s*(\d{1,2})\s*(?:st|nd|rd|th)?\s*", needle)
    if m:
        return ("us_ccss", _band_for_grade_number(int(m.group(1))))

    return None


def _band_for_grade_number(grade: int) -> str:
    """Map a grade integer (1-12) to one of our band labels.

    University -> university. Grades 1-2 -> K-2. 3-5 -> 3-5. Etc.
    """
    if grade <= 2:
        return "K-2"
    if grade <= 5:
        return "3-5"
    if grade <= 8:
        return "6-8"
    if grade <= 10:
        return "9-10"
    if grade <= 12:
        return "11-12"
    return "university"


def band_for_age(age: int | None) -> str | None:
    """Coarse age -> band fallback when grade_level is missing/unparseable.

    Used by the seed-priors endpoint so a fresh user with a typo in
    `grade_level` still gets reasonable priors as long as `age` is set.
    """
    if age is None:
        return None
    if age <= 7:
        return "K-2"
    if age <= 10:
        return "3-5"
    if age <= 13:
        return "6-8"
    if age <= 15:
        return "9-10"
    if age <= 18:
        return "11-12"
    return "university"


@lru_cache(maxsize=64)
def priors_for(curriculum: str, band: str) -> dict[str, float]:
    """Per-band priors with topic keys canonicalized to English.

    The on-disk JSON intentionally keeps Hungarian labels for `hu_nat` so
    a curriculum reviewer can read it; we canonicalize at read time so
    callers can look up by the same English keys regardless of source
    curriculum. If two source labels map to the same canonical key (rare
    but possible), the higher prior wins -- a deliberate "best-of"
    choice for cross-curriculum coverage.
    """
    raw = dict(_data().get(curriculum, {}).get(band, {}))
    out: dict[str, float] = {}
    for label, mastery in raw.items():
        canon = canonicalize_topic(label)
        if not canon:
            continue
        try:
            value = float(mastery)
        except (TypeError, ValueError):
            continue
        if canon not in out or value > out[canon]:
            out[canon] = value
    return out


def expected_mastery(
    topic: str | None, curriculum: str | None, band: str | None
) -> float:
    """How mastered should a 'typical' student of (curriculum, band) be on `topic`?

    Returns 0.0 if the topic is not in the curriculum at that band, which
    we interpret as 'above-level' from the student's perspective. Used by
    the topic-grade alignment check in `style_policy.derive_directives`.
    """
    canon = canonicalize_topic(topic)
    if not (canon and curriculum and band):
        return 0.0
    return float(priors_for(curriculum, band).get(canon, 0.0))


@lru_cache(maxsize=1)
def topic_universe() -> tuple[str, ...]:
    """All canonical topic strings appearing anywhere in the priors table.

    Sorted, deduplicated. Centroids for these are computed by
    `agents/topic_classifier.py`.
    """
    seen: set[str] = set()
    for curriculum, bands in _data().items():
        if curriculum.startswith("_") or curriculum in (
            "topic_aliases",
            "grade_level_resolver",
        ):
            continue
        if not isinstance(bands, dict):
            continue
        for band_topics in bands.values():
            if not isinstance(band_topics, dict):
                continue
            for topic in band_topics.keys():
                canon = canonicalize_topic(topic)
                if canon:
                    seen.add(canon)
    return tuple(sorted(seen))


def grade_priors_seed(
    grade_level: str | None,
    *,
    age: int | None = None,
) -> list[tuple[str, float]]:
    """Build the (topic, mastery) list to seed `student_progress` for a user.

    Includes topics from THIS band and the band immediately below
    (because students don't forget what they learned last year, and the
    tutor benefits from knowing they have it). Below-band topics are
    written at min(0.95, prior + 0.1) capped at 0.95.

    Resolution order:
      1. Try to map grade_level (e.g. "9. évfolyam", "Grade 7") to
         (curriculum, band).
      2. If that fails but `age` is set, fall back to age-derived band.
         Curriculum defaults to us_ccss in that case (see
         `band_for_age`).
      3. If both fail, return [] (no priors seeded -- tutor still works,
         just without grade-derived calibration).
    """
    resolved = resolve_grade_band(grade_level)
    curriculum: str | None
    band: str | None
    if resolved:
        curriculum, band = resolved
    else:
        band = band_for_age(age)
        curriculum = "us_ccss" if band else None

    if not (curriculum and band):
        return []

    bands_order = ["K-2", "3-5", "6-8", "9-10", "11-12", "university"]
    if band not in bands_order:
        return []
    idx = bands_order.index(band)

    seed: dict[str, float] = {}
    if idx > 0:
        prev_band = bands_order[idx - 1]
        for topic, mastery in priors_for(curriculum, prev_band).items():
            canon = canonicalize_topic(topic)
            if canon:
                seed[canon] = min(0.95, mastery + 0.10)
    for topic, mastery in priors_for(curriculum, band).items():
        canon = canonicalize_topic(topic)
        if canon:
            # Same-band priors win over below-band bumps when both exist.
            seed[canon] = mastery

    return sorted(seed.items(), key=lambda kv: kv[0])
