"""What the analysis must produce (ADR-03, section 18.4).

This schema is the contract, not the prompt. The prompt asks; this decides.
A model that returns something else has failed, and its answer is an error
rather than a degraded result — that is the whole of ADR-03, and it is what
stops a plausible-looking hallucination from reaching a customer.

The bounds are not arbitrary. `confidence` starts at 0.5 because the prompt
tells the model not to extract below it: a value under 0.5 arriving here means
the model ignored the instruction, and the schema catches it. `source_start_ms`
must land inside a real segment, so every decision and task is clickable
(EF-43) and none can be invented from nothing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    """An arbitration the meeting explicitly settled.

    Not an idea, not a hypothesis, not an unfinished debate. The distinction is
    the single most important thing this product gets right or wrong: a report
    that turns "on pourrait" into a decision is worse than no report.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=8, max_length=400)
    source_start_ms: int = Field(ge=0)
    confidence: float = Field(ge=0.5, le=1.0)


class Task(BaseModel):
    """A concrete action somebody took on."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=5, max_length=300)
    # Only when a name is actually spoken. Never inferred: guessing who is
    # responsible is how a report starts an argument.
    assignee: str | None = Field(default=None, max_length=200)
    # The words used, verbatim. The model does not compute dates; a calendar
    # does, later, and getting that wrong silently is worse than leaving it.
    deadline_text: str | None = Field(default=None, max_length=120)
    source_start_ms: int = Field(ge=0)
    confidence: float = Field(ge=0.5, le=1.0)


class MeetingReport(BaseModel):
    """The structured report (EF-42)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=4, max_length=120)
    language: Literal["fr", "en"]
    participants: list[str] = Field(default_factory=list, max_length=30)
    summary: list[str] = Field(min_length=1, max_length=6)
    decisions: list[Decision] = Field(default_factory=list, max_length=40)
    tasks: list[Task] = Field(default_factory=list, max_length=60)

    @property
    def sources(self) -> list[int]:
        """Every timestamp this report points at."""
        timestamps = [decision.source_start_ms for decision in self.decisions]
        timestamps.extend(task.source_start_ms for task in self.tasks)
        return timestamps


class SegmentWindow(BaseModel):
    """One stretch of transcript a source timestamp may fall into."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class UnsourcedItemError(ValueError):
    """A decision or task points at a moment that is not in the transcript.

    Which means the model invented it, or invented the timestamp. Either way
    the item cannot be shown: EF-43 promises every extracted element resolves
    to a passage the user can play.
    """

    def __init__(self, timestamps: list[int]) -> None:
        self.timestamps = timestamps
        listed = ", ".join(str(value) for value in timestamps[:5])
        super().__init__(f"extracted items point at no transcript segment: {listed}")


def check_sources(report: MeetingReport, windows: list[SegmentWindow]) -> None:
    """Refuse a report whose items point nowhere (section 18.4, EF-43).

    Checked here rather than in a validator on the model because it needs the
    transcript, which the schema has no business carrying. A tolerance is
    allowed at the edges: providers round timestamps, and rejecting a report
    over a few milliseconds would fail meetings that are perfectly good.
    """
    if not windows:
        return

    tolerance_ms = 1000
    orphans = [
        timestamp
        for timestamp in report.sources
        if not any(
            window.start_ms - tolerance_ms <= timestamp <= window.end_ms + tolerance_ms
            for window in windows
        )
    ]
    if orphans:
        raise UnsourcedItemError(orphans)
