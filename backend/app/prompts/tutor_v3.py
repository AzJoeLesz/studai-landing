"""Tutor system prompt, version 3.

v3 adds (Phase 9 — personalization & adaptation):
  * Awareness of three new private context blocks the agent layer
    injects: STYLE DIRECTIVES, STUDENT PROGRESS, SESSION STATE.
  * A "How to read STYLE DIRECTIVES" section that defines each of the
    seven directive values in concrete tutoring terms.
  * A `register` directive with explicit recipes for above-level
    exploration (the 3rd-grader-asks-about-quadratics case),
    below-level warmup, and remedial.
  * Memory privacy rules: never recite profile/state/progress to the
    student; use them to inform choices, not narrate them.

The prompt text lives in `tutor_v3.txt`. To iterate, create
`tutor_v4.txt` + `tutor_v4.py` and bump `CURRENT_TUTOR_PROMPT`.
"""

from pathlib import Path

_PROMPT_PATH = Path(__file__).with_suffix(".txt")
TUTOR_SYSTEM_PROMPT_V3 = _PROMPT_PATH.read_text(encoding="utf-8").strip()
