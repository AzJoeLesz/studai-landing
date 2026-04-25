"""Tutor system prompt, version 2.

v2 adds:
  * Explicit intent/mode detection (problem-solving vs concept vs
    verification vs conversational).
  * Detailed mistake-handling protocol (identify → locate → explain → retry).
  * Single-move-per-turn rule (AutoTutor research).
  * Inference-not-evaluative question guidance (Vail et al. 2016).
  * Anti-patterns list (empty praise, answer leaking, language switching).

The prompt text lives in `tutor_v2.txt`.  To iterate further, create
`tutor_v3.txt` + `tutor_v3.py` and bump `CURRENT_TUTOR_PROMPT`.
"""

from pathlib import Path

_PROMPT_PATH = Path(__file__).with_suffix(".txt")
TUTOR_SYSTEM_PROMPT_V2 = _PROMPT_PATH.read_text(encoding="utf-8").strip()
