"""LLM-based answer judge for the placement quiz (Phase 9E refinement).

Self-grading (✓ / I-don't-know) gave a worthless signal — kids tap the
checkmark either way. This module replaces it with a tiny LLM call that
takes (problem, canonical answer, student answer) and returns a bool.

Same fire-and-call pattern as `tutor._check_answer_leak`: cheap model,
short prompt, single-token output. Failures are non-fatal: if the LLM
errors, we fall back to a strict normalized-string compare so we never
strand a placement attempt.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from app.core.config import get_settings
from app.db.schemas import MessageInput
from app.llm import get_llm_client

logger = logging.getLogger(__name__)


_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "placement_judge_v1.txt"
)
_JUDGE_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

# Phrases that mean "I don't know" in either supported UI language. We
# short-circuit before paying the LLM call -- the answer is unambiguous.
_DONT_KNOW_TOKENS = {
    "",
    "?",
    "??",
    "skip",
    "idk",
    "i don't know",
    "i dont know",
    "dont know",
    "no idea",
    "nem tudom",
    "nem",
    "kihagy",
    "passz",
    "nincs",
}


def _normalize(text: str) -> str:
    """Lowercase + strip + drop accents + collapse whitespace.

    Used both for the dont-know short-circuit and as the fallback path
    when the LLM call fails.
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", no_accents).strip().lower()


def _is_dont_know(student_answer: str) -> bool:
    return _normalize(student_answer) in _DONT_KNOW_TOKENS


def _strict_fallback_match(
    student_answer: str, canonical_answer: str | None
) -> bool:
    """Last-resort: did the student write the canonical answer verbatim?

    Used only when the LLM judge call raises. We strip currency
    symbols, units, equals-signs and the like and require an exact
    normalized-string match. Conservative -- false negatives are
    much better than false positives in a placement context.
    """
    if not canonical_answer:
        return False
    s = re.sub(r"[\$\s,]", "", _normalize(student_answer))
    c = re.sub(r"[\$\s,]", "", _normalize(canonical_answer))
    if not s or not c:
        return False
    return s == c


async def judge_answer(
    *,
    problem_text: str,
    canonical_answer: str | None,
    student_answer: str,
) -> bool:
    """Return True iff the student's answer is mathematically equivalent.

    Order:
      1. If `student_answer` is empty / 'skip' / 'I don't know' (any
         supported phrasing) -> NO immediately.
      2. If we have no canonical answer to compare against -> NO. (We
         don't try to judge correctness from the problem alone.)
      3. Call the LLM judge with the system prompt above.
      4. On any error, fall back to a strict normalized-string compare.
    """
    if _is_dont_know(student_answer):
        return False
    if not canonical_answer:
        return False

    settings = get_settings()
    llm = get_llm_client()

    user_payload = (
        f"PROBLEM:\n{problem_text[:2000]}\n\n"
        f"CANONICAL ANSWER:\n{canonical_answer[:500]}\n\n"
        f"STUDENT ANSWER:\n{student_answer[:500]}"
    )
    messages = [
        MessageInput(role="system", content=_JUDGE_SYSTEM_PROMPT),
        MessageInput(role="user", content=user_payload),
    ]

    try:
        verdict = await llm.complete(
            messages,
            model=settings.placement_judge_model,
            max_tokens=3,
        )
        verdict = (verdict or "").strip().upper()
        if verdict.startswith("YES"):
            return True
        if verdict.startswith("NO"):
            return False
        # Garbage output -- fall back rather than guess.
        logger.debug("answer_judge: unexpected verdict %r", verdict)
    except Exception:
        logger.warning(
            "answer_judge: LLM call failed; using strict fallback",
            exc_info=True,
        )

    return _strict_fallback_match(student_answer, canonical_answer)
