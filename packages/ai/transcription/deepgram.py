"""Deepgram, the fallback transcription supplier (section 15.2, EF-45).

Exists so that an AssemblyAI outage is a slower meeting rather than a failed
one. It is also the hedge on a question nobody has answered: EF-41 asks for a
word error rate under 15 % on Cameroonian-accented French, and POC #2 — the
benchmark that would have established which supplier meets it — was skipped.
Having both wired means switching is configuration rather than a project.

Deepgram answers synchronously for pre-recorded audio, so there is no polling
here. Keyterm prompting is Nova-3's equivalent of AssemblyAI's key terms; if it
turns out to be unavailable for French, the fallback is `keywords`, and that is
a one-line change confined to this file.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from ai.transcription.base import (
    Channel,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
    Utterance,
)

_ENDPOINT = "https://api.deepgram.com/v1/listen"
_CONNECT_TIMEOUT = 10.0


class DeepgramProvider(TranscriptionProvider):
    """Transcription through Deepgram."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "nova-3",
        timeout_seconds: float = 1800.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return f"deepgram:{self._model}"

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            headers={"Authorization": f"Token {self._api_key}"},
            timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=self._timeout),
        )

    async def transcribe(
        self,
        audio_url: str,
        *,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        stereo_channels: bool = False,
    ) -> TranscriptionResult:
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("model", self._model),
            ("smart_format", "true"),
            ("punctuate", "true"),
            ("utterances", "true"),
            ("diarize", "true"),
            # The "euh" and "hum" of a meeting are noise in a written report.
            ("filler_words", "false"),
            # Never censor a meeting: a bleeped word in a board minute is worse
            # than the word.
            ("profanity_filter", "false"),
        ]
        params.append(("language", language_hint or "multi"))
        if stereo_channels:
            params.append(("multichannel", "true"))
        params.extend(("keyterm", term) for term in (keyterms or []))

        client = self._open()
        owned = self._client is None
        try:
            response = await client.post(_ENDPOINT, params=params, json={"url": audio_url})
        except httpx.HTTPError as exc:
            message = f"could not reach Deepgram: {type(exc).__name__}"
            raise TranscriptionError(message) from exc
        finally:
            if owned:
                await client.aclose()

        if response.status_code >= 500:
            message = f"Deepgram is failing (HTTP {response.status_code})"
            raise TranscriptionError(message)
        if response.status_code >= 400:
            message = f"Deepgram refused the request (HTTP {response.status_code})"
            raise TranscriptionError(message, retryable=False)

        return _to_result(response.json(), provider=self.name)

    async def health(self) -> bool:
        client = self._open()
        owned = self._client is None
        try:
            response = await client.get("https://api.deepgram.com/v1/projects")
            return response.status_code == 200
        except httpx.HTTPError:
            return False
        finally:
            if owned:
                await client.aclose()


def _to_result(body: dict[str, Any], *, provider: str) -> TranscriptionResult:
    """Map Deepgram's answer onto the shared shape."""
    results = body.get("results") or {}
    metadata = body.get("metadata") or {}

    utterances = [
        Utterance(
            speaker_tag=_tag(item.get("speaker")),
            start_ms=int(float(item.get("start", 0.0)) * 1000),
            end_ms=int(float(item.get("end", 0.0)) * 1000),
            text=str(item.get("transcript", "")),
            confidence=float(item.get("confidence", 0.0)),
            channel=_channel_of(item),
        )
        for item in results.get("utterances") or []
        if str(item.get("transcript", "")).strip()
    ]

    duration = float(metadata.get("duration") or 0.0)
    if not duration and utterances:
        duration = max(u.end_ms for u in utterances) / 1000

    detected = ""
    channels = results.get("channels") or []
    if channels:
        detected = str(channels[0].get("detected_language") or "")

    return TranscriptionResult(
        language=(detected or "fr")[:5],
        utterances=utterances,
        duration_seconds=int(duration),
        provider=provider,
        cost_usd=Decimal(0),
    )


def _tag(speaker: object) -> str:
    """Deepgram numbers speakers; the rest of the system letters them."""
    if speaker is None:
        return "A"
    try:
        index = int(str(speaker))
    except ValueError:
        return str(speaker)[:16]
    # 0 -> A, 1 -> B, and past Z fall back to the number rather than wrapping
    # round to A again, which would merge two people into one.
    return chr(ord("A") + index) if 0 <= index < 26 else f"S{index}"


def _channel_of(item: dict[str, Any]) -> Channel | None:
    raw = item.get("channel")
    if raw is None:
        return None
    return "local" if str(raw) == "0" else "remote"
