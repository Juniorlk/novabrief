"""The transcription contract (ADR-01, section 18.1).

Business code never names a supplier. It asks a `TranscriptionProvider` for a
`TranscriptionResult`, and which company answered is configuration — that is
what lets the preference order change from the back-office without a
deployment, and what makes the fallback of EF-45 possible at all.

Two things are deliberately *not* in this interface.

**No audio bytes.** A provider is handed a presigned URL and fetches the
recording itself. Streaming hundreds of megabytes through our own process to
hand it straight back out would double the bandwidth and put meeting audio in
the API's memory for no gain.

**No summarising.** Both suppliers offer it. ADR-02 separates transcription
from intelligence on purpose: the analysis happens on text, by a model we
prompt and whose output we validate, so that every decision and task can be
traced to a timestamp. A supplier-side summary would be untraceable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Channel = Literal["local", "remote", "mixed"]


class Utterance(BaseModel):
    """One diarised stretch of speech.

    `speaker_tag` is the provider's own label (A, B, C…), normalised by the
    router. The human name, when the meeting reveals one, is resolved later:
    a provider cannot know that "merci Cabrel" names the speaker who just
    stopped talking.
    """

    model_config = ConfigDict(extra="forbid")

    speaker_tag: str = Field(min_length=1, max_length=16)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    # EF-31 keeps the local and remote tracks apart until encoding precisely so
    # this can be known; it is what tells the author from the other attendees.
    channel: Channel | None = None


class TranscriptionResult(BaseModel):
    """What a provider returns, whichever provider it was."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(min_length=2, max_length=5)
    utterances: list[Utterance]
    duration_seconds: int = Field(ge=0)
    # "assemblyai:universal-3.5", "deepgram:nova-3" — the model as well as the
    # company, because a quality regression usually comes from a model change.
    provider: str = Field(min_length=1, max_length=40)
    # What this call actually cost, written to usage_ledger (ADR-08). Decimal:
    # summed over thousands of meetings, a float drifts.
    cost_usd: Decimal = Decimal(0)

    @property
    def full_text(self) -> str:
        """The transcript as one block, for the analysis step."""
        return "\n".join(f"{u.speaker_tag}: {u.text}" for u in self.utterances)


class TranscriptionError(Exception):
    """A provider failed. Carries whether trying another one makes sense."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        # Retryable means "this looks like the supplier, not the request".
        # A malformed request fails identically at the next provider, so
        # failing over would only spend money to fail twice.
        self.retryable = retryable
        super().__init__(message)


class TranscriptionProvider(Protocol):
    """Turns a recording into diarised, timestamped text."""

    @property
    def name(self) -> str:
        """Short identifier, used in logs and in the ledger."""
        ...

    async def transcribe(
        self,
        audio_url: str,
        *,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        stereo_channels: bool = False,
    ) -> TranscriptionResult:
        """Transcribe the recording at `audio_url`.

        `language_hint` forces a language when the organization has set one;
        without it the provider detects (EF-41). `keyterms` is the
        organization's lexicon (EF-05), which is what makes local proper nouns
        come back spelled correctly.
        """
        ...

    async def health(self) -> bool:
        """Whether the supplier is answering at all."""
        ...
