"""The processing status socket (EF-44, section 17.2).

`WS /api/v1/ws/meetings/{id}`, opened with a single-use ticket.

**It polls the database.** Every two seconds it reads the meeting's state and
pushes a frame when it changed. Redis pub/sub would be tidier in principle, but
a meeting changes state five or six times in its life, the poll is one indexed
primary-key read, and pub/sub adds a delivery guarantee to reason about between
a worker and a socket. When the number of concurrent viewers makes that trade
wrong, the endpoint changes and nothing else does.

The socket closes itself once the meeting reaches a state nothing follows. A
client holding a connection open on a finished meeting is a connection nobody
will ever close.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import set_current_organization
from app.logging import get_logger
from app.models import Meeting, MeetingStatus
from app.tickets import TicketStore
from schemas.meetings import MeetingStatusEvent

router = APIRouter(tags=["meetings"])
logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 2.0
# Nothing follows these, so there is nothing left to watch.
_FINAL = {
    MeetingStatus.PUBLISHED.value,
    MeetingStatus.AUDIO_PURGED.value,
    MeetingStatus.FAILED.value,
    MeetingStatus.CANCELLED.value,
    MeetingStatus.DELETED.value,
    MeetingStatus.QUOTA_HOLD.value,
}

# Rough figures for the estimate of EF-44, and rough on purpose: a precise
# number that keeps slipping reads as a broken promise, while an honest
# "about a minute" does not. Null once there is nothing useful to say.
_REMAINING_SECONDS = {
    MeetingStatus.QUEUED.value: 300,
    MeetingStatus.TRANSCRIBING.value: 180,
    MeetingStatus.FALLBACK_STT.value: 240,
    MeetingStatus.ANALYZING.value: 60,
    MeetingStatus.COMPLETED.value: 5,
}


@router.websocket("/ws/meetings/{meeting_id}")
async def meeting_status(websocket: WebSocket, meeting_id: uuid.UUID) -> None:
    """Push this meeting's state until it stops changing."""
    store: TicketStore | None = getattr(websocket.app.state, "tickets", None)
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        websocket.app.state, "session_factory", None
    )
    ticket = websocket.query_params.get("ticket")

    if store is None or factory is None or not ticket:
        # 1008 is "policy violation". Refused before accepting, so an
        # unauthenticated client never gets an open socket at all.
        await websocket.close(code=1008)
        return

    claims = await store.redeem(ticket)
    if claims is None or claims.meeting_id != meeting_id:
        # Expired, already used, or pointed at a different meeting. All three
        # are the same answer: this ticket does not open this socket.
        await websocket.close(code=1008)
        return

    await websocket.accept()
    last_sent: str | None = None

    try:
        while True:
            async with factory() as session:
                await set_current_organization(session, claims.organization_id)
                meeting = await session.get(Meeting, meeting_id)

            if meeting is None:
                await websocket.close(code=1000)
                return

            if meeting.status != last_sent:
                # Validated rather than constructed: the column is a plain
                # string and the frame declares a closed set, so a state the
                # client does not know about fails here instead of reaching it.
                event = MeetingStatusEvent.model_validate(
                    {
                        "meeting_id": meeting.id,
                        "status": meeting.status,
                        "estimated_seconds_remaining": _REMAINING_SECONDS.get(meeting.status),
                        "failed_reason": meeting.failed_reason,
                    }
                )
                await websocket.send_text(event.model_dump_json())
                last_sent = meeting.status

            if meeting.status in _FINAL:
                await websocket.close(code=1000)
                return

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except WebSocketDisconnect:
        # The client left. Normal, and not worth an error line.
        logger.info("status_socket_closed", meeting_id=str(meeting_id))
