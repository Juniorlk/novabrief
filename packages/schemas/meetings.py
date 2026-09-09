"""Request and response shapes for meetings (lot L2).

Source of truth for the contract (ADR-10): the desktop client and the web app
generate their types from here, so the three cannot drift apart.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

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
    "DecisionOut",
    "DeclareMeetingRequest",
    "FinalizeRequest",
    "FinalizeUploadRequest",
    "MeetingDetail",
    "MeetingState",
    "MeetingStatusEvent",
    "MeetingSummary",
    "ReportOut",
    "TaskOut",
    "TranscriptSegmentOut",
    "UpdateMeetingRequest",
    "UploadPart",
    "UploadTicket",
    "UploadedPart",
    "WebSocketTicket",
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


# --------------------------------------------------------------------------
# Upload and finalisation (EF-40, section 16.4, section 17.2)
# --------------------------------------------------------------------------

# Hex, lower case, 64 characters. Written out rather than left as a plain `str`
# so a client sending a base64 digest is told at the boundary.
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FinalizeUploadRequest(_Base):
    """The local manifest: what the desktop is about to upload.

    Sent once the Opus file is encoded and hashed, before a single byte leaves
    the machine. The size is what determines how many presigned slots come
    back, so it has to be the real one.
    """

    size_bytes: int = Field(gt=0)
    sha256: Sha256
    duration_seconds: int = Field(ge=0)
    paused_seconds: int = Field(default=0, ge=0)


class UploadPart(_Base):
    """One presigned slot, to be filled with a chunk of the recording."""

    part_number: int = Field(ge=1)
    url: str


class UploadTicket(_Base):
    """Everything the client needs to upload, and nothing else.

    The URLs are bearer credentials with a short life: whoever holds one can
    write that part until it expires. They are handed to a caller who has
    already been authorised, and never logged.
    """

    upload_id: str
    part_size_bytes: int
    parts: list[UploadPart]
    expires_in_seconds: int


class UploadedPart(_Base):
    """What the store returned for one part, echoed back at finalisation."""

    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=128)


class FinalizeRequest(_Base):
    """Every part is uploaded; assemble the object and queue the work."""

    upload_id: str = Field(min_length=1, max_length=256)
    parts: list[UploadedPart] = Field(min_length=1)
    # Which build produced this recording. It costs nothing to record and is
    # the first thing worth knowing when one version starts failing.
    client_version: str | None = Field(default=None, max_length=32)


# --------------------------------------------------------------------------
# Reading a finished meeting (EF-42, EF-43, EF-52)
# --------------------------------------------------------------------------


class TranscriptSegmentOut(_Base):
    """One diarised passage, as the player shows it."""

    id: uuid.UUID
    speaker_tag: str
    speaker_name: str | None
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None
    channel: str | None


class DecisionOut(_Base):
    """A decision, with the moment it was taken."""

    id: uuid.UUID
    content: str
    # EF-43: clicking this plays the passage it came from.
    source_start_ms: int
    confidence: float
    human_status: str
    edited_content: str | None


class TaskOut(_Base):
    """A task, with what was said about its deadline and what that resolves to."""

    id: uuid.UUID
    action: str
    assignee_name: str | None
    assignee_user_id: uuid.UUID | None
    # Both, always: when the computed date is wrong the original wording is
    # the only way anyone can tell.
    deadline_text: str | None
    deadline_date: date | None
    source_start_ms: int
    confidence: float
    human_status: str
    edited_content: str | None


class ReportOut(_Base):
    """The structured report of one meeting."""

    id: uuid.UUID
    title: str
    participants: list[str]
    summary: list[str]
    decisions: list[DecisionOut]
    tasks: list[TaskOut]
    # Which model and which prompt produced it (section 18.6).
    model_version: str | None
    prompt_version: str | None
    generated_at: datetime


class MeetingDetail(_Base):
    """A meeting with everything the player needs."""

    meeting: MeetingSummary
    report: ReportOut | None
    segments: list[TranscriptSegmentOut]
    # Presigned and short-lived, absent once the audio has been purged
    # (ADR-06). Null is a normal answer, not an error.
    audio_url: str | None


class WebSocketTicket(_Base):
    """A single-use ticket for the status socket (section 17.2)."""

    ticket: str
    expires_in_seconds: int


class MeetingStatusEvent(_Base):
    """One frame on the status socket (EF-44)."""

    meeting_id: uuid.UUID
    status: MeetingState
    # Null when there is nothing useful to say. A made-up number that keeps
    # slipping is worse than no estimate at all.
    estimated_seconds_remaining: int | None = None
    failed_reason: str | None = None
