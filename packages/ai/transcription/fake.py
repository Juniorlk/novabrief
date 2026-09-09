"""Transcription doubles.

Novafrik cancelled the 200-meeting validation campaign on 2026-09-08, so the
pipeline is built and tested without calling a supplier. These stand in.

They are deliberately not clever. A double that guesses what AssemblyAI would
return teaches the tests to expect the double, not the supplier. These return
what they were told to return, or fail the way they were told to fail, so a
test states its own premise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ai.transcription.base import (
    TranscriptionError,
    TranscriptionResult,
    Utterance,
)


def sample_result(
    *,
    provider: str = "fake:v1",
    language: str = "fr",
    duration_seconds: int = 120,
) -> TranscriptionResult:
    """A small, plausible transcript with two speakers."""
    return TranscriptionResult(
        language=language,
        utterances=[
            Utterance(
                speaker_tag="A",
                start_ms=0,
                end_ms=4000,
                text="Bonjour a tous, on commence par le point budget.",
                confidence=0.94,
                channel="local",
            ),
            Utterance(
                speaker_tag="B",
                start_ms=4200,
                end_ms=9000,
                text="Merci Cabrel. Je propose qu'on valide l'enveloppe cette semaine.",
                confidence=0.91,
                channel="remote",
            ),
        ],
        duration_seconds=duration_seconds,
        provider=provider,
        cost_usd=Decimal("0.00500"),
    )


@dataclass
class FakeTranscriptionProvider:
    """Answers with whatever it was configured to answer."""

    provider_name: str = "fake:v1"
    result: TranscriptionResult | None = None
    # Raised instead of answering. A list so a test can say "fail twice, then
    # succeed", which is the shape of a retry.
    failures: list[TranscriptionError] = field(default_factory=list)
    healthy: bool = True
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.provider_name

    async def transcribe(
        self,
        audio_url: str,
        *,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        stereo_channels: bool = False,
    ) -> TranscriptionResult:
        self.calls.append(
            {
                "audio_url": audio_url,
                "language_hint": language_hint,
                "keyterms": list(keyterms or []),
                "stereo_channels": stereo_channels,
            }
        )
        if self.failures:
            raise self.failures.pop(0)
        return self.result or sample_result(provider=self.provider_name)

    async def health(self) -> bool:
        return self.healthy
