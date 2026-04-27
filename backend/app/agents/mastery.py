"""Hybrid BKT + IRT mastery model.

Per `docs/phase9_personalization.md`. State representation is BKT
(`student_progress.mastery_score in [0,1]`); the update rule is BKT-IDEM
(Pardos & Heffernan 2011) with item-difficulty-effect modulation of the
guess and slip probabilities; item selection (placement quiz, future
"what to show next") is IRT-style: pick a problem near the current
ability `theta = logit(mastery_score)`.

This module is intentionally small. It does NOT import from agents/
besides standard libs and the repository layer; cycles are easy to
introduce when state_updater.py imports mastery.py and vice versa.

Citations:
  * Corbett & Anderson 1994 -- original BKT.
  * Pardos & Heffernan 2011 -- KT-IDEM (item-difficulty effect).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from app.db import repositories as repo
from app.db.schemas import EvidenceSource, StudentProgress

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (live in code, not config, until we have data to fit them)
# ---------------------------------------------------------------------------
P_T = 0.10  # transit per attempt -- chance of mastering between attempts

# Difficulty mapping: the corpus has a free-text `difficulty` column;
# we map common labels to numeric `b`. Unknown labels default to 0
# (medium).
_DIFFICULTY_NUMERIC = {
    "easy":   -1.0,
    "level 1": -1.0,
    "level 2": -0.5,
    "medium":  0.0,
    "level 3": 0.0,
    "level 4": 0.5,
    "hard":    1.0,
    "level 5": 1.0,
    "easy_medium": -0.5,
}

# Source weight: how strongly this evidence type moves mastery. The
# extractor is noisy LLM-derived signal; placement and step-checks are
# clean and get full weight; ratings are intermediate.
_SOURCE_WEIGHT: dict[EvidenceSource, float] = {
    "prior":      1.0,   # only used for seeding, but harmless if reused
    "placement":  1.0,
    "extractor":  0.30,
    "rating":     0.50,
    "step_check": 1.0,
}


def _difficulty_to_b(label: str | None) -> float:
    if not label:
        return 0.0
    key = label.strip().lower()
    return _DIFFICULTY_NUMERIC.get(key, 0.0)


def _guess_prob(b: float) -> float:
    return max(0.05, min(0.40, 0.20 - 0.10 * b))


def _slip_prob(b: float) -> float:
    return max(0.05, min(0.40, 0.10 + 0.10 * b))


# ---------------------------------------------------------------------------
# Pure update rule
# ---------------------------------------------------------------------------
def _bkt_update(prior: float, *, correct: bool, b: float) -> float:
    """One BKT-IDEM step. Returns posterior mastery in [0,1]."""
    p_g = _guess_prob(b)
    p_s = _slip_prob(b)
    if correct:
        num = prior * (1 - p_s)
        den = prior * (1 - p_s) + (1 - prior) * p_g
    else:
        num = prior * p_s
        den = prior * p_s + (1 - prior) * (1 - p_g)
    if den <= 0:
        # Degenerate; keep the prior. Should not happen with the clip
        # ranges above.
        return prior
    posterior_after_obs = num / den
    posterior = posterior_after_obs + (1 - posterior_after_obs) * P_T
    return max(0.0, min(1.0, posterior))


@dataclass(frozen=True)
class UpdateOutcome:
    """Computed posterior, returned by `apply_update`."""

    topic: str
    prior: float
    posterior: float
    evidence_source: EvidenceSource
    weight: float


def _weighted_blend(prior: float, raw_posterior: float, weight: float) -> float:
    """Blend raw BKT posterior with the prior by `weight`.

    weight=1.0 -> trust the BKT update fully.
    weight=0.0 -> ignore the update (mastery unchanged).
    weight=0.3 -> noisy extractor signals -- nudge but don't lurch.
    """
    return max(0.0, min(1.0, prior + weight * (raw_posterior - prior)))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def apply_graded_update(
    *,
    user_id: UUID,
    topic: str,
    correct: bool,
    difficulty: str | None,
    evidence_source: EvidenceSource,
) -> UpdateOutcome:
    """Apply one BKT-IDEM update for a graded outcome (placement, step-check, rating).

    Reads the current mastery from `student_progress`, computes the new
    posterior, writes it back. The `evidence_source` decides the weight
    used to blend the raw posterior with the prior (see `_SOURCE_WEIGHT`).
    """
    weight = _SOURCE_WEIGHT.get(evidence_source, 0.5)
    b = _difficulty_to_b(difficulty)

    existing = repo.get_progress_for_topic(user_id, topic)
    prior = existing.mastery_score if existing else 0.5
    raw_posterior = _bkt_update(prior, correct=correct, b=b)
    final = _weighted_blend(prior, raw_posterior, weight)

    next_count = (existing.evidence_count if existing else 0) + 1
    repo.upsert_progress(
        user_id,
        topic,
        mastery_score=final,
        evidence_source=evidence_source,
        evidence_count=next_count,
    )
    return UpdateOutcome(
        topic=topic,
        prior=prior,
        posterior=final,
        evidence_source=evidence_source,
        weight=weight,
    )


def update_from_extractor(
    *, user_id: UUID, topic: str, delta: float
) -> UpdateOutcome | None:
    """Apply a noisy mastery signal from the post-turn extractor.

    The extractor returns `delta in [-1.0, 1.0]`. We convert that to a
    pseudo-observation: positive deltas -> "looked correct" with
    confidence proportional to |delta|; negative -> "looked wrong" with
    confidence proportional to |delta|. The conversion is symmetric.

    Internally this is implemented as a graded BKT-IDEM update with
    `evidence_source = 'extractor'` (which carries the 0.3 weight), and
    the magnitude of the signal further scales the effective weight.
    Tiny |delta| signals are dropped entirely to keep noise low.
    """
    if not topic:
        return None
    if abs(delta) < 0.2:
        return None

    correct = delta > 0
    # Difficulty unknown at extractor time -- treat as medium (b = 0).
    weight = _SOURCE_WEIGHT["extractor"] * min(1.0, abs(delta))
    b = 0.0

    existing = repo.get_progress_for_topic(user_id, topic)
    prior = existing.mastery_score if existing else 0.5
    raw_posterior = _bkt_update(prior, correct=correct, b=b)
    final = _weighted_blend(prior, raw_posterior, weight)

    next_count = (existing.evidence_count if existing else 0) + 1
    repo.upsert_progress(
        user_id,
        topic,
        mastery_score=final,
        evidence_source="extractor",
        evidence_count=next_count,
    )
    return UpdateOutcome(
        topic=topic,
        prior=prior,
        posterior=final,
        evidence_source="extractor",
        weight=weight,
    )


# ---------------------------------------------------------------------------
# IRT item selection (placement quiz)
# ---------------------------------------------------------------------------
_DIFFICULTY_BUCKETS: list[tuple[str, float]] = [
    ("easy", -1.0),
    ("medium", 0.0),
    ("hard", 1.0),
]


def mastery_to_theta(mastery_score: float) -> float:
    """Convert a [0,1] mastery into ability theta on the IRT scale.

    Clipped away from 0 and 1 to avoid -inf/+inf at the boundaries.
    """
    p = max(0.001, min(0.999, mastery_score))
    return math.log(p / (1 - p))


def pick_difficulty_for(mastery_score: float) -> str:
    """IRT-style: pick the discrete difficulty bucket nearest to theta.

    Returns one of: "easy", "medium", "hard".
    """
    theta = mastery_to_theta(mastery_score)
    best_label = "medium"
    best_dist = float("inf")
    for label, b in _DIFFICULTY_BUCKETS:
        dist = abs(b - theta)
        if dist < best_dist:
            best_dist = dist
            best_label = label
    return best_label


def next_difficulty_after_outcome(
    current: str, *, correct: bool
) -> str:
    """Adaptive staircase: right -> harder; wrong -> easier.

    Used by the placement quiz when we don't yet have a confident
    mastery estimate (the first 1-2 questions). Once a few outcomes have
    moved the score, `pick_difficulty_for(mastery)` is more principled.
    """
    order = ["easy", "medium", "hard"]
    try:
        idx = order.index(current.lower())
    except ValueError:
        idx = 1
    if correct and idx < len(order) - 1:
        return order[idx + 1]
    if not correct and idx > 0:
        return order[idx - 1]
    return order[idx]


__all__ = [
    "apply_graded_update",
    "update_from_extractor",
    "mastery_to_theta",
    "pick_difficulty_for",
    "next_difficulty_after_outcome",
    "UpdateOutcome",
]
