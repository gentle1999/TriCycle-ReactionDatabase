"""Bounded retries for transactions PostgreSQL explicitly rolled back."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from functools import wraps

TRANSACTION_RETRY_LIMIT = 3
logger = logging.getLogger(__name__)


def is_transaction_conflict(error: Exception) -> bool:
    driver_error = getattr(error, "orig", error)
    return getattr(driver_error, "sqlstate", None) in {"40P01", "40001"}


async def wait_for_transaction_retry(attempt: int) -> None:
    await asyncio.sleep(0.1 * 2**attempt + random.uniform(0, 0.1))


def retry_transaction[**P, T](operation: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Retry only operations that own and close their entire transaction.

    Never use this around a statement in a live session, external side effects,
    or operations that commit multiple windows without tracking commit receipts.
    Transport errors are excluded because their commit outcome may be unknown.
    """

    @wraps(operation)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        for attempt in range(TRANSACTION_RETRY_LIMIT + 1):
            try:
                return await operation(*args, **kwargs)
            except Exception as error:
                if not is_transaction_conflict(error) or attempt == TRANSACTION_RETRY_LIMIT:
                    raise
                logger.warning(
                    "retrying rolled-back transaction operation=%s attempt=%d",
                    operation.__qualname__,
                    attempt + 1,
                )
                await wait_for_transaction_retry(attempt)
        raise AssertionError("unreachable transaction retry")

    return wrapped
