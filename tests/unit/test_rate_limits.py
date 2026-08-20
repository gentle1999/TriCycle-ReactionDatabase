from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from redis.exceptions import RedisError

from tricycle_reaction_db.application.rate_limits import (
    RateLimitBackendUnavailable,
    RedisFixedWindowRateLimiter,
    create_rate_limiter,
)


@dataclass
class _SharedRedisState:
    counts: dict[str, int] = field(default_factory=dict)
    ttl: int = 30


class _FakeRedisClient:
    def __init__(self, state: _SharedRedisState) -> None:
        self.state = state
        self.closed = False

    async def eval(
        self,
        script: str,
        number_of_keys: int,
        key: str,
        window_seconds: int,
    ) -> list[int]:
        assert "INCR" in script
        assert "EXPIRE" in script
        assert number_of_keys == 1
        self.state.counts[key] = self.state.counts.get(key, 0) + 1
        return [self.state.counts[key], min(self.state.ttl, window_seconds)]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_limiters_share_one_atomic_budget_and_reset() -> None:
    state = _SharedRedisState(ttl=18)
    first = RedisFixedWindowRateLimiter(
        client=_FakeRedisClient(state),
        maximum_requests=2,
        window_seconds=30,
        namespace="deployment:read",
    )
    second = RedisFixedWindowRateLimiter(
        client=_FakeRedisClient(state),
        maximum_requests=2,
        window_seconds=30,
        namespace="deployment:read",
    )

    assert (await first.check("user:123")).remaining == 1
    assert (await second.check("user:123")).remaining == 0
    rejected = await first.check("user:123")

    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 18
    assert state.counts == {"deployment:read:user:123": 3}

    state.counts.clear()
    reset = await second.check("user:123")
    assert reset.allowed is True
    assert reset.remaining == 1


class _UnavailableRedisClient:
    async def eval(self, *_args: Any) -> None:
        raise RedisError("connection refused")


@pytest.mark.asyncio
async def test_redis_limiter_fails_closed_when_backend_is_unavailable() -> None:
    limiter = RedisFixedWindowRateLimiter(
        client=_UnavailableRedisClient(),
        maximum_requests=2,
        window_seconds=30,
        namespace="deployment:read",
    )

    with pytest.raises(RateLimitBackendUnavailable):
        await limiter.check("user:123")


def test_redis_factory_disables_client_retries_for_fast_failure() -> None:
    with patch("tricycle_reaction_db.application.rate_limits.Redis.from_url") as from_url:
        create_rate_limiter(
            policy="read",
            maximum_requests=2,
            window_seconds=30,
            backend="redis",
            redis_url="redis://127.0.0.1:6379/15",
        )

    kwargs = from_url.call_args.kwargs
    assert kwargs["socket_connect_timeout"] == 2
    assert kwargs["socket_timeout"] == 2
    assert kwargs["retry"].get_retries() == 0
