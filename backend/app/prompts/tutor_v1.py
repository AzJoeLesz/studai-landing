"""Tutor system prompt, version 1.

The prompt text lives in `tutor_v1.txt` so eval tools and Python share a
single source of truth. Never edit this file's content semantics —
to change the prompt, create `tutor_v2.txt` + `tutor_v2.py` and bump
`CURRENT_TUTOR_PROMPT` in `app/prompts/__init__.py`.
"""

from pathlib import Path

_PROMPT_PATH = Path(__file__).with_suffix(".txt")
TUTOR_SYSTEM_PROMPT_V1 = _PROMPT_PATH.read_text(encoding="utf-8").strip()
