"""Loading the versioned prompts (section 18.3, ADR-01).

Read from `packages/ai/prompts/` rather than embedded in the code, because
section 18.6 requires every prompt change to be evaluated before it ships and a
prompt buried in a Python string is a prompt nobody reviews as a prompt.

`PROMPT_VERSION` selects the file. The version that produced a report is
written next to it, so a regression can be traced to the prompt that caused it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import ai

_PROMPTS = Path(ai.__file__).resolve().parent / "prompts"
_BLOCK = re.compile(r"^## (?P<name>\w+)\s*$.*?^```\s*$(?P<body>.*?)^```\s*$", re.M | re.S)


class PromptNotFoundError(RuntimeError):
    """The configured prompt version does not exist.

    Loud rather than falling back to another version: a worker silently
    running last month's prompt would produce reports nobody could explain.
    """


@lru_cache(maxsize=8)
def load(version: str) -> dict[str, str]:
    """The blocks of one prompt file, keyed by their heading."""
    path = _PROMPTS / f"{version}.md"
    if not path.is_file():
        message = f"prompt version {version!r} is not in {_PROMPTS}"
        raise PromptNotFoundError(message)

    text = path.read_text(encoding="utf-8")
    blocks = {
        match.group("name").lower(): match.group("body").strip() for match in _BLOCK.finditer(text)
    }
    missing = {"system", "repair"} - set(blocks)
    if missing:
        message = f"prompt version {version!r} is missing: {', '.join(sorted(missing))}"
        raise PromptNotFoundError(message)
    return blocks
