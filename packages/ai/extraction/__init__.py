"""The structured report and the rules it must satisfy (ADR-03)."""

from ai.extraction.schema import (
    Decision,
    MeetingReport,
    SegmentWindow,
    Task,
    UnsourcedItemError,
    check_sources,
)

__all__ = [
    "Decision",
    "MeetingReport",
    "SegmentWindow",
    "Task",
    "UnsourcedItemError",
    "check_sources",
]
