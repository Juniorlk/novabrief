"""Quota and the consumption ledger (ADR-08, ADR-09, section 20.3).

Two jobs that have to stay together, because getting them apart is how a
customer is billed for something they were not given, or given something they
were not billed for.

**No plan value is written here.** Quotas come from the organization's row,
which lot L5 fills from the `plans` table. This module only ever compares
against what is stored, which is what ADR-09 asks for.

**A null quota means no plan has been assigned yet**, not a quota of zero.
Before billing exists, every organization is in that state and nothing is held.
Once L5 assigns plans, the same code starts enforcing without changing.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import Meeting, Organization, UsageEntry
from app.uuid7 import uuid7

logger = get_logger(__name__)


class QuotaExceededError(Exception):
    """The organization does not have the seconds this would consume."""

    def __init__(self, *, requested: int, remaining: int) -> None:
        self.requested = requested
        self.remaining = remaining
        super().__init__(f"{requested}s requested, {remaining}s remaining")


def remaining_seconds(organization: Organization) -> int | None:
    """What is left of the quota, or None when no plan is assigned.

    None is not "unlimited forever": it is "nobody has said yet", which before
    lot L5 is true of every organization.
    """
    if organization.quota_seconds is None:
        return None
    return max(organization.quota_seconds - organization.consumed_seconds, 0)


def can_afford(organization: Organization, seconds: int) -> bool:
    """Whether processing this meeting is within the quota.

    Section 20.3: exceeding the quota blocks processing, never recording. The
    caller puts the meeting in QUOTA_HOLD rather than refusing the upload — the
    audio is already paid for in effort and must never be lost.
    """
    left = remaining_seconds(organization)
    return left is None or seconds <= left


async def consume(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    seconds: int,
    allow_overshoot: bool = False,
) -> int:
    """Charge the quota, atomically, and return the new total.

    One statement with the guard in its `WHERE`. Two workers publishing at the
    same instant would otherwise both read the same `consumed_seconds`, both
    decide there is room, and both write — leaving the organization over its
    quota by exactly one meeting. PostgreSQL serialises the row update, so the
    second one finds the condition false and raises.

    Raises :class:`QuotaExceededError` rather than clamping: a silent clamp
    would bill for less than was delivered, and nobody would ever notice.

    **`allow_overshoot` is for the publication step, and it is not a loophole.**
    The gate is `can_afford` at finalisation; between that check and this charge
    sit the transcription and the analysis, both already paid for to the
    suppliers. If concurrent meetings ate the remaining seconds in between,
    refusing here would strand a finished report in ANALYZING — the transaction
    rolls back, Celery retries, the LLM is billed again, and after the last
    attempt the meeting sits in a state EF-45's Retry button cannot even reach.
    A report that was produced is a report that gets recorded and billed. The
    overshoot is logged so it is a fact somebody can see, not an accident.
    """
    if seconds < 0:
        message = "consumption cannot be negative"
        raise ValueError(message)

    statement = update(Organization).where(Organization.id == organization_id)
    if not allow_overshoot:
        statement = statement.where(
            # A null quota is not a limit, so the guard passes.
            (Organization.quota_seconds.is_(None))
            | (Organization.consumed_seconds + seconds <= Organization.quota_seconds)
        )
    statement = statement.values(
        consumed_seconds=Organization.consumed_seconds + seconds
    ).returning(Organization.consumed_seconds, Organization.quota_seconds)

    result = cast("CursorResult[Any]", await session.execute(statement))
    row = result.first()

    if row is None:
        # The guard failed. Re-read to say by how much, which is what the
        # client needs in order to buy the right pack.
        organization = await session.get(Organization, organization_id)
        left = remaining_seconds(organization) if organization else 0
        raise QuotaExceededError(requested=seconds, remaining=left or 0)

    consumed, quota = int(row[0]), row[1]
    if quota is not None and consumed > quota:
        logger.warning(
            "quota_overshot",
            seconds=seconds,
            consumed_total=consumed,
            quota_seconds=int(quota),
        )
    logger.info("quota_consumed", seconds=seconds, consumed_total=consumed)
    return consumed


async def record(
    session: AsyncSession,
    *,
    organization: Organization,
    meeting: Meeting,
    seconds_billed: int,
    stt_provider: str | None = None,
    stt_cost_usd: Decimal = Decimal(0),
    llm_provider: str | None = None,
    llm_tokens_in: int = 0,
    llm_tokens_out: int = 0,
    llm_cost_usd: Decimal = Decimal(0),
    storage_cost_usd: Decimal = Decimal(0),
) -> UsageEntry:
    """ADR-08: write down what this meeting actually cost.

    Written once, at publication, and never updated. The unit-economics
    dashboard of section 5.9 reads these rows as history; correcting one after
    the fact would silently rewrite a past week's margin.
    """
    entry = UsageEntry(
        id=uuid7(),
        organization_id=organization.id,
        meeting_id=meeting.id,
        seconds_billed=seconds_billed,
        stt_provider=stt_provider,
        stt_cost_usd=stt_cost_usd,
        llm_provider=llm_provider,
        llm_tokens_in=llm_tokens_in,
        llm_tokens_out=llm_tokens_out,
        llm_cost_usd=llm_cost_usd,
        storage_cost_usd=storage_cost_usd,
    )
    session.add(entry)
    logger.info(
        "usage_recorded",
        meeting_id=str(meeting.id),
        seconds_billed=seconds_billed,
        stt_provider=stt_provider,
        llm_provider=llm_provider,
    )
    return entry
