"""Delete expired and retained revoked browser sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta

from tricycle_reaction_db.application.services.authentication import AuthenticationService
from tricycle_reaction_db.db.session import dispose_engine


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--revoked-retention-days",
        type=int,
        default=30,
        help="retain revoked sessions for this many days (default: 30)",
    )
    arguments = parser.parse_args()
    if arguments.revoked_retention_days < 0:
        parser.error("--revoked-retention-days must be non-negative")
    return arguments


async def _cleanup(revoked_retention_days: int) -> int:
    try:
        return await AuthenticationService.cleanup_sessions(
            revoked_retention=timedelta(days=revoked_retention_days)
        )
    finally:
        await dispose_engine()


def main() -> None:
    arguments = _arguments()
    deleted_count = asyncio.run(_cleanup(arguments.revoked_retention_days))
    print(
        json.dumps(
            {
                "deleted_count": deleted_count,
                "revoked_retention_days": arguments.revoked_retention_days,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
