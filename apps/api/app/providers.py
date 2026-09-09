"""Building the AI providers from configuration (ADR-01).

One place decides which suppliers exist and in what order, so business code
never names one and the back-office of lot L6 can reorder them without a
deployment.

A missing key is not silently tolerated in production. A worker that starts
with no transcription provider would accept meetings, fail every one of them,
and look like a supplier outage. It refuses to start instead.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from ai.llm import LLMProvider, OpenAIProvider
from ai.transcription import (
    AssemblyAIProvider,
    DeepgramProvider,
    TranscriptionProvider,
    TranscriptionRouter,
)
from app.config import Settings
from app.logging import get_logger

logger = get_logger(__name__)


def _assemblyai(settings: Settings) -> TranscriptionProvider:
    return AssemblyAIProvider(
        api_key=settings.assemblyai_api_key or "",
        model=settings.assemblyai_model,
        timeout_seconds=settings.transcription_timeout_seconds,
    )


def _deepgram(settings: Settings) -> TranscriptionProvider:
    return DeepgramProvider(
        api_key=settings.deepgram_api_key or "",
        model=settings.deepgram_model,
        timeout_seconds=settings.transcription_timeout_seconds,
    )


_BUILDERS: dict[str, Callable[[Settings], TranscriptionProvider]] = {
    "assemblyai": _assemblyai,
    "deepgram": _deepgram,
}

_KEYS: dict[str, Callable[[Settings], str | None]] = {
    "assemblyai": lambda s: s.assemblyai_api_key,
    "deepgram": lambda s: s.deepgram_api_key,
}


def build_transcription_router(settings: Settings) -> TranscriptionRouter:
    """The configured chain of transcription suppliers, in preference order.

    Suppliers without a key are left out rather than added and left to fail:
    an empty slot in the chain would burn a retry and a circuit-breaker slot on
    every meeting for no possible benefit.
    """
    providers: list[TranscriptionProvider] = []
    skipped: list[str] = []

    for name in settings.transcription_order:
        builder = _BUILDERS.get(name)
        if builder is None:
            logger.warning("unknown_transcription_provider", provider=name)
            continue
        if not _KEYS[name](settings):
            skipped.append(name)
            continue
        providers.append(builder(settings))

    if skipped:
        # Loud, because a fallback that is quietly absent is worse than no
        # fallback at all: EF-45 would be believed to hold when it does not.
        logger.warning("transcription_provider_without_key", providers=skipped)

    if not providers:
        message = (
            "no transcription provider is configured; set ASSEMBLYAI_API_KEY "
            "or DEEPGRAM_API_KEY before starting a worker"
        )
        raise RuntimeError(message)

    price = (
        Decimal(str(settings.transcription_price_per_hour_usd))
        if settings.transcription_price_per_hour_usd is not None
        else None
    )
    return TranscriptionRouter(providers, price_per_hour_usd=price)


def build_llm(settings: Settings) -> LLMProvider:
    """The configured analysis model.

    One vendor today. The abstraction is not speculative: ADR-01 requires the
    business code never to name one, and swapping models is the single most
    likely change this system will face.
    """
    if settings.llm_provider != "openai":
        message = f"unknown LLM provider {settings.llm_provider!r}"
        raise RuntimeError(message)
    if not settings.openai_api_key:
        message = "OPENAI_API_KEY is not set; meetings cannot be analysed"
        raise RuntimeError(message)

    return OpenAIProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model_default,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
        price_per_million_in_usd=(
            Decimal(str(settings.llm_price_per_million_in_usd))
            if settings.llm_price_per_million_in_usd is not None
            else None
        ),
        price_per_million_out_usd=(
            Decimal(str(settings.llm_price_per_million_out_usd))
            if settings.llm_price_per_million_out_usd is not None
            else None
        ),
    )
