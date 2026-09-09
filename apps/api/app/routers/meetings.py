"""Meeting endpoints (EF-40, section 17.2).

Thin over `app.services.meetings`. The status never appears in a request body:
it moves through the state machine, which is the whole point of having one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.deps import CurrentCaller, ScopedSession
from app.errors import ProblemError
from app.models import MeetingStatus
from app.presenters import meeting_summary
from app.services import meetings
from schemas.meetings import (
    DeclareMeetingRequest,
    MeetingState,
    MeetingSummary,
    UpdateMeetingRequest,
)

router = APIRouter(tags=["meetings"])

_STATUS_FOR_CODE = {
    "MEETING_NOT_FOUND": 404,
    "FORBIDDEN": 403,
    "ILLEGAL_TRANSITION": 409,
}


def _as_problem(error: meetings.MeetingError) -> ProblemError:
    return ProblemError(
        status_code=_STATUS_FOR_CODE.get(error.code, 400),
        code=error.code,
        title=str(error),
    )


@router.post(
    "/meetings",
    response_model=MeetingSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Declare a meeting",
)
async def declare(
    payload: DeclareMeetingRequest,
    caller: CurrentCaller,
    session: ScopedSession,
) -> MeetingSummary:
    """EF-40: called when recording starts, and returns the `debug_id`."""
    meeting = await meetings.declare(
        session,
        organization=caller.organization,
        author=caller.user,
        started_at=payload.started_at,
        title=payload.title,
        is_private=payload.is_private,
    )
    return meeting_summary(meeting)


@router.get(
    "/meetings",
    response_model=list[MeetingSummary],
    summary="List the organization's meetings",
)
async def index(
    caller: CurrentCaller,
    session: ScopedSession,
    meeting_status: Annotated[MeetingState | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MeetingSummary]:
    """Newest first. RLS scopes this; no tenant filter is written here."""
    rows = await meetings.listing(
        session,
        status=MeetingStatus(meeting_status) if meeting_status else None,
        limit=limit,
        offset=offset,
    )
    return [meeting_summary(meeting) for meeting in rows]


@router.get(
    "/meetings/{meeting_id}",
    response_model=MeetingSummary,
    summary="One meeting",
)
async def show(
    meeting_id: uuid.UUID, caller: CurrentCaller, session: ScopedSession
) -> MeetingSummary:
    """A meeting in another organization answers 404, not 403.

    RLS makes the two indistinguishable, which is what stops the endpoint from
    confirming that an identifier exists somewhere else.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
    return meeting_summary(meeting)


@router.patch(
    "/meetings/{meeting_id}",
    response_model=MeetingSummary,
    summary="Edit a meeting",
)
async def edit(
    meeting_id: uuid.UUID,
    payload: UpdateMeetingRequest,
    caller: CurrentCaller,
    session: ScopedSession,
) -> MeetingSummary:
    """Author or administrator. Title and privacy only."""
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
        updated = await meetings.update(
            session,
            meeting=meeting,
            caller=caller.user,
            changes=payload.model_dump(exclude_unset=True),
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
    return meeting_summary(updated)


@router.delete(
    "/meetings/{meeting_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a meeting",
)
async def destroy(meeting_id: uuid.UUID, caller: CurrentCaller, session: ScopedSession) -> None:
    """Author or administrator.

    A state, not a `DELETE FROM`: the consumption record has to keep a meeting
    to point at (section 19.2).
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
        await meetings.remove(session, meeting=meeting, caller=caller.user)
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
