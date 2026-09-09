"""Evaluation sets (section 18.6).

What measures whether the analysis is trustworthy, run before any change to a
prompt, a model or a provider ships.
"""

from ai.evaluation.hallucination import (
    VAGUE_CASES,
    Case,
    Outcome,
    hallucination_rate,
    report_lines,
    run,
)

__all__ = [
    "VAGUE_CASES",
    "Case",
    "Outcome",
    "hallucination_rate",
    "report_lines",
    "run",
]
