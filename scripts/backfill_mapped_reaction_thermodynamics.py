"""Rebuild materialized thermodynamic profiles for every mapped reaction."""

from __future__ import annotations

import argparse
import asyncio
from functools import partial
from typing import Any

from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reactions_thermodynamics,
)
from tricycle_reaction_db.db.models import MappedReaction
from tricycle_reaction_db.db.session import session_factory


def _backfill_batch(session: Any, *, offset: int, batch_size: int) -> tuple[int, int]:
    """Refresh one stable ID-ordered batch and return row/profile counts."""

    mapped_reactions = session.exec(
        select(MappedReaction)
        .order_by(col(MappedReaction.id))
        .offset(offset)
        .limit(batch_size)
    ).all()
    if not mapped_reactions:
        return 0, 0
    results = refresh_mapped_reactions_thermodynamics(session, mapped_reactions)
    return len(mapped_reactions), sum(len(result.profiles) for result in results)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="number of mapped reactions refreshed per transaction (default: 100)",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    return args


async def main() -> None:
    args = _parse_args()
    offset = 0
    mapped_count = 0
    profile_count = 0
    async with session_factory() as session:
        while True:
            batch_count, batch_profiles = await session.run_sync(
                partial(_backfill_batch, offset=offset, batch_size=args.batch_size)
            )
            if batch_count == 0:
                break
            await session.commit()
            offset += batch_count
            mapped_count += batch_count
            profile_count += batch_profiles
            print(
                f"backfilled {mapped_count} mapped reactions and {profile_count} profiles",
                flush=True,
            )
    print(f"backfilled {mapped_count} mapped reactions and {profile_count} profiles")


if __name__ == "__main__":
    asyncio.run(main())
