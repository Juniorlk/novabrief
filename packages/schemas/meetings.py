"""Request and response shapes for meetings (lot L2).

Source of truth for the contract (ADR-10): the desktop client and the web app
generate their types from here, so the three cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The server-side states of section 11. The desktop's own states (RECORDING,
# PAUSED, FINALIZING_LOCAL…) are deliberately absent: the API never observes
# them and must not pretend to.
MeetingState = Literal[
    "CREATED",
    "UPLOADING",
    "QUEUED",
    "QUOTA_HOLD",
    "TRANSCRIBING",
    "FALLBACK_STT",
    "ANALYZING",
    "COMPLETED",
    "PUBLISHED",
    "AUDIO_PURGED",
    "FAILED",
    "CANCELLED",
    "DELETED",
]

__all__ = [
    "DeclareMeetingRequest",
    "MeetingState",
    "MeetingSummary",
    "UpdateMeetingRequest",
]


class _Base(BaseModel):
    """Shared configuration: reject unknown fields."""

    # A typo in a client payload should be a visible 422, not a value silently
    # ignored while the caller believes it was applied.
    model_config = ConfigDict(extra="forbid")


class DeclareMeetingRequest(_Base):
    """EF-40: the desktop announces that a recording has started.

    Sent at the beginning, not the end. A laptop that dies mid-meeting then
    still leaves a row to reconcile against.
    """

    started_at: datetime
    # Optional: most recordings start before anyone has thought of a name, and
    # the title is usually written by the extraction (EF-42) anyway.
    title: str | None = Field(default=None, min_length=1, max_length=200)
    is_private: bool = False


class UpdateMeetingRequest(_Base):
    """What a human may change about a meeting.

    Status is absent on purpose: it moves through the state machine, never
    through a request body.
    """

    title: str | None = Field(default=None, max_length=200)
    is_private: bool | None = None


class MeetingSummary(_Base):
    """A meeting as the API returns it."""

    id: uuid.UUID
    title: str | None
    status: MeetingState
    language: str | None
    started_at: datetime
    duration_seconds: int
    is_private: bool
    # ADR-07: quoted in a support ticket, it ties the desktop, the API, the
    # workers and the provider calls together.
    debug_id: str
    created_by: uuid.UUID
    failed_reason: str | None
    purge_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
