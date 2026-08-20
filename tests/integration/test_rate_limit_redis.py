import asyncio
import os
from contextlib import suppress
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from tricycle_reaction_db.application.rate_limits import (
    RateLimitBackendUnavailable,
    close_rate_limit_clients,
    create_rate_limiter,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_REDIS_TESTS") != "1",
        reason="set TRICYCLE_RUN_REDIS_TESTS=1 to run Redis tests",
    ),
]


@pytest.mark.asyncio
async def test_real_redis_limiters_share_and_expire_one_atomic_budget() -> None:
    redis_url = os.getenv("TRICYCLE_RATE_LIMIT_REDIS_URL", "redis://127.0.0.1:6379/15")
    key_prefix = f"reaction-database-integration-{uuid4()}"
    subject = "user:shared-budget"
    redis_key = f"{key_prefix}:read:{subject}"
    first = create_rate_limiter(
        policy="read",
        maximum_requests=2,
        window_seconds=1,
        backend="redis",
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    second = create_rate_limiter(
        policy="read",
        maximum_requests=2,
        window_seconds=1,
        backend="redis",
        redis_url=redis_url,
        key_prefix=key_prefix,
    )
    inspection_client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )

    try:
        assert (await first.check(subject)).remaining == 1
        assert (await second.check(subject)).remaining == 0
        rejected = await first.check(subject)

        assert rejected.allowed is False
        assert rejected.retry_after_seconds == 1
        assert await inspection_client.get(redis_key) == "3"
        assert await inspection_client.ttl(redis_key) in {0, 1}

        await asyncio.sleep(1.1)

        reset = await second.check(subject)
        assert reset.allowed is True
        assert reset.remaining == 1
    finally:
        with suppress(RedisError, OSError):
            await inspection_client.delete(redis_key)
        await inspection_client.aclose()
        await close_rate_limit_clients()


@pytest.mark.asyncio
async def test_real_redis_connection_failure_is_fail_closed() -> None:
    limiter = create_rate_limiter(
        policy="read",
        maximum_requests=2,
        window_seconds=1,
        backend="redis",
        redis_url="redis://127.0.0.1:1/15",
        key_prefix=f"reaction-database-integration-{uuid4()}",
    )

    try:
        with pytest.raises(RateLimitBackendUnavailable):
            await limiter.check("user:backend-down")
    finally:
        await close_rate_limit_clients()
