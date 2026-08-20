"""Rebuild materialized thermodynamic profiles for every mapped reaction."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlmodel import col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.db.models import MappedReaction
from tricycle_reaction_db.db.session import session_factory


def _backfill(session: Any) -> tuple[int, int]:
    mapped_reactions = session.exec(select(MappedReaction).order_by(col(MappedReaction.id))).all()
    profile_count = 0
    for mapped_reaction in mapped_reactions:
        result = refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
        profile_count += len(result.profiles)
    return len(mapped_reactions), profile_count


async def main() -> None:
    async with session_factory() as session:
        mapped_count, profile_count = await session.run_sync(_backfill)
        await session.commit()
    print(f"backfilled {mapped_count} mapped reactions and {profile_count} profiles")


if __name__ == "__main__":
    asyncio.run(main())
