"""Structured extraction (lot L2.6).

No model is called. What is proved is everything around it: that an answer the
schema rejects never becomes a report, that an item pointing at nothing is
refused, that three failures fail the meeting without charging the customer,
and that dates and assignees are only filled in when the meeting actually said
so.

What is *not* proved: whether GPT-5 mini obeys the prompt. The evaluation
harness for that is written and runnable (`ai.evaluation.hallucination`) and
has never been pointed at a real model — the campaign was cancelled on
2026-09-08.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ai.evaluation import VAGUE_CASES, hallucination_rate
from ai.evaluation import run as run_evaluation
from ai.extraction import MeetingReport, SegmentWindow, UnsourcedItemError, check_sources
from ai.llm.base import LLMError, Usage
from ai.llm.fake import FakeLLMProvider
from app.config import get_settings
from app.db import create_session_factory, set_current_organization
from app.models import Decision, Meeting, Organization, Report, Task, TranscriptSegment, User
from app.prompts import PromptNotFoundError, load
from app.services import extraction
from app.services.meetings import declare
from app.uuid7 import uuid7

pytestmark = pytest.mark.asyncio

APP_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://novabrief_app:novabrief-app-dev@localhost:5432/novabrief",
)


def _valid_report(**overrides: object) -> str:
    body: dict[str, object] = {
        "title": "Point budget et plan editorial",
        "language": "fr",
        "participants": ["Cabrel", "Marie"],
        "summary": ["L'enveloppe budgetaire a ete validee."],
        "decisions": [
            {
                "content": "Le devis de 5 millions de FCFA a ete valide.",
                "source_start_ms": 4200,
                "confidence": 0.95,
            }
        ],
        "tasks": [
            {
                "action": "Preparer le plan editorial",
                "assignee": "Cabrel",
                "deadline_text": "demain",
                "source_start_ms": 4200,
                "confidence": 0.9,
            }
        ],
    }
    body.update(overrides)
    return json.dumps(body)


# --------------------------------------------------------------------------
# The prompt is a file, and its version is recorded
# --------------------------------------------------------------------------


async def test_the_prompt_has_the_blocks_the_pipeline_needs() -> None:
    blocks = load("extract_v1")

    assert "system" in blocks
    assert "repair" in blocks
    assert "{error}" in blocks["repair"], "the repair prompt must quote the validation error"


async def test_the_prompt_states_the_rule_that_matters() -> None:
    """Risk number 2 of the specification lives or dies on this instruction."""
    system = load("extract_v1")["system"]

    assert "N'invente jamais" in system
    assert "Ne deduis jamais un responsable" in system


async def test_an_unknown_prompt_version_is_loud() -> None:
    """A worker silently running last month's prompt is unexplainable output."""
    with pytest.raises(PromptNotFoundError):
        load("extract_v99")


# --------------------------------------------------------------------------
# The schema decides (ADR-03)
# --------------------------------------------------------------------------


async def test_a_low_confidence_item_is_rejected_by_the_schema() -> None:
    """The prompt says not to extract below 0.5; the schema enforces it."""
    with pytest.raises(ValueError, match="confidence"):
        MeetingReport.model_validate_json(
            _valid_report(
                decisions=[
                    {"content": "Peut-etre le devis.", "source_start_ms": 0, "confidence": 0.2}
                ]
            )
        )


async def test_an_extra_field_is_rejected() -> None:
    """A model inventing a field is a model not following the schema."""
    with pytest.raises(ValueError, match="extra"):
        MeetingReport.model_validate_json(_valid_report(sentiment="positive"))


async def test_an_item_pointing_at_nothing_is_refused() -> None:
    """EF-43: every extracted item resolves to a passage the user can play."""
    report = MeetingReport.model_validate_json(
        _valid_report(
            decisions=[
                {"content": "Une decision inventee.", "source_start_ms": 999_999, "confidence": 0.9}
            ]
        )
    )

    with pytest.raises(UnsourcedItemError):
        check_sources(report, [SegmentWindow(start_ms=0, end_ms=9000)])


async def test_provider_rounding_does_not_fail_a_good_report() -> None:
    """Suppliers round timestamps; failing a meeting over 200 ms would be absurd."""
    report = MeetingReport.model_validate_json(
        _valid_report(
            decisions=[
                {"content": "Une vraie decision.", "source_start_ms": 9200, "confidence": 0.9}
            ],
            tasks=[],
        )
    )

    check_sources(report, [SegmentWindow(start_ms=0, end_ms=9000)])


# --------------------------------------------------------------------------
# Normalising deadlines
# --------------------------------------------------------------------------


async def test_tomorrow_is_the_day_after_the_meeting() -> None:
    """Not the day after the worker ran: a queue backlog would shift every date."""
    result = extraction.normalise_deadline("demain", reference=date(2026, 9, 9))

    assert result == date(2026, 9, 10)


async def test_a_weekday_resolves_forward() -> None:
    # 2026-09-09 is a Wednesday.
    assert extraction.normalise_deadline("vendredi", reference=date(2026, 9, 9)) == date(
        2026, 9, 11
    )


async def test_a_day_number_that_has_passed_moves_to_next_month() -> None:
    assert extraction.normalise_deadline("avant le 5", reference=date(2026, 9, 9)) == date(
        2026, 10, 5
    )


async def test_an_expression_nobody_can_resolve_stays_empty() -> None:
    """A wrong date is worse than no date: somebody plans around it."""
    assert extraction.normalise_deadline("des que possible", reference=date(2026, 9, 9)) is None
    assert extraction.normalise_deadline("le 31", reference=date(2026, 2, 1)) is None


async def test_the_original_wording_is_never_replaced() -> None:
    """Kept beside the date, because that is the only way to spot a bad guess."""
    assert extraction.normalise_deadline(None, reference=date(2026, 9, 9)) is None


# --------------------------------------------------------------------------
# The whole step, against the database
# --------------------------------------------------------------------------


@dataclass
class Fixture:
    engine: AsyncEngine
    organization_id: uuid.UUID
    meeting_id: uuid.UUID
    segments: list[TranscriptSegment]


@pytest_asyncio.fixture
async def prepared(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Fixture]:
    """An organization with a meeting in ANALYZING and a transcript behind it."""
    monkeypatch.setenv("DATABASE_URL", APP_DATABASE_URL)
    get_settings.cache_clear()

    engine = create_async_engine(APP_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"no test database reachable ({type(exc).__name__})")

    organization_id = uuid7()
    factory = create_session_factory(engine)

    async with factory() as session, session.begin():
        await set_current_organization(session, organization_id)
        organization = Organization(id=organization_id, name="Test Org")
        session.add(organization)
        await session.flush()
        author = User(
            id=uuid7(),
            organization_id=organization_id,
            email=f"owner-{uuid.uuid4().hex[:8]}@test.cm",
            password_hash="x",
            full_name="Cabrel Ndongo",
            role="OWNER",
            timezone="Africa/Douala",
        )
        session.add(author)
        await session.flush()

        meeting = await declare(
            session,
            organization=organization,
            author=author,
            started_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )
        meeting.status = "ANALYZING"
        meeting.duration_seconds = 600
        meeting_id = meeting.id

    segments = [
        TranscriptSegment(
            id=uuid7(),
            organization_id=organization_id,
            transcript_id=uuid7(),
            speaker_tag="A",
            start_ms=0,
            end_ms=4000,
            text="Bonjour a tous.",
        ),
        TranscriptSegment(
            id=uuid7(),
            organization_id=organization_id,
            transcript_id=uuid7(),
            speaker_tag="B",
            start_ms=4200,
            end_ms=9000,
            text="On valide le devis de 5 millions.",
        ),
    ]

    yield Fixture(
        engine=engine,
        organization_id=organization_id,
        meeting_id=meeting_id,
        segments=segments,
    )

    await engine.dispose()
    get_settings.cache_clear()


async def _extract(fixture: Fixture, llm: FakeLLMProvider) -> extraction.Extracted:
    prompts = load("extract_v1")
    factory = create_session_factory(fixture.engine)
    async with factory() as session, session.begin():
        await set_current_organization(session, fixture.organization_id)
        organization = await session.get(Organization, fixture.organization_id)
        meeting = await session.get(Meeting, fixture.meeting_id)
        assert organization is not None and meeting is not None
        return await extraction.extract_report(
            session,
            llm=llm,
            system_prompt=prompts["system"],
            repair_prompt=prompts["repair"],
            prompt_version="extract_v1",
            organization=organization,
            meeting=meeting,
            segments=fixture.segments,
        )


async def test_a_valid_answer_becomes_a_report(prepared: Fixture) -> None:
    llm = FakeLLMProvider(answers=[_valid_report()])

    await _extract(prepared, llm)

    factory = create_session_factory(prepared.engine)
    async with factory() as session:
        await set_current_organization(session, prepared.organization_id)
        report = await session.scalar(
            select(Report).where(Report.meeting_id == prepared.meeting_id)
        )
        decisions = (
            await session.scalars(
                select(Decision).where(Decision.meeting_id == prepared.meeting_id)
            )
        ).all()
        tasks = (
            await session.scalars(select(Task).where(Task.meeting_id == prepared.meeting_id))
        ).all()

    assert report is not None
    assert report.prompt_version == "extract_v1"
    assert report.model_version == "fake:v1"
    assert len(decisions) == 1
    assert len(tasks) == 1


async def test_a_rejected_answer_is_retried_with_the_error(prepared: Fixture) -> None:
    """A model told only "that was wrong" produces a different wrong answer."""
    llm = FakeLLMProvider(
        answers=[json.dumps({"title": "trop court"}), _valid_report()],
    )

    result = await _extract(prepared, llm)

    assert len(llm.calls) == 2
    second_prompt = llm.calls[1][1]
    assert "schema" in second_prompt.lower() or "conforme" in second_prompt.lower()
    assert result.report is not None


async def test_three_invalid_answers_fail_the_meeting(prepared: Fixture) -> None:
    """EF-45: past three attempts it fails, rather than shipping a half report."""
    llm = FakeLLMProvider(answers=[json.dumps({"title": "x"})] * 3)

    with pytest.raises(extraction.ExtractionFailedError) as raised:
        await _extract(prepared, llm)

    assert raised.value.code == "INVALID_MODEL_OUTPUT"
    assert len(llm.calls) == 3


async def test_an_invented_timestamp_is_retried_not_stored(prepared: Fixture) -> None:
    """The check that stops an invented decision reaching a customer."""
    invented = _valid_report(
        decisions=[
            {"content": "Une decision inventee.", "source_start_ms": 999_999, "confidence": 0.9}
        ],
        tasks=[],
    )
    llm = FakeLLMProvider(answers=[invented, _valid_report()])

    await _extract(prepared, llm)

    assert len(llm.calls) == 2


async def test_a_refusal_is_not_retried(prepared: Fixture) -> None:
    """A rejected request fails the same way three times; it just costs more."""
    llm = FakeLLMProvider(failures=[LLMError("bad request", retryable=False)])

    with pytest.raises(extraction.ExtractionFailedError) as raised:
        await _extract(prepared, llm)

    assert raised.value.code == "LLM_REFUSED"
    assert len(llm.calls) == 1


async def test_a_spoken_first_name_is_linked_to_the_member(prepared: Fixture) -> None:
    llm = FakeLLMProvider(answers=[_valid_report()])

    await _extract(prepared, llm)

    factory = create_session_factory(prepared.engine)
    async with factory() as session:
        await set_current_organization(session, prepared.organization_id)
        task = await session.scalar(select(Task).where(Task.meeting_id == prepared.meeting_id))

    assert task is not None
    assert task.assignee_name == "Cabrel"
    assert task.assignee_user_id is not None, "an exact first name should link to the member"


async def test_an_unknown_name_stays_unlinked(prepared: Fixture) -> None:
    """Assigning work to the wrong colleague is how a report starts an argument."""
    llm = FakeLLMProvider(
        answers=[
            _valid_report(
                tasks=[
                    {
                        "action": "Preparer le plan editorial",
                        "assignee": "Quelquun",
                        "deadline_text": None,
                        "source_start_ms": 4200,
                        "confidence": 0.9,
                    }
                ]
            )
        ]
    )

    await _extract(prepared, llm)

    factory = create_session_factory(prepared.engine)
    async with factory() as session:
        await set_current_organization(session, prepared.organization_id)
        task = await session.scalar(select(Task).where(Task.meeting_id == prepared.meeting_id))

    assert task is not None
    assert task.assignee_name == "Quelquun"
    assert task.assignee_user_id is None


async def test_the_deadline_is_computed_from_the_meetings_day(prepared: Fixture) -> None:
    llm = FakeLLMProvider(answers=[_valid_report()])

    await _extract(prepared, llm)

    factory = create_session_factory(prepared.engine)
    async with factory() as session:
        await set_current_organization(session, prepared.organization_id)
        task = await session.scalar(select(Task).where(Task.meeting_id == prepared.meeting_id))

    assert task is not None
    assert task.deadline_text == "demain"
    assert task.deadline_date == date(2026, 9, 10)


async def test_an_empty_transcript_is_refused(prepared: Fixture) -> None:
    llm = FakeLLMProvider(answers=[_valid_report()])
    prompts = load("extract_v1")
    factory = create_session_factory(prepared.engine)

    async with factory() as session, session.begin():
        await set_current_organization(session, prepared.organization_id)
        organization = await session.get(Organization, prepared.organization_id)
        meeting = await session.get(Meeting, prepared.meeting_id)
        assert organization is not None and meeting is not None

        with pytest.raises(extraction.ExtractionFailedError) as raised:
            await extraction.extract_report(
                session,
                llm=llm,
                system_prompt=prompts["system"],
                repair_prompt=prompts["repair"],
                prompt_version="extract_v1",
                organization=organization,
                meeting=meeting,
                segments=[],
            )

    assert raised.value.code == "EMPTY_TRANSCRIPT"


async def test_the_transcript_carries_its_timestamps(prepared: Fixture) -> None:
    """Rule 5 of the prompt is impossible to obey without them."""
    rendered = extraction.render_transcript(prepared.segments)

    assert "[0]" in rendered
    assert "[4200]" in rendered


# --------------------------------------------------------------------------
# The evaluation harness (section 18.6)
# --------------------------------------------------------------------------


async def test_the_evaluation_set_covers_the_ways_a_model_invents() -> None:
    """Section 18.6 asks for ten vague phrasings that must produce nothing."""
    assert len(VAGUE_CASES) >= 10
    assert len({case.name for case in VAGUE_CASES}) == len(VAGUE_CASES)


async def test_a_well_behaved_model_scores_zero() -> None:
    """The harness itself, checked against a double that extracts nothing."""
    empty = json.dumps(
        {
            "title": "Echange informel",
            "language": "fr",
            "participants": [],
            "summary": ["Aucune decision n'a ete prise."],
            "decisions": [],
            "tasks": [],
        }
    )
    llm = FakeLLMProvider(answers=[empty] * len(VAGUE_CASES))

    outcomes = await run_evaluation(llm, system_prompt="x")

    assert hallucination_rate(outcomes) == 0.0


async def test_a_model_that_invents_is_caught() -> None:
    """If this ever passes silently, the measurement is worthless."""
    llm = FakeLLMProvider(answers=[_valid_report()] * len(VAGUE_CASES))

    outcomes = await run_evaluation(llm, system_prompt="x")

    assert hallucination_rate(outcomes) == 1.0


async def test_the_usage_of_a_call_is_reported() -> None:
    """ADR-08: tokens and cost reach the ledger."""
    llm = FakeLLMProvider(
        answers=[_valid_report()],
        usage=Usage(model="fake:v1", tokens_in=1200, tokens_out=300, cost_usd=Decimal("0.00250")),
    )

    _, usage = await llm.extract("system", "user", MeetingReport)

    assert usage.tokens_in == 1200
    assert usage.cost_usd == Decimal("0.00250")
