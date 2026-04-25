"""System prompt versions.

Each file in this package is a frozen version. Never edit an existing
version file — create a new one (`tutor_v3.py`) and bump `CURRENT_TUTOR_PROMPT`
below. This way we can always answer 'what prompt produced this message?'
"""

from app.prompts.tutor_v1 import TUTOR_SYSTEM_PROMPT_V1
from app.prompts.tutor_v2 import TUTOR_SYSTEM_PROMPT_V2

CURRENT_TUTOR_PROMPT = TUTOR_SYSTEM_PROMPT_V2

__all__ = [
    "CURRENT_TUTOR_PROMPT",
    "TUTOR_SYSTEM_PROMPT_V1",
    "TUTOR_SYSTEM_PROMPT_V2",
]
