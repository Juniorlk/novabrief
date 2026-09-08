"""Rate limiting (section 17.3): 10 requests a minute on auth, 600 elsewhere.

The tight limit on authentication is the point. Sign-in is where an attacker
tries passwords and where an attacker probes which addresses are customers;
600 a minute would let both proceed comfortably.

Counters live in Redis so every API replica shares them — a per-process limiter
multiplies the real limit by the number of instances, which is the failure mode
that makes people think rate limiting is working when it is not.

The window is fixed rather than sliding. A fixed window lets a caller send up to
twice the limit across a boundary, which we accept: the alternative costs a
sorted set per client and the limits here exist to blunt abuse, not to meter
billing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.logging import get_logger

logger = get_logger(__name__)

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Decision:
    """Whether a request may proceed, and what to tell the caller."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimiter(Protocol):
    """Counts requests per caller per window."""

    async def hit(self, key: str, limit: int) -> Decision:
        """Record one request and say whether it is allowed."""
        ...


# After this many consecutive failures, stop calling Redis for a while.
_BREAKER_THRESHOLD = 3
# How long to skip Redis once the breaker opens.
_BREAKER_COOLDOWN_SECONDS = 10.0


class RedisRateLimiter:
    """Shared counters, so the limit is the same however many replicas run.

    Failing open is not enough on its own: failing open *slowly* is nearly as
    bad as failing closed. With a dead Redis, every request would otherwise
    wait for the client timeout before being allowed through, so an outage of
    the rate limiter becomes an outage of the API. A short circuit breaker
    stops calling Redis at all once it has clearly gone, and the client is
    built with aggressive timeouts so even the first few calls fail fast.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._consecutive_failures = 0
        self._skip_until = 0.0

    async def aclose(self) -> None:
        """Release the connection pool.

        Without this the sockets stay open until the process dies, which on a
        rolling restart means a pile of connections Redis still believes are
        live.
        """
        await self._redis.aclose()

    async def hit(self, key: str, limit: int) -> Decision:
        now = time.monotonic()
        if now < self._skip_until:
            # The breaker is open: allow without paying for a call we expect
            # to fail.
            return Decision(allowed=True, limit=limit, remaining=limit, retry_after=0)

        try:
            pipeline = self._redis.pipeline()
            pipeline.incr(key)
            # The expiry is set on every hit rather than only on the first.
            # Setting it once leaves a key immortal if the process dies between
            # INCR and EXPIRE, which locks that caller out for good.
            pipeline.expire(key, WINDOW_SECONDS)
            count, _ = await pipeline.execute()
        except (RedisError, OSError, TimeoutError):
            # Fail open. A rate limiter is a guard rail; refusing every request
            # when it breaks would turn a Redis outage into a full outage.
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._skip_until = now + _BREAKER_COOLDOWN_SECONDS
            logger.warning(
                "rate_limiter_unavailable",
                limiter="redis",
                consecutive_failures=self._consecutive_failures,
            )
            return Decision(allowed=True, limit=limit, remaining=limit, retry_after=0)

        self._consecutive_failures = 0
        used = int(count)
        return Decision(
            allowed=used <= limit,
            limit=limit,
            remaining=max(0, limit - used),
            retry_after=WINDOW_SECONDS if used > limit else 0,
        )


class InMemoryRateLimiter:
    """Process-local counters, for tests and for running without Redis.

    Not suitable for more than one replica: each process would grant the full
    allowance. It exists so a developer can run the API without Redis, and so
    the limiting logic can be tested without one.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, limit: int) -> Decision:
        now = time.monotonic()
        count, expires_at = self._counts.get(key, (0, 0.0))
        if now >= expires_at:
            count, expires_at = 0, now + WINDOW_SECONDS

        count += 1
        self._counts[key] = (count, expires_at)

        return Decision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after=int(expires_at - now) + 1 if count > limit else 0,
        )
