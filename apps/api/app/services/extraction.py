"""Turning a transcript into a structured report (EF-42, EF-43, section 18.2).

Steps 5 to 7. The model is asked for JSON matching a schema; the answer is
validated; a failure is retried with the validation error quoted back, three
times, and then the meeting fails (EF-45). ADR-03 in practice: an invalid
answer is an error, never a partial result to salvage.

Everything after the model is about not overstepping what was said. Deadlines
are normalised from the words the meeting used, and the original words are kept
beside the date, because when the calendar arithmetic is wrong the original is
the only way anyone can tell. An assignee is linked to a member only on an
exact first-name match; anything looser is a suggestion for a human, since
assigning work to the wrong colleague is how a report starts an argument.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.extraction import MeetingReport, SegmentWindow, UnsourcedItemError, check_sources
from ai.llm.base import InvalidStructuredOutputError, LLMError, LLMProvider, Usage
from app.logging import get_logger
from app.models import Decision, Meeting, Organization, Report, Task, TranscriptSegment, User
from app.uuid7 import uuid7

logger = get_logger(__name__)

# EF-45: three attempts, then FAILED.
MAX_ATTEMPTS = 3


class ExtractionFailedError(Exception):
    """The analysis could not produce a valid report. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Extracted:
    """What the step produced."""

    report: Report
    usage: Usage
    attempts: int


def render_transcript(segments: list[TranscriptSegment]) -> str:
    """The transcript as the model sees it.

    Each line carries its start time because rule 5 of the prompt requires
    every extracted item to point at one. Without the timestamps the model has
    nothing to cite, and EF-43's clickable source becomes impossible.
    """
    return "\n".join(
        f"[{segment.start_ms}] {segment.speaker_name or segment.speaker_tag}: {segment.text}"
        for segment in segments
    )


async def extract_report(
    session: AsyncSession,
    *,
    llm: LLMProvider,
    system_prompt: str,
    repair_prompt: str,
    prompt_version: str,
    organization: Organization,
    meeting: Meeting,
    segments: list[TranscriptSegment],
) -> Extracted:
    """Analyse the transcript and write the report.

    The session must already be scoped to `organization`.
    """
    if not segments:
        raise ExtractionFailedError("EMPTY_TRANSCRIPT", "there is nothing to analyse")

    windows = [
        SegmentWindow(start_ms=segment.start_ms, end_ms=segment.end_ms) for segment in segments
    ]
    user_prompt = render_transcript(segments)

    report: MeetingReport | None = None
    usage: Usage | None = None
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = user_prompt if attempt == 1 else _repair(repair_prompt, last_error, user_prompt)
        try:
            candidate, spent = await llm.extract(system_prompt, prompt, MeetingReport)
            # The schema said the shape is right; this says the content points
            # at something real. A model that invents a timestamp invents the
            # item attached to it (EF-43).
            check_sources(candidate, windows)
        except InvalidStructuredOutputError as exc:
            last_error = exc.detail
            logger.warning("extraction_schema_rejected", attempt=attempt)
            continue
        except UnsourcedItemError as exc:
            last_error = str(exc)
            logger.warning("extraction_unsourced_items", attempt=attempt)
            continue
        except LLMError as exc:
            if not exc.retryable:
                raise ExtractionFailedError("LLM_REFUSED", "the model refused the request") from exc
            last_error = type(exc).__name__
            logger.warning("extraction_provider_error", attempt=attempt)
            continue

        report, usage = candidate, spent
        break

    if report is None or usage is None:
        # EF-45: past three attempts the meeting fails, an alert is raised and
        # the customer gets a Retry button. Salvaging a partial answer would
        # hand somebody a report nobody can trust.
        raise ExtractionFailedError(
            "INVALID_MODEL_OUTPUT",
            f"no valid report after {MAX_ATTEMPTS} attempts",
        )

    persisted = await _persist(
        session,
        organization=organization,
        meeting=meeting,
        report=report,
        model_version=usage.model,
        prompt_version=prompt_version,
    )
    logger.info(
        "meeting_analysed",
        meeting_id=str(meeting.id),
        decisions=len(report.decisions),
        tasks=len(report.tasks),
    )
    return Extracted(report=persisted, usage=usage, attempts=MAX_ATTEMPTS)


def _repair(template: str, error: str, transcript: str) -> str:
    """The retry prompt: what was wrong, then the transcript again.

    Quoting the error is what makes the second attempt different from the
    first. A model told only "that was wrong" produces a different wrong
    answer.
    """
    return f"{template.format(error=error)}\n\n{transcript}"


async def _persist(
    session: AsyncSession,
    *,
    organization: Organization,
    meeting: Meeting,
    report: MeetingReport,
    model_version: str,
    prompt_version: str,
) -> Report:
    """Write the report, its decisions and its tasks."""
    row = Report(
        id=uuid7(),
        organization_id=organization.id,
        meeting_id=meeting.id,
        title=report.title,
        participants=list(report.participants),
        summary=list(report.summary),
        model_version=model_version,
        prompt_version=prompt_version,
    )
    session.add(row)

    for decision in report.decisions:
        session.add(
            Decision(
                id=uuid7(),
                organization_id=organization.id,
                meeting_id=meeting.id,
                content=decision.content,
                source_start_ms=decision.source_start_ms,
                confidence=decision.confidence,
            )
        )

    members = await _members_by_first_name(session)
    reference = await _meeting_day(session, meeting)

    for task in report.tasks:
        session.add(
            Task(
                id=uuid7(),
                organization_id=organization.id,
                meeting_id=meeting.id,
                action=task.action,
                assignee_name=task.assignee,
                assignee_user_id=_match_member(task.assignee, members),
                deadline_text=task.deadline_text,
                deadline_date=normalise_deadline(task.deadline_text, reference=reference),
                source_start_ms=task.source_start_ms,
                confidence=task.confidence,
            )
        )

    if not meeting.title:
        # Only when the human left it blank: a title somebody typed outranks
        # one a model chose.
        meeting.title = report.title[:200]

    return row


async def _meeting_day(session: AsyncSession, meeting: Meeting) -> date:
    """The meeting's date in the timezone of whoever recorded it.

    In UTC a meeting held at 21:00 in Douala falls on the following day, and
    every "demain" in it would resolve one day late.

    The author is fetched rather than reached through a relationship: a lazy
    load inside a worker raises `MissingGreenlet`, and it fails only once there
    is real data to load — which is to say, in production.
    """
    started = meeting.started_at or datetime.now(UTC)
    author = await session.get(User, meeting.created_by)
    try:
        zone = ZoneInfo(author.timezone) if author else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - defensive
        zone = ZoneInfo("UTC")
    return started.astimezone(zone).date()


async def _members_by_first_name(session: AsyncSession) -> dict[str, User]:
    """Members indexed by first name, dropping any that is ambiguous.

    Two colleagues called Marie means "Marie" identifies nobody. Leaving both
    out is right: an unlinked name is a small annoyance, the wrong colleague
    assigned a task is an argument.
    """
    users = (await session.scalars(select(User).where(User.revoked_at.is_(None)))).all()
    index: dict[str, list[User]] = {}
    for user in users:
        first = _fold(user.full_name.split()[0]) if user.full_name.strip() else ""
        if first:
            index.setdefault(first, []).append(user)
    return {name: found[0] for name, found in index.items() if len(found) == 1}


def _match_member(assignee: str | None, members: dict[str, User]) -> uuid.UUID | None:
    """Link a spoken name to a member, only when it is unmistakable."""
    if not assignee:
        return None
    candidate = _fold(assignee.split()[0]) if assignee.split() else ""
    found = members.get(candidate)
    return found.id if found else None


def _fold(value: str) -> str:
    """Lower-case and strip accents, so "Cédric" matches "cedric"."""
    stripped = unicodedata.normalize("NFKD", value)
    return "".join(char for char in stripped if not unicodedata.combining(char)).lower()


# Section 18.2 step 6. Deliberately small: these are the expressions that
# actually appear in a meeting. Anything else leaves `deadline_date` null,
# which reads as "not computed" rather than as a wrong date somebody acts on.
_WEEKDAYS = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def normalise_deadline(text: str | None, *, reference: date) -> date | None:
    """Turn the words the meeting used into a date, or leave it alone.

    `reference` is the day of the meeting: "vendredi" means the Friday after
    that meeting, not after the day the worker happens to run.

    Returning None is a legitimate answer and the common one. A wrong date on a
    task is worse than no date, because somebody plans around it.
    """
    if not text:
        return None

    lowered = _fold(text.strip())

    if lowered in {"aujourd'hui", "aujourdhui", "today", "ce jour"}:
        return reference
    if lowered in {"demain", "tomorrow"}:
        return reference + timedelta(days=1)
    if lowered in {"apres-demain", "after tomorrow"}:
        return reference + timedelta(days=2)

    # "avant le 15", "le 15", "before the 15th" - the coming 15th, which is
    # next month when the 15th has already passed.
    day_only = re.fullmatch(
        r"(?:avant\s+le|le|before\s+the|by\s+the)?\s*(\d{1,2})(?:er|th|st)?", lowered
    )
    if day_only:
        return _next_day_of_month(int(day_only.group(1)), reference=reference)

    for name, index in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            ahead = (index - reference.weekday()) % 7 or 7
            if "prochain" in lowered or "next" in lowered:
                ahead = ahead if ahead > 0 else 7
            return reference + timedelta(days=ahead)

    if "semaine prochaine" in lowered or "next week" in lowered:
        return reference + timedelta(days=7 - reference.weekday() + 4)
    if "fin du mois" in lowered or "end of the month" in lowered:
        return _end_of_month(reference)

    return None


def _next_day_of_month(day: int, *, reference: date) -> date | None:
    """The coming occurrence of a day number, or None if it is not a real day."""
    if not 1 <= day <= 31:
        return None
    month, year = reference.month, reference.year
    if day <= reference.day:
        month, year = (1, year + 1) if month == 12 else (month + 1, year)
    try:
        return date(year, month, day)
    except ValueError:
        # The 31st of a month that has thirty days.
        return None


def _end_of_month(reference: date) -> date:
    if reference.month == 12:
        return date(reference.year, 12, 31)
    return date(reference.year, reference.month + 1, 1) - timedelta(days=1)
