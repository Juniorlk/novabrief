"""Meeting endpoints (EF-40, section 17.2).

Thin over `app.services.meetings`. The status never appears in a request body:
it moves through the state machine, which is the whole point of having one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.config import Settings, get_settings
from app.deps import CurrentCaller, ScopedSession
from app.errors import ProblemError
from app.models import MeetingStatus
from app.presenters import meeting_summary
from app.services import meetings
from app.storage import StorageProvider
from schemas.meetings import (
    DeclareMeetingRequest,
    FinalizeRequest,
    FinalizeUploadRequest,
    MeetingState,
    MeetingSummary,
    UpdateMeetingRequest,
    UploadPart,
    UploadTicket,
)

router = APIRouter(tags=["meetings"])

_STATUS_FOR_CODE = {
    "MEETING_NOT_FOUND": 404,
    "FORBIDDEN": 403,
    "ILLEGAL_TRANSITION": 409,
    "UPLOAD_NOT_STARTED": 409,
    "UPLOAD_INCOMPLETE": 409,
    "SIZE_MISMATCH": 422,
    "STORAGE_UNAVAILABLE": 503,
}


def dispatch_pipeline(request: Request) -> meetings.Dispatch:
    """How a queued meeting reaches the workers.

    Resolved through the app so a test can replace it with a recorder. Imported
    inside the function on purpose: importing the Celery task at module scope
    would make every API process build a broker connection it never uses.
    """
    override: meetings.Dispatch | None = getattr(request.app.state, "dispatch", None)
    if override is not None:
        return override

    def send(*, organization_id: uuid.UUID, meeting_id: uuid.UUID) -> None:
        from app.tasks.pipeline import transcribe_meeting

        transcribe_meeting.delay(str(organization_id), str(meeting_id))

    return send


Dispatcher = Annotated[meetings.Dispatch, Depends(dispatch_pipeline)]


def storage_provider(request: Request) -> StorageProvider:
    """The object store, attached to the app at startup."""
    provider: StorageProvider | None = getattr(request.app.state, "storage", None)
    if provider is None:  # pragma: no cover - only if the app was built wrongly
        raise ProblemError(
            status_code=503,
            code="STORAGE_UNAVAILABLE",
            title="Object storage is not configured.",
        )
    return provider


Storage = Annotated[StorageProvider, Depends(storage_provider)]


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


@router.post(
    "/meetings/{meeting_id}/finalize-local",
    response_model=UploadTicket,
    summary="Hand back presigned slots for the recording",
)
async def finalize_local(
    meeting_id: uuid.UUID,
    payload: FinalizeUploadRequest,
    caller: CurrentCaller,
    session: ScopedSession,
    storage: Storage,
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadTicket:
    """Section 16.4: the local manifest goes in, upload slots come out.

    The audio never passes through this API. The client writes straight to the
    bucket with the URLs below, which is what keeps a small server from
    becoming a file proxy for hundreds of megabytes.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
        upload = await meetings.start_upload(
            session,
            storage=storage,
            meeting=meeting,
            caller=caller.user,
            size_bytes=payload.size_bytes,
            sha256=payload.sha256,
            duration_seconds=payload.duration_seconds,
            paused_seconds=payload.paused_seconds,
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error

    return UploadTicket(
        upload_id=upload.upload_id,
        part_size_bytes=upload.part_size_bytes,
        parts=[UploadPart(part_number=part.part_number, url=part.url) for part in upload.parts],
        expires_in_seconds=settings.r2_presign_ttl_seconds,
    )


@router.post(
    "/meetings/{meeting_id}/finalize",
    response_model=MeetingSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Close the upload and queue the processing",
)
async def finalize(
    meeting_id: uuid.UUID,
    payload: FinalizeRequest,
    caller: CurrentCaller,
    session: ScopedSession,
    storage: Storage,
    dispatch: Dispatcher,
) -> MeetingSummary:
    """EF-40: 202 in under 500 ms, and the user never waits on a request.

    The size the store reports is checked against the size declared at
    `finalize-local`. A truncated upload is refused rather than transcribed
    into a confident, incomplete report.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
        queued = await meetings.finalize(
            session,
            storage=storage,
            organization=caller.organization,
            meeting=meeting,
            caller=caller.user,
            upload_id=payload.upload_id,
            parts=[(part.part_number, part.etag) for part in payload.parts],
            client_version=payload.client_version,
            dispatch=dispatch,
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
    return meeting_summary(queued)


@router.post(
    "/meetings/{meeting_id}/cancel",
    response_model=MeetingSummary,
    summary="Abandon a recording before it is queued",
)
async def cancel(
    meeting_id: uuid.UUID,
    caller: CurrentCaller,
    session: ScopedSession,
    storage: Storage,
) -> MeetingSummary:
    """The author changed their mind, or the recording was a mistake."""
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id)
        cancelled = await meetings.abandon(
            session, storage=storage, meeting=meeting, caller=caller.user
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
    return meeting_summary(cancelled)
