"""AssemblyAI, the primary transcription supplier (section 15.2).

Called with speaker labels, language detection (or a forced language when the
organization has set one), and the organization's lexicon as key terms. No
summarising, no topic detection, no sentiment: ADR-02 keeps analysis on our
side of the line.
"""

from __future__ import annotations

import asyncio
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

_BASE_URL = "https://api.assemblyai.com/v2"
# The supplier transcribes asynchronously: we submit, then poll. Three seconds
# is frequent enough that a short meeting is not held up by the polling itself.
_POLL_INTERVAL_SECONDS = 3.0
_CONNECT_TIMEOUT = 10.0


class AssemblyAIProvider(TranscriptionProvider):
    """Transcription through AssemblyAI."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "universal-3.5",
        timeout_seconds: float = 1800.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return f"assemblyai:{self._model}"

    def _open(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"authorization": self._api_key},
            timeout=httpx.Timeout(_CONNECT_TIMEOUT, read=60.0),
        )

    async def transcribe(
        self,
        audio_url: str,
        *,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        stereo_channels: bool = False,
    ) -> TranscriptionResult:
        payload: dict[str, Any] = {
            "audio_url": audio_url,
            "speaker_labels": True,
            "punctuate": True,
            "format_text": True,
            # EF-31 encodes the local track left and the remote right; keeping
            # the channels apart is what lets the author be told from the room.
            "multichannel": stereo_channels,
        }
        if language_hint:
            payload["language_code"] = language_hint
        else:
            payload["language_detection"] = True
        if keyterms:
            payload["keyterms_prompt"] = keyterms

        client = self._open()
        owned = self._client is None
        try:
            job_id = await self._submit(client, payload)
            completed = await self._await_completion(client, job_id)
        finally:
            if owned:
                await client.aclose()

        return _to_result(completed, provider=self.name)

    async def _submit(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> str:
        try:
            response = await client.post("/transcript", json=payload)
        except httpx.HTTPError as exc:
            message = f"could not reach AssemblyAI: {type(exc).__name__}"
            raise TranscriptionError(message) from exc

        if response.status_code >= 500:
            message = f"AssemblyAI is failing (HTTP {response.status_code})"
            raise TranscriptionError(message)
        if response.status_code >= 400:
            # A rejected request fails the same way at the next supplier, so
            # failing over would spend money to fail twice.
            message = f"AssemblyAI refused the request (HTTP {response.status_code})"
            raise TranscriptionError(message, retryable=False)

        return str(response.json()["id"])

    async def _await_completion(self, client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout

        while True:
            if loop.time() > deadline:
                # EF-45 puts the ceiling at ten minutes per hour of audio; past
                # it the supplier is not slow, it is stuck, and the fallback is
                # faster than waiting.
                message = "AssemblyAI exceeded its time budget"
                raise TranscriptionError(message)

            try:
                response = await client.get(f"/transcript/{job_id}")
            except httpx.HTTPError as exc:
                message = f"could not reach AssemblyAI: {type(exc).__name__}"
                raise TranscriptionError(message) from exc

            if response.status_code >= 500:
                message = f"AssemblyAI is failing (HTTP {response.status_code})"
                raise TranscriptionError(message)

            body: dict[str, Any] = response.json()
            status = body.get("status")
            if status == "completed":
                return body
            if status == "error":
                # The supplier's message quotes the job; it is not logged.
                message = "AssemblyAI could not transcribe this recording"
                raise TranscriptionError(message)

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def health(self) -> bool:
        client = self._open()
        owned = self._client is None
        try:
            response = await client.get("/transcript", params={"limit": 1})
            return response.status_code == 200
        except httpx.HTTPError:
            return False
        finally:
            if owned:
                await client.aclose()


def _to_result(body: dict[str, Any], *, provider: str) -> TranscriptionResult:
    """Map AssemblyAI's answer onto the shared shape."""
    utterances = [
        Utterance(
            speaker_tag=str(item.get("speaker") or "A"),
            start_ms=int(item.get("start", 0)),
            end_ms=int(item.get("end", 0)),
            text=str(item.get("text", "")),
            confidence=float(item.get("confidence", 0.0)),
            channel=_channel_of(item),
        )
        for item in body.get("utterances") or []
        if str(item.get("text", "")).strip()
    ]

    duration_ms = int(body.get("audio_duration") or 0) * 1000
    if not duration_ms and utterances:
        duration_ms = max(u.end_ms for u in utterances)

    return TranscriptionResult(
        language=str(body.get("language_code") or "fr")[:5],
        utterances=utterances,
        duration_seconds=duration_ms // 1000,
        provider=provider,
        # The supplier does not price a job in its response; the router prices
        # it from the duration so the ledger is never left empty (ADR-08).
        cost_usd=Decimal(0),
    )


def _channel_of(item: dict[str, Any]) -> Channel | None:
    """Which track this came from, when the audio was sent as two."""
    raw = item.get("channel")
    if raw is None:
        return None
    # Left is the microphone, right is everything the machine played (EF-31).
    return "local" if str(raw) in {"1", "left"} else "remote"
