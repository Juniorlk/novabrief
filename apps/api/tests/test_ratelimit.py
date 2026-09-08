"""Rate limiting (section 17.3).

The behaviour that matters is not "does it count" but what it does at the
edges: what happens on the boundary, what happens when Redis is down, and who
shares an allowance with whom.
"""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.ratelimit import Decision, InMemoryRateLimiter, RedisRateLimiter

pytestmark = pytest.mark.asyncio


async def test_requests_under_the_limit_are_allowed() -> None:
    limiter = InMemoryRateLimiter()

    for _ in range(10):
        decision = await limiter.hit("caller", limit=10)
        assert decision.allowed


async def test_the_request_after_the_limit_is_refused() -> None:
    limiter = InMemoryRateLimiter()
    for _ in range(10):
        await limiter.hit("caller", limit=10)

    decision = await limiter.hit("caller", limit=10)

    assert not decision.allowed
    assert decision.retry_after > 0


async def test_the_remaining_count_counts_down() -> None:
    """Clients back off on their own when told how much is left."""
    limiter = InMemoryRateLimiter()

    first = await limiter.hit("caller", limit=10)
    second = await limiter.hit("caller", limit=10)

    assert first.remaining == 9
    assert second.remaining == 8


async def test_remaining_never_goes_negative() -> None:
    limiter = InMemoryRateLimiter()
    for _ in range(15):
        decision = await limiter.hit("caller", limit=10)

    assert decision.remaining == 0


async def test_callers_do_not_share_an_allowance() -> None:
    """One customer exhausting their quota must not lock out another."""
    limiter = InMemoryRateLimiter()
    for _ in range(11):
        await limiter.hit("noisy", limit=10)

    decision = await limiter.hit("quiet", limit=10)

    assert decision.allowed


class _BrokenRedis:
    """A Redis whose every call fails, the way an outage looks."""

    def pipeline(self) -> _BrokenRedis:
        return self

    def incr(self, key: str) -> None:
        pass

    def expire(self, key: str, seconds: int) -> None:
        pass

    async def execute(self) -> None:
        raise RedisConnectionError("connection refused")


async def test_a_redis_outage_lets_traffic_through() -> None:
    """The limiter fails open, on purpose.

    It is a guard rail, not a gate. Refusing every request when Redis is
    unreachable would turn a cache outage into a full outage — the limiter
    causing the incident it exists to soften.
    """
    limiter = RedisRateLimiter(_BrokenRedis())  # type: ignore[arg-type]

    decision = await limiter.hit("caller", limit=10)

    assert decision.allowed
    assert decision.remaining == 10


async def test_the_decision_reports_the_configured_limit() -> None:
    limiter = InMemoryRateLimiter()

    decision = await limiter.hit("caller", limit=600)

    assert decision.limit == 600
    assert isinstance(decision, Decision)


async def test_the_breaker_stops_calling_a_dead_redis() -> None:
    """Failing open slowly is nearly as bad as failing closed.

    With a dead Redis every request would otherwise wait for the client
    timeout before being let through, so an outage of the limiter becomes an
    outage of the API. After a few failures the limiter stops trying.
    """
    broken = _CountingBrokenRedis()
    limiter = RedisRateLimiter(broken)  # type: ignore[arg-type]

    for _ in range(20):
        decision = await limiter.hit("caller", limit=10)
        assert decision.allowed

    # Three failures open the breaker; the remaining calls skip Redis entirely.
    assert broken.attempts == 3, f"expected to stop after 3 attempts, made {broken.attempts}"


class _CountingBrokenRedis(_BrokenRedis):
    """A broken Redis that records how often it was actually called."""

    def __init__(self) -> None:
        self.attempts = 0

    def pipeline(self) -> _CountingBrokenRedis:
        self.attempts += 1
        return self
