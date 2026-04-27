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
      None / "" / unrecognized  -> None
    """
    if not grade_level:
        return None
    needle = grade_level.strip().lower()
    if not needle:
        return None
    for entry in _resolver_patterns():
        token = str(entry.get("match", "")).lower()
        if token and token in needle:
            return (entry["curriculum"], entry["band"])
    return None


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
) -> list[tuple[str, float]]:
    """Build the (topic, mastery) list to seed `student_progress` for a user.

    Includes topics from THIS band and the band immediately below
    (because students don't forget what they learned last year, and the
    tutor benefits from knowing they have it). Below-band topics are
    written at min(0.95, prior + 0.1) capped at 0.95.
    """
    resolved = resolve_grade_band(grade_level)
    if not resolved:
        return []
    curriculum, band = resolved

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
