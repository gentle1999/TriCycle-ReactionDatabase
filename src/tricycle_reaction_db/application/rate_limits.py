"""Shared and process-local fixed-window request limiters."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol
from weakref import WeakSet

from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from tricycle_reaction_db.application.query_cost import (
    FixedWindowRateLimiter,
    RateLimitDecision,
)


class AsyncRateLimiter(Protocol):
    maximum_requests: int

    def check(self, key: str) -> Awaitable[RateLimitDecision]: ...


class ProcessFixedWindowRateLimiter:
    """Async facade over the bounded development-only process limiter."""

    def __init__(self, *, maximum_requests: int, window_seconds: int) -> None:
        self.maximum_requests = maximum_requests
        self._limiter = FixedWindowRateLimiter(
            maximum_requests=maximum_requests,
            window_seconds=window_seconds,
        )

    async def check(self, key: str) -> RateLimitDecision:
        return self._limiter.check(key)


class RateLimitBackendUnavailable(RuntimeError):
    """The shared limiter could not make an authoritative decision."""

    code = "rate_limit_backend_unavailable"
    message = "shared rate-limit backend is unavailable"

    def __init__(self) -> None:
        super().__init__(f"[{self.code}] {self.message}")


_REDIS_FIXED_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


class RedisFixedWindowRateLimiter:
    """Atomic fixed-window limiter shared by every process using one Redis."""

    def __init__(
        self,
        *,
        client: Any,
        maximum_requests: int,
        window_seconds: int,
        namespace: str,
    ) -> None:
        self._client = client
        self.maximum_requests = maximum_requests
        self.window_seconds = window_seconds
        self.namespace = namespace.strip(":")

    async def check(self, key: str) -> RateLimitDecision:
        redis_key = f"{self.namespace}:{key}"
        try:
            result = await self._client.eval(
                _REDIS_FIXED_WINDOW_SCRIPT,
                1,
                redis_key,
                self.window_seconds,
            )
            count, ttl = (int(result[0]), int(result[1]))
        except (RedisError, OSError, TypeError, ValueError) as error:
            raise RateLimitBackendUnavailable() from error
        retry_after = max(1, ttl if ttl > 0 else self.window_seconds)
        if count > self.maximum_requests:
            return RateLimitDecision(False, 0, retry_after)
        return RateLimitDecision(True, self.maximum_requests - count, 0)

    async def close(self) -> None:
        await self._client.aclose()


_redis_limiters: WeakSet[RedisFixedWindowRateLimiter] = WeakSet()


def create_rate_limiter(
    *,
    policy: str,
    maximum_requests: int,
    window_seconds: int,
    backend: str = "memory",
    redis_url: str | None = None,
    key_prefix: str = "reaction-database",
) -> AsyncRateLimiter:
    if backend == "memory":
        return ProcessFixedWindowRateLimiter(
            maximum_requests=maximum_requests,
            window_seconds=window_seconds,
        )
    if backend != "redis":
        raise ValueError(f"unsupported rate-limit backend: {backend}")
    if not redis_url:
        raise RuntimeError("Redis rate limiting requires TRICYCLE_RATE_LIMIT_REDIS_URL")
    client = Redis.from_url(
        redis_url,
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )
    limiter = RedisFixedWindowRateLimiter(
        client=client,
        maximum_requests=maximum_requests,
        window_seconds=window_seconds,
        namespace=f"{key_prefix}:{policy}",
    )
    _redis_limiters.add(limiter)
    return limiter


async def close_rate_limit_clients() -> None:
    limiters = list(_redis_limiters)
    _redis_limiters.clear()
    for limiter in limiters:
        await limiter.close()


__all__ = [
    "AsyncRateLimiter",
    "ProcessFixedWindowRateLimiter",
    "RateLimitBackendUnavailable",
    "RedisFixedWindowRateLimiter",
    "close_rate_limit_clients",
    "create_rate_limiter",
]
