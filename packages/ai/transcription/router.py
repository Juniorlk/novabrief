"""Choosing a supplier, and giving up on one (EF-45, section 18.1).

The router is what turns two providers into an availability guarantee. It
tries them in the configured order, and it remembers which ones are failing so
that a supplier having a bad hour is skipped instead of being asked again on
every meeting.

The breaker's rule comes from section 18.1: open after five failures inside ten
minutes, half-open after two minutes. Half-open matters as much as open — a
breaker that never re-tries is just a supplier you turned off, and nobody would
notice it had come back.

**The state is per process.** A worker learns which suppliers are down from its
own failures, not from its colleagues'. That is a deliberate simplification: a
shared breaker means a round trip to Redis in the hot path and a new way for
the whole fleet to be wrong at once. The cost is that each worker pays its own
five failures. With the fleet this size that is a handful of extra calls, not a
design flaw — but the back-office of lot L6 is meant to display breaker state,
and displaying a per-process value would be misleading, so it will need
promoting to shared state then.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from ai.transcription.base import (
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
)

# Section 18.1.
FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 600.0
HALF_OPEN_AFTER_SECONDS = 120.0


class AllProvidersFailedError(TranscriptionError):
    """Every supplier in the chain refused or failed."""

    def __init__(self, attempts: dict[str, str]) -> None:
        self.attempts = attempts
        summary = ", ".join(f"{name}: {reason}" for name, reason in attempts.items())
        super().__init__(f"no transcription provider succeeded ({summary})")


@dataclass
class Breaker:
    """One supplier's recent history."""

    failures: deque[float] = field(default_factory=deque)
    opened_at: float | None = None

    def record_failure(self, *, now: float) -> None:
        self.failures.append(now)
        self._forget_old(now=now)
        if len(self.failures) >= FAILURE_THRESHOLD:
            self.opened_at = now

    def record_success(self, *, now: float) -> None:
        # A success closes it outright rather than decrementing: the point of
        # half-open is to ask "is it back?", and one clear yes is the answer.
        self.failures.clear()
        self.opened_at = None

    def is_open(self, *, now: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= HALF_OPEN_AFTER_SECONDS:
            # Half-open: let exactly one call through to find out. Whether it
            # succeeds or fails, the next call sees an updated state.
            self.opened_at = None
            self.failures.clear()
            return False
        return True

    def _forget_old(self, *, now: float) -> None:
        while self.failures and now - self.failures[0] > FAILURE_WINDOW_SECONDS:
            self.failures.popleft()


class TranscriptionRouter:
    """Tries the configured suppliers in order, skipping the ones that are down."""

    def __init__(
        self,
        providers: list[TranscriptionProvider],
        *,
        price_per_hour_usd: Decimal | None = None,
    ) -> None:
        if not providers:
            message = "a router needs at least one provider"
            raise ValueError(message)
        self._providers = providers
        # Priced per hour of audio because that is how both suppliers bill and
        # neither returns a cost in its response. Configuration, not a
        # constant: ADR-09 keeps rates out of the code.
        self._price_per_hour = price_per_hour_usd
        self._breakers: dict[str, Breaker] = {p.name: Breaker() for p in providers}

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    def is_available(self, name: str) -> bool:
        """Whether this supplier would be tried right now."""
        breaker = self._breakers.get(name)
        return breaker is None or not breaker.is_open(now=time.monotonic())

    async def transcribe(
        self,
        audio_url: str,
        *,
        language_hint: str | None = None,
        keyterms: list[str] | None = None,
        stereo_channels: bool = False,
    ) -> tuple[TranscriptionResult, bool]:
        """Transcribe, returning the result and whether a fallback was used.

        The flag is not decoration: section 11 gives FALLBACK_STT its own state
        so an operator can see that a meeting was rescued rather than served
        normally, and EF-45 asks for the incident to be traced.
        """
        attempts: dict[str, str] = {}
        used_fallback = False

        for index, provider in enumerate(self._providers):
            now = time.monotonic()
            breaker = self._breakers[provider.name]
            if breaker.is_open(now=now):
                attempts[provider.name] = "circuit open"
                continue

            try:
                result = await provider.transcribe(
                    audio_url,
                    language_hint=language_hint,
                    keyterms=keyterms,
                    stereo_channels=stereo_channels,
                )
            except TranscriptionError as exc:
                attempts[provider.name] = str(exc)
                if not exc.retryable:
                    # The request itself is wrong. Another supplier would
                    # reject it identically, so stop rather than pay twice.
                    raise
                breaker.record_failure(now=time.monotonic())
                used_fallback = True
                continue

            breaker.record_success(now=time.monotonic())
            return self._priced(result), index > 0 or used_fallback

        raise AllProvidersFailedError(attempts)

    def _priced(self, result: TranscriptionResult) -> TranscriptionResult:
        """Fill in the cost, since neither supplier reports one.

        Left at zero when no rate is configured rather than guessed: a made-up
        figure in `usage_ledger` would quietly corrupt the margin dashboard,
        which is worse than an obvious hole in it.
        """
        if self._price_per_hour is None or result.cost_usd:
            return result
        hours = Decimal(result.duration_seconds) / Decimal(3600)
        return result.model_copy(
            update={"cost_usd": (hours * self._price_per_hour).quantize(Decimal("0.00001"))}
        )
