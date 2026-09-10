"""The life cycle of a meeting (section 11, EF-40).

The state machine is a **table**, not a chain of conditionals. Section 11 draws
thirteen server states and about twenty legal moves between them; expressed as
`if` statements spread across the workers, the illegal moves would be the ones
nobody wrote down and therefore nobody tested. Written as data, the illegal
moves are simply everything absent from the table, and `advance` is the only
way to change a status.

That single entry point matters more than the table. It is what guarantees that
every transition is timestamped and audited (section 11 requires both), and it
is why no worker can set `status = 'COMPLETED'` on a meeting that never left
the queue.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.debug_id import new_debug_id
from app.logging import get_logger
from app.models import ActorType, AuditLog, Meeting, MeetingStatus, Organization, Role, User
from app.services import usage
from app.storage import MultipartUpload, StorageError, StorageProvider, audio_key
from app.uuid7 import uuid7

logger = get_logger(__name__)


class Dispatch(Protocol):
    """Hands a queued meeting to the workers.

    A callable rather than an import of the Celery task: the service must not
    depend on the queue, so tests can watch what would have been dispatched
    without a broker anywhere in sight.
    """

    def __call__(self, *, organization_id: uuid.UUID, meeting_id: uuid.UUID) -> None: ...


_S = MeetingStatus

# Every legal move, straight from the diagram in section 11. Read it as "from
# this state, these are the only places a meeting may go next".
TRANSITIONS: Mapping[MeetingStatus, frozenset[MeetingStatus]] = {
    # The desktop has declared the meeting; nothing has been uploaded yet.
    _S.CREATED: frozenset({_S.UPLOADING, _S.CANCELLED, _S.DELETED}),
    # Segments are arriving. QUOTA_HOLD is reached here rather than later: the
    # audio is safely stored, only the processing waits for payment.
    _S.UPLOADING: frozenset({_S.QUEUED, _S.QUOTA_HOLD, _S.CANCELLED, _S.DELETED}),
    _S.QUOTA_HOLD: frozenset({_S.QUEUED, _S.CANCELLED, _S.DELETED}),
    _S.QUEUED: frozenset({_S.TRANSCRIBING, _S.FAILED, _S.DELETED}),
    # EF-45: the fallback provider takes over on 5xx, timeout or open breaker.
    _S.TRANSCRIBING: frozenset({_S.ANALYZING, _S.FALLBACK_STT, _S.FAILED, _S.DELETED}),
    _S.FALLBACK_STT: frozenset({_S.ANALYZING, _S.FAILED, _S.DELETED}),
    _S.ANALYZING: frozenset({_S.COMPLETED, _S.FAILED, _S.DELETED}),
    _S.COMPLETED: frozenset({_S.PUBLISHED, _S.DELETED}),
    # Only here is the quota decremented and the cost recorded (section 11).
    _S.PUBLISHED: frozenset({_S.AUDIO_PURGED, _S.DELETED}),
    _S.AUDIO_PURGED: frozenset({_S.DELETED}),
    # EF-45 gives a "Retry" button, which sends the meeting back to the queue.
    _S.FAILED: frozenset({_S.QUEUED, _S.DELETED}),
    # Terminal.
    _S.CANCELLED: frozenset(),
    _S.DELETED: frozenset(),
}

# States from which no work is outstanding. Used to refuse a retry on something
# that was never in trouble.
TERMINAL: frozenset[MeetingStatus] = frozenset({_S.CANCELLED, _S.DELETED, _S.AUDIO_PURGED})


class MeetingError(Exception):
    """A meeting operation was refused. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class IllegalTransitionError(MeetingError):
    """The move is not in the table.

    A separate class because this one is a programming error rather than a
    client mistake: it means a worker tried to move a meeting somewhere section
    11 does not allow, and it should be loud.
    """

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(
            "ILLEGAL_TRANSITION",
            f"a meeting cannot move from {current} to {target}",
        )


def allowed_from(status: str) -> frozenset[MeetingStatus]:
    """Where a meeting in this state may go next."""
    try:
        return TRANSITIONS[MeetingStatus(status)]
    except ValueError:  # pragma: no cover - the column is constrained
        return frozenset()


async def advance(
    session: AsyncSession,
    *,
    meeting: Meeting,
    to: MeetingStatus,
    actor: User | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Meeting:
    """Move a meeting to `to`, or refuse.

    The only way a status changes. `actor` is None for the workers, which act
    as the system rather than on anybody's behalf.

    Section 11 asks that every transition be logged with a timestamp and an
    actor; that happens here, once, instead of being remembered at twenty call
    sites.
    """
    if to not in allowed_from(meeting.status):
        raise IllegalTransitionError(meeting.status, to.value)

    previous = meeting.status
    meeting.status = to.value

    now = datetime.now(UTC)
    if to is MeetingStatus.COMPLETED:
        meeting.completed_at = now
    if to is MeetingStatus.FAILED:
        # A stable code, never the provider's message: those quote the payload
        # they choked on, which is meeting content.
        meeting.failed_reason = reason
    if to is MeetingStatus.AUDIO_PURGED:
        meeting.purged_at = now
        meeting.audio_key = None

    entry: dict[str, Any] = {"from": previous, "to": to.value}
    if reason:
        entry["reason"] = reason
    if metadata:
        entry.update(metadata)

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=meeting.organization_id,
            actor_id=actor.id if actor else None,
            actor_type=ActorType.USER.value if actor else ActorType.SYSTEM.value,
            action="meeting.transition",
            target_type="meeting",
            target_id=meeting.id,
            metadata_json=entry,
        )
    )
    logger.info(
        "meeting_transition",
        meeting_id=str(meeting.id),
        from_status=previous,
        to_status=to.value,
    )
    return meeting


async def declare(
    session: AsyncSession,
    *,
    organization: Organization,
    author: User,
    started_at: datetime,
    title: str | None = None,
    is_private: bool = False,
) -> Meeting:
    """EF-40: the desktop announces a recording has begun.

    Called at the *start* of the recording, not the end, so that a laptop which
    crashes mid-meeting still leaves a row to reconcile against — and so the
    `debug_id` exists before anything can go wrong with it (ADR-07).

    The session must already be scoped to `organization`.
    """
    meeting = Meeting(
        id=uuid7(),
        organization_id=organization.id,
        created_by=author.id,
        title=title.strip() if title else None,
        status=MeetingStatus.CREATED.value,
        started_at=started_at,
        is_private=is_private,
        debug_id=new_debug_id("MTG"),
    )
    session.add(meeting)
    # `created_at` comes from a server default, so it does not exist until the
    # row reaches the database. Presenting the meeting before this flush hands
    # the response model a None it is right to reject.
    await session.flush()

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=organization.id,
            actor_id=author.id,
            actor_type=ActorType.USER.value,
            action="meeting.created",
            target_type="meeting",
            target_id=meeting.id,
        )
    )
    logger.info("meeting_declared", meeting_id=str(meeting.id), debug_id=meeting.debug_id)
    return meeting


def _is_administrator(caller: User) -> bool:
    """Whether this caller administers the organization."""
    return caller.role in {Role.OWNER.value, Role.ADMIN.value}


def _may_administer(meeting: Meeting, caller: User) -> bool:
    """Whether this caller may edit or remove this meeting.

    Section 17.2 gives it to the author or an administrator. An ordinary member
    reads their colleagues' meetings but does not rewrite them.
    """
    return meeting.created_by == caller.id or _is_administrator(caller)


def may_read(meeting: Meeting, caller: User) -> bool:
    """Whether this caller may see this meeting at all (EF-22, section 17.2).

    A meeting marked private is visible to its author and to administrators,
    and to nobody else. RLS cannot express this: the rows belong to the
    organization, and the restriction is between colleagues inside it.

    This is a read gate, and it has to be applied at every door — the list, the
    single meeting, the report, the audio link and the status socket all reach
    the same row by different routes. Anything that returns a `Meeting` goes
    through here or through the query filter below.
    """
    return not meeting.is_private or _may_administer(meeting, caller)


def _readable(caller: User) -> ColumnElement[bool]:
    """The same rule as :func:`may_read`, as a SQL predicate.

    Written twice on purpose, and kept next to its twin so the two are read
    together: filtering in Python after a `LIMIT` would silently return short
    pages, and paging by a rule the database does not know is how a private
    meeting ends up occupying a slot in somebody else's list.
    """
    if _is_administrator(caller):
        return true()
    return or_(Meeting.is_private.is_(False), Meeting.created_by == caller.id)


async def get(session: AsyncSession, *, meeting_id: uuid.UUID, caller: User) -> Meeting:
    """One meeting, or a 404-shaped refusal.

    RLS confines the lookup to the caller's organization, so a meeting in
    another tenant is indistinguishable from one that does not exist — which is
    the answer we want it to give.

    A private meeting belonging to a colleague answers the same way, for the
    same reason: 403 would confirm that the meeting exists, which is precisely
    what its author asked us not to reveal.
    """
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None or meeting.status == MeetingStatus.DELETED.value:
        raise MeetingError("MEETING_NOT_FOUND", "no such meeting")
    if not may_read(meeting, caller):
        raise MeetingError("MEETING_NOT_FOUND", "no such meeting")
    return meeting


async def listing(
    session: AsyncSession,
    *,
    caller: User,
    status: MeetingStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Meeting]:
    """The organization's meetings, newest first.

    Deleted ones are excluded rather than filtered by the caller: leaving that
    to each client is how a tombstone eventually shows up in somebody's list.
    Private ones belonging to colleagues are excluded for the same reason.
    """
    query = select(Meeting).where(
        Meeting.status != MeetingStatus.DELETED.value,
        _readable(caller),
    )
    if status is not None:
        query = query.where(Meeting.status == status.value)
    query = query.order_by(Meeting.created_at.desc()).limit(limit).offset(offset)
    return list((await session.scalars(query)).all())


async def update(
    session: AsyncSession,
    *,
    meeting: Meeting,
    caller: User,
    changes: Mapping[str, Any],
) -> Meeting:
    """Edit what a human may edit: the title, and whether it is private.

    Status is not here. It moves through :func:`advance` and nowhere else, so
    no request body can declare a meeting COMPLETED.
    """
    if not _may_administer(meeting, caller):
        raise MeetingError("FORBIDDEN", "only the author or an administrator may edit a meeting")

    applied: dict[str, Any] = {}
    if "title" in changes:
        title = changes["title"]
        applied["title"] = title.strip() if title else None
    if "is_private" in changes and changes["is_private"] is not None:
        applied["is_private"] = changes["is_private"]

    if not applied:
        return meeting

    for field, value in applied.items():
        setattr(meeting, field, value)

    session.add(
        AuditLog(
            id=uuid7(),
            organization_id=meeting.organization_id,
            actor_id=caller.id,
            actor_type=ActorType.USER.value,
            action="meeting.updated",
            target_type="meeting",
            target_id=meeting.id,
            metadata_json={"fields": sorted(applied)},
        )
    )
    return meeting


async def remove(session: AsyncSession, *, meeting: Meeting, caller: User) -> Meeting:
    """Delete a meeting, as a state rather than a `DELETE FROM`.

    The row survives so `usage_ledger` keeps a meeting to point at: section 19.2
    requires the consumption record to outlive the meeting for accounting. What
    the customer asked to be rid of — the audio, the transcript, the report — is
    removed by the purge that follows this transition.
    """
    if not _may_administer(meeting, caller):
        raise MeetingError("FORBIDDEN", "only the author or an administrator may delete a meeting")

    return await advance(session, meeting=meeting, to=MeetingStatus.DELETED, actor=caller)


# --------------------------------------------------------------------------
# Upload and finalisation (EF-40, section 16.4)
# --------------------------------------------------------------------------


async def start_upload(
    session: AsyncSession,
    *,
    storage: StorageProvider,
    meeting: Meeting,
    caller: User,
    size_bytes: int,
    sha256: str,
    duration_seconds: int,
    paused_seconds: int,
) -> MultipartUpload:
    """`finalize-local`: the recording is encoded; hand back somewhere to put it.

    The declared size, duration and digest are written now, before a byte
    moves. That ordering is what makes the finalisation checkable at all: at
    `finalize` we compare what the store actually holds against what was
    promised here, and a mismatch means the upload is not what it claimed.

    Only the author may upload: a colleague has no business attaching audio to
    someone else's recording.
    """
    if meeting.created_by != caller.id:
        raise MeetingError("FORBIDDEN", "only the author may upload a meeting's audio")

    meeting.audio_sha256 = sha256
    meeting.audio_bytes = size_bytes
    meeting.duration_seconds = duration_seconds
    meeting.paused_seconds = paused_seconds
    meeting.audio_key = audio_key(organization_id=meeting.organization_id, meeting_id=meeting.id)

    # CREATED → UPLOADING. Refused if the meeting already moved on, which is
    # what stops a replayed request from reopening a finished recording.
    await advance(session, meeting=meeting, to=MeetingStatus.UPLOADING, actor=caller)

    try:
        upload = await storage.start_multipart(key=meeting.audio_key, size_bytes=size_bytes)
    except StorageError as exc:
        raise MeetingError("STORAGE_UNAVAILABLE", str(exc)) from exc

    # Kept so the upload can be aborted if it is never finished. The store will
    # not tell us later which uploads are open under a key, and parts left
    # behind are billed until somebody names them.
    meeting.audio_upload_id = upload.upload_id
    return upload


async def finalize(
    session: AsyncSession,
    *,
    storage: StorageProvider,
    organization: Organization,
    meeting: Meeting,
    caller: User,
    upload_id: str,
    parts: Sequence[tuple[int, str]],
    client_version: str | None = None,
    dispatch: Dispatch | None = None,
) -> Meeting:
    """`finalize`: assemble the parts and queue the work.

    What is verified here is the **size**: the store is asked what it actually
    holds and the answer must match what was declared at `finalize-local`. A
    truncated or padded upload is refused and the meeting does not enter the
    queue.

    What is *not* verified here is the SHA-256, and it is worth being precise
    about why. Computing it server-side means reading the whole object, and the
    API never touches the audio — that is the point of presigned uploads.
    The digest is stored and checked by the worker that fetches the recording
    for transcription, which is the first place the bytes actually exist.

    Answers by advancing to QUEUED, or to QUOTA_HOLD when the organization has
    no seconds left; the caller replies 202 either way (EF-40).
    """
    if meeting.created_by != caller.id:
        raise MeetingError("FORBIDDEN", "only the author may finalize a meeting")
    if meeting.audio_key is None:
        raise MeetingError("UPLOAD_NOT_STARTED", "this meeting has no upload in progress")

    try:
        stored = await storage.complete_multipart(
            key=meeting.audio_key, upload_id=upload_id, etags=list(parts)
        )
    except StorageError as exc:
        raise MeetingError("UPLOAD_INCOMPLETE", str(exc)) from exc

    if meeting.audio_bytes is not None and stored.size_bytes != meeting.audio_bytes:
        # Refused rather than accepted-and-flagged: transcribing a truncated
        # recording would produce a confident, incomplete report, which is
        # worse than no report at all.
        await storage.delete(key=meeting.audio_key)
        raise MeetingError(
            "SIZE_MISMATCH",
            f"the stored audio is {stored.size_bytes} bytes, {meeting.audio_bytes} were declared",
        )

    # The size the store reports wins over the declared one: it is the only
    # number that describes what actually exists.
    meeting.audio_bytes = stored.size_bytes
    # Closed: there is nothing left to abort.
    meeting.audio_upload_id = None
    # ADR-06: the clock starts when the recording lands, not when it is
    # processed. A meeting stuck in a queue must not have its retention
    # silently extended by the backlog.
    meeting.purge_at = retention_deadline(organization)

    # Section 20.3: going over quota blocks the *processing*, never the
    # recording. The audio is already stored and stays stored; the meeting
    # waits for a pack or a renewal instead of being refused. Losing it here
    # would be the one failure a customer cannot recover from.
    entry: dict[str, Any] = {}
    if client_version:
        entry["client_version"] = client_version

    if usage.can_afford(organization, meeting.duration_seconds):
        destination = MeetingStatus.QUEUED
    else:
        destination = MeetingStatus.QUOTA_HOLD
        entry["remaining_seconds"] = usage.remaining_seconds(organization)

    await advance(
        session,
        meeting=meeting,
        to=destination,
        actor=caller,
        metadata=entry or None,
    )

    if destination is MeetingStatus.QUEUED and dispatch is not None:
        # Handed off, not run here: EF-40 promises a 202 in under 500 ms, and
        # the user never waits on a request. A meeting held for quota is not
        # dispatched — the webhook that lifts the hold will do it (section 11).
        dispatch(organization_id=meeting.organization_id, meeting_id=meeting.id)
    logger.info("meeting_finalized", meeting_id=str(meeting.id), size_bytes=stored.size_bytes)
    return meeting


async def abandon(
    session: AsyncSession, *, storage: StorageProvider, meeting: Meeting, caller: User
) -> Meeting:
    """Give up on a recording before it is queued.

    The parts left in the store are dropped. An abandoned multipart upload is
    billed until it is aborted, nobody ever notices those, and this function
    took a storage provider for a long time without using it — the docstring
    described the intention and the code did not carry it out.

    The abort is best-effort and deliberately does not block the cancellation:
    the customer asked to abandon the recording, and a store that is briefly
    unreachable must not turn that into an error they have to retry. What is
    left behind is picked up by the sweep in :func:`purge_due_audio`.
    """
    if meeting.created_by != caller.id:
        raise MeetingError("FORBIDDEN", "only the author may cancel a meeting")

    if meeting.audio_key and meeting.audio_upload_id:
        try:
            await storage.abort_multipart(key=meeting.audio_key, upload_id=meeting.audio_upload_id)
        except StorageError:
            logger.warning("upload_abort_failed", meeting_id=str(meeting.id))
        else:
            meeting.audio_upload_id = None

    return await advance(session, meeting=meeting, to=MeetingStatus.CANCELLED, actor=caller)


# --------------------------------------------------------------------------
# Retry and audio retention (EF-45, ADR-06)
# --------------------------------------------------------------------------


async def retry(session: AsyncSession, *, meeting: Meeting, caller: User) -> Meeting:
    """EF-45: put a failed meeting back in the queue.

    Only from FAILED. Re-running a meeting that succeeded would pay for a
    second transcription and overwrite a report somebody may already have read
    and corrected.
    """
    if not _may_administer(meeting, caller):
        raise MeetingError("FORBIDDEN", "only the author or an administrator may retry a meeting")
    if meeting.status != MeetingStatus.FAILED.value:
        raise MeetingError("NOT_FAILED", "only a failed meeting can be retried")

    # Cleared so the next failure is not confused with the last one.
    meeting.failed_reason = None
    return await advance(session, meeting=meeting, to=MeetingStatus.QUEUED, actor=caller)


def retention_deadline(organization: Organization, *, now: datetime | None = None) -> datetime:
    """When this organization's audio should disappear (ADR-06).

    Read from the organization's own row, never from a constant: the retention
    a customer bought is data, and writing 30 here would make ADR-09 a
    suggestion.
    """
    moment = now or datetime.now(UTC)
    return moment + timedelta(days=organization.audio_retention_days)


async def purge_due_audio(
    session: AsyncSession, *, storage: StorageProvider, now: datetime | None = None
) -> list[uuid.UUID]:
    """ADR-06: the audio goes, the text stays.

    Published meetings past their retention lose the recording and nothing
    else. The report, the transcript and the tasks are the durable part of the
    product; the audio is the expensive, sensitive part nobody agreed to keep
    for ever.

    **A meeting that never reaches publication is swept too**, and for a long
    time it was not. Only PUBLISHED was considered, so a recording that failed,
    was cancelled or was deleted kept its audio in the bucket permanently —
    which is the exact opposite of what ADR-06 says the audio is. Those are the
    meetings least likely to be looked at again and most likely to be
    forgotten.

    Three cases, and they end differently on purpose:

    * PUBLISHED past its deadline moves to AUDIO_PURGED. There is a report to
      point at, so the state is worth recording.
    * FAILED past its deadline loses the recording without changing state. The
      Retry button lives on FAILED and a retry needs the audio, so the
      retention window doubles as the retry window; past it, retrying would
      fail on missing audio, which is honest rather than surprising.
    * CANCELLED and DELETED lose it at the next sweep, deadline or not. Nothing
      can be done with those recordings, and DELETED means somebody asked.

    CANCELLED and DELETED are terminal in the transition table, so their key is
    cleared in place rather than through :func:`advance`. That is a data
    change, not a status change.

    The session must be scoped by the caller, one organization at a time: this
    reads and writes tenant rows, and a cross-tenant sweep has no business
    holding a session that could see them all.
    """
    moment = now or datetime.now(UTC)
    expired = and_(Meeting.purge_at.is_not(None), Meeting.purge_at <= moment)
    due = (
        await session.scalars(
            select(Meeting).where(
                or_(
                    and_(Meeting.status == MeetingStatus.PUBLISHED.value, expired),
                    and_(Meeting.status == MeetingStatus.FAILED.value, expired),
                    Meeting.status.in_(
                        [MeetingStatus.CANCELLED.value, MeetingStatus.DELETED.value]
                    ),
                ),
                # Nothing to do for a meeting that never had a recording, or
                # whose recording has already gone.
                or_(Meeting.audio_key.is_not(None), Meeting.audio_upload_id.is_not(None)),
            )
        )
    ).all()

    purged: list[uuid.UUID] = []
    for meeting in due:
        if not await _drop_recording(storage, meeting):
            # Left for the next run rather than marked purged: claiming the
            # audio is gone while it sits in the bucket is the one outcome
            # ADR-06 cannot tolerate.
            continue

        if meeting.status == MeetingStatus.PUBLISHED.value:
            # `advance` clears the key and stamps the time.
            await advance(session, meeting=meeting, to=MeetingStatus.AUDIO_PURGED)
        else:
            meeting.audio_key = None
            meeting.purged_at = moment
            logger.info(
                "audio_purged_without_transition",
                meeting_id=str(meeting.id),
                status=meeting.status,
            )
        purged.append(meeting.id)

    return purged


async def _drop_recording(storage: StorageProvider, meeting: Meeting) -> bool:
    """Remove whatever this meeting still holds in the store.

    Both an object and an unfinished upload, because a meeting can hold either.
    A recording abandoned between `finalize-local` and `finalize` has no object
    at all — only parts, which are billed and which nothing lists for us.

    Answers whether the store is now clean, so the caller knows whether it may
    say so in the database.
    """
    if meeting.audio_upload_id and meeting.audio_key:
        try:
            await storage.abort_multipart(key=meeting.audio_key, upload_id=meeting.audio_upload_id)
        except StorageError:
            logger.warning("upload_abort_failed", meeting_id=str(meeting.id))
            return False
        meeting.audio_upload_id = None

    if meeting.audio_key:
        try:
            await storage.delete(key=meeting.audio_key)
        except StorageError:
            logger.warning("audio_purge_failed", meeting_id=str(meeting.id))
            return False

    return True
