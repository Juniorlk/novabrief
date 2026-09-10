"""Meeting endpoints (EF-40, section 17.2).

Thin over `app.services.meetings`. The status never appears in a request body:
it moves through the state machine, which is the whole point of having one.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select

from app.config import Settings, get_settings
from app.deps import CurrentCaller, ScopedSession, after_commit
from app.errors import ProblemError
from app.models import Decision, Meeting, MeetingStatus, Report, Task, TranscriptSegment
from app.presenters import meeting_summary, report, transcript_segment
from app.services import meetings, transcription
from app.storage import StorageProvider
from app.tickets import (
    TICKET_TTL_SECONDS,
    TicketClaims,
    TicketStore,
    new_ticket,
)
from schemas.meetings import (
    DeclareMeetingRequest,
    FinalizeRequest,
    FinalizeUploadRequest,
    MeetingDetail,
    MeetingState,
    MeetingSummary,
    UpdateMeetingRequest,
    UploadPart,
    UploadTicket,
    WebSocketTicket,
)

router = APIRouter(tags=["meetings"])

_STATUS_FOR_CODE = {
    "MEETING_NOT_FOUND": 404,
    "NOT_FAILED": 409,
    "FORBIDDEN": 403,
    "ILLEGAL_TRANSITION": 409,
    "UPLOAD_NOT_STARTED": 409,
    "UPLOAD_INCOMPLETE": 409,
    "SIZE_MISMATCH": 422,
    "STORAGE_UNAVAILABLE": 503,
    "AUDIO_GONE": 409,
}


def dispatch_pipeline(request: Request) -> meetings.Dispatch:
    """How a meeting reaches the workers.

    Resolved through the app so a test can replace it with a recorder. Imported
    inside the function on purpose: importing the Celery task at module scope
    would make every API process build a broker connection it never uses.

    Two stages, because a retry does not always start from the beginning
    (`meetings.retry`). Sending an already-transcribed meeting back through
    transcription pays the supplier twice for bytes we already hold.
    """
    override: meetings.Dispatch | None = getattr(request.app.state, "dispatch", None)
    if override is not None:
        return override

    def send(*, organization_id: uuid.UUID, meeting_id: uuid.UUID, stage: meetings.Stage) -> None:
        if stage == "analyse":
            from app.tasks.analysis import analyse_meeting

            analyse_meeting.delay(str(organization_id), str(meeting_id))
            return

        from app.tasks.pipeline import transcribe_meeting

        transcribe_meeting.delay(str(organization_id), str(meeting_id))

    return send


Dispatcher = Annotated[meetings.Dispatch, Depends(dispatch_pipeline)]


def ticket_store(request: Request) -> TicketStore:
    """Where single-use WebSocket tickets live."""
    store: TicketStore | None = getattr(request.app.state, "tickets", None)
    if store is None:  # pragma: no cover - only if the app was built wrongly
        raise ProblemError(
            status_code=503,
            code="TICKETS_UNAVAILABLE",
            title="Realtime status is not configured.",
        )
    return store


Tickets = Annotated[TicketStore, Depends(ticket_store)]


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


def _queue(
    request: Request,
    dispatch: meetings.Dispatch,
    *,
    meeting: Meeting,
    stage: meetings.Stage,
) -> None:
    """Hand the meeting to the workers once this transaction has committed.

    Handed off rather than run inline: EF-40 promises a 202 in under 500 ms and
    the user never waits on a request. Deferred rather than sent immediately
    because the row the worker needs does not exist outside this transaction
    yet - see `app.deps.after_commit`.
    """
    organization_id = meeting.organization_id
    identifier = meeting.id
    after_commit(
        request,
        lambda: dispatch(organization_id=organization_id, meeting_id=identifier, stage=stage),
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
    """Newest first.

    RLS scopes this to the organization; no tenant filter is written here. The
    caller is passed for the one thing RLS cannot express — a colleague's
    private meeting is inside the same tenant and still must not appear.
    """
    rows = await meetings.listing(
        session,
        caller=caller.user,
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
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
    request: Request,
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
        queued = await meetings.finalize(
            session,
            storage=storage,
            organization=caller.organization,
            meeting=meeting,
            caller=caller.user,
            upload_id=payload.upload_id,
            parts=[(part.part_number, part.etag) for part in payload.parts],
            client_version=payload.client_version,
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error

    if queued.status == MeetingStatus.QUEUED.value:
        # After the commit, never before: a worker reading this row while the
        # transaction is still open sees UPLOADING, declines to act, and the
        # meeting waits in QUEUED for a message that has already been consumed.
        _queue(request, dispatch, meeting=queued, stage="transcribe")
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
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
        cancelled = await meetings.abandon(
            session, storage=storage, meeting=meeting, caller=caller.user
        )
    except meetings.MeetingError as error:
        raise _as_problem(error) from error
    return meeting_summary(cancelled)


@router.get(
    "/meetings/{meeting_id}/report",
    response_model=MeetingDetail,
    summary="The report, the transcript and a link to the audio",
)
async def detail(
    meeting_id: uuid.UUID,
    caller: CurrentCaller,
    session: ScopedSession,
    storage: Storage,
) -> MeetingDetail:
    """Everything the player needs (EF-52).

    The audio link is presigned and short-lived, and absent once the recording
    has been purged (ADR-06) — a null there is a normal answer, not an error.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
    except meetings.MeetingError as error:
        raise _as_problem(error) from error

    transcript = await transcription.existing_transcript(session, meeting_id=meeting_id)
    segments: list[TranscriptSegment] = []
    if transcript is not None:
        segments = list(
            (
                await session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.transcript_id == transcript.id)
                    .order_by(TranscriptSegment.start_ms)
                )
            ).all()
        )

    row = await session.scalar(select(Report).where(Report.meeting_id == meeting_id))
    rendered = None
    if row is not None:
        decisions = list(
            (
                await session.scalars(
                    select(Decision)
                    .where(Decision.meeting_id == meeting_id)
                    .order_by(Decision.source_start_ms)
                )
            ).all()
        )
        tasks = list(
            (
                await session.scalars(
                    select(Task).where(Task.meeting_id == meeting_id).order_by(Task.source_start_ms)
                )
            ).all()
        )
        rendered = report(row, decisions=decisions, tasks=tasks)

    audio_url = None
    if meeting.audio_key:
        audio_url = await storage.presign_get(key=meeting.audio_key)

    return MeetingDetail(
        meeting=meeting_summary(meeting),
        report=rendered,
        segments=[transcript_segment(segment) for segment in segments],
        audio_url=audio_url,
    )


@router.post(
    "/meetings/{meeting_id}/retry",
    response_model=MeetingSummary,
    summary="Retry a failed meeting",
)
async def retry(
    request: Request,
    meeting_id: uuid.UUID,
    caller: CurrentCaller,
    session: ScopedSession,
    dispatch: Dispatcher,
) -> MeetingSummary:
    """EF-45's Retry button. Author or administrator, and only from FAILED.

    Resumes at the stage that failed rather than from the top: a meeting whose
    transcript survived goes straight back to analysis.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
        requeued, stage = await meetings.retry(session, meeting=meeting, caller=caller.user)
    except meetings.MeetingError as error:
        raise _as_problem(error) from error

    _queue(request, dispatch, meeting=requeued, stage=stage)
    return meeting_summary(requeued)


@router.post(
    "/meetings/{meeting_id}/ws-ticket",
    response_model=WebSocketTicket,
    summary="A single-use ticket for the status socket",
)
async def issue_ticket(
    meeting_id: uuid.UUID,
    caller: CurrentCaller,
    session: ScopedSession,
    tickets: Tickets,
) -> WebSocketTicket:
    """Section 17.2.

    A browser cannot set an Authorization header on a WebSocket, and a token in
    a query string ends up in every access log. This grants one thing, once,
    for sixty seconds: watching this meeting's status.
    """
    try:
        meeting = await meetings.get(session, meeting_id=meeting_id, caller=caller.user)
    except meetings.MeetingError as error:
        raise _as_problem(error) from error

    ticket = new_ticket()
    await tickets.issue(
        ticket,
        TicketClaims(
            organization_id=caller.organization.id,
            user_id=caller.user.id,
            meeting_id=meeting.id,
        ),
    )
    return WebSocketTicket(ticket=ticket, expires_in_seconds=TICKET_TTL_SECONDS)
