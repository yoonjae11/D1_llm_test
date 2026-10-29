"""UTF-8 prompt resources used by the chatbot runtime.

Prompt text lives in ``.txt`` files so it can be reviewed and changed without
mixing model instructions into node implementations.  Use the helpers exported
here rather than opening resource paths directly.
"""

from .loader import load_chat_prompt, load_prompt, load_role_prompt, split_role_prompt

__all__ = [
    "load_chat_prompt",
    "load_prompt",
    "load_role_prompt",
    "split_role_prompt",
]
