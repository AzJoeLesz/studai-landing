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
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Placement profile (which corpus subset is age-appropriate?)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacementProfile:
    """Defines which corpus subset to draw placement-quiz problems from.

    Why per-band: the global source allowlist
    (``hendrycks``/``gsm8k``/``openstax``) was too coarse for the
    placement quiz. Hendrycks "Level 1" is still 9th-grade AMC
    competition math — way above a 4th grader. So the placement
    quiz now picks sources by band:

        K-2 / 3-5 / 6-8  -> gsm8k only (grade-school word problems)
        9-10             -> hendrycks (Levels 1-3) + gsm8k
        11-12            -> hendrycks (Levels 2-5) + openstax
        university       -> hendrycks (Levels 3-5)

    `difficulty_map`, when not None, overrides the default
    `mastery.corpus_difficulties_for()` mapping. We override for
    bands where Hendrycks dominates because "easy/medium/hard" mean
    very different things inside vs outside Hendrycks: an "easy"
    Hendrycks problem (Level 1) is still a hard 9th-grade problem.
    """

    sources: tuple[str, ...]
    difficulty_map: dict[str, list[str]] | None = None


_DEFAULT_PROFILE = PlacementProfile(
    sources=("hendrycks", "gsm8k"),
    difficulty_map=None,
)


def placement_profile_for_band(band: str | None) -> PlacementProfile:
    """Pick the right corpus subset for a student of `band`.

    Unknown / missing band -> a permissive default that still excludes
    the noisy synthetic datasets (asdiv, svamp, mawps).
    """
    if band in ("K-2", "3-5", "6-8"):
        # Grade school. Hendrycks at any difficulty is too advanced.
        return PlacementProfile(sources=("gsm8k",), difficulty_map=None)
    if band == "9-10":
        return PlacementProfile(
            sources=("hendrycks", "gsm8k"),
            difficulty_map={
                "easy":   ["Level 1"],
                "medium": ["Level 1", "Level 2"],
                "hard":   ["Level 2", "Level 3"],
            },
        )
    if band == "11-12":
        return PlacementProfile(
            sources=("hendrycks", "openstax"),
            difficulty_map={
                "easy":   ["Level 2"],
                "medium": ["Level 3"],
                "hard":   ["Level 4", "Level 5"],
            },
        )
    if band == "university":
        return PlacementProfile(
            sources=("hendrycks",),
            difficulty_map={
                "easy":   ["Level 3"],
                "medium": ["Level 4"],
                "hard":   ["Level 5"],
            },
        )
    return _DEFAULT_PROFILE


def placement_profile_for_user(
    grade_level: str | None, age: int | None
) -> PlacementProfile:
    """Convenience: profile from whatever the student gave us.

    Resolution order: parse `grade_level` -> band; else `age` -> band;
    else default profile. Mirrors `grade_priors_seed`.
    """
    resolved = resolve_grade_band(grade_level)
    if resolved:
        _, band = resolved
        return placement_profile_for_band(band)
    band = band_for_age(age)
    return placement_profile_for_band(band)


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

    Returns 0.0 if the topic is not in the curriculum at that band.
    Note: 0.0 here is ambiguous on its own — it could mean either
    "above level (not yet introduced)" or "below level (assumed
    mastered, dropped from new-topics list)". Use `topic_band_status`
    for that distinction; this function only tells you the prior.
    """
    canon = canonicalize_topic(topic)
    if not (canon and curriculum and band):
        return 0.0
    return float(priors_for(curriculum, band).get(canon, 0.0))


# ---------------------------------------------------------------------------
# Topic vs band relationship
# ---------------------------------------------------------------------------
_BANDS_ORDER: tuple[str, ...] = (
    "K-2", "3-5", "6-8", "9-10", "11-12", "university",
)


def topic_band_status(
    topic: str | None,
    curriculum: str | None,
    band: str | None,
) -> str:
    """Where does this topic sit relative to the student's band?

    Returns one of:
      * `"at_level"` -- the topic appears in the student's band's
        priors (it is part of their current curriculum).
      * `"above_level"` -- the topic appears ONLY in higher bands
        (e.g. 4th grader asking about quadratic functions).
      * `"below_level"` -- the topic appears ONLY in lower bands
        (e.g. 12th grader asking about basic addition).
      * `"unknown"` -- the topic isn't in the priors table for this
        curriculum at all (caller should treat as `at_level`).

    Why this matters: my previous code used `expected_mastery == 0.0`
    as a proxy for "above level". That was wrong -- a topic gets a
    prior of 0.0 in two opposite cases: (a) it hasn't been introduced
    yet at that band (above), and (b) it was introduced earlier and
    dropped from the new-topics list (below). A 12th grader asking
    "what is 7 + 8?" was incorrectly getting `above_level_exploration`
    because addition isn't in the 11-12 priors. This function fixes
    that by walking the band order.
    """
    canon = canonicalize_topic(topic)
    if not (canon and curriculum and band) or band not in _BANDS_ORDER:
        return "unknown"
    student_idx = _BANDS_ORDER.index(band)

    if canon in priors_for(curriculum, band):
        return "at_level"

    in_above = any(
        canon in priors_for(curriculum, _BANDS_ORDER[i])
        for i in range(student_idx + 1, len(_BANDS_ORDER))
    )
    in_below = any(
        canon in priors_for(curriculum, _BANDS_ORDER[i])
        for i in range(0, student_idx)
    )
    if in_above:
        return "above_level"
    if in_below:
        return "below_level"
    return "unknown"


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
