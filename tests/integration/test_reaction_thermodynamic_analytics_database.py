import csv
import hashlib
import io
import json
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import col

from tricycle_reaction_db.application.services import ReactionThermodynamicAnalyticsService
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    MappedReaction,
    MappedReactionThermodynamicProfile,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import MappedReactionKind

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest.mark.asyncio
async def test_statistics_and_export_cover_the_same_visible_profiles(
    development_query_principal: object,
) -> None:
    del development_query_principal
    statistics = await ReactionThermodynamicAnalyticsService.statistics()
    export_stream = await ReactionThermodynamicAnalyticsService.export_csv()
    payload = "".join([chunk async for chunk in export_stream])
    rows = list(csv.DictReader(io.StringIO(payload)))

    assert len(rows) == statistics.profile_count
    assert sum(item.count for item in statistics.activation_gibbs_free_energy_kcal_mol) == (
        statistics.activation_profile_count
    )
    assert sum(item.count for item in statistics.reaction_gibbs_free_energy_kcal_mol) == (
        statistics.reaction_profile_count
    )
    assert len(statistics.scatter) == min(statistics.complete_profile_count, 1_000)


@pytest.mark.asyncio
async def test_statistics_and_export_share_logical_reaction_filters(
    development_query_principal: object,
) -> None:
    del development_query_principal
    suffix = uuid4().hex
    logical_reaction_ids: list[UUID] = []
    async with session_factory() as session:
        for index in range(2):
            digest = hashlib.sha256(f"analytics-filter:{suffix}:{index}".encode()).hexdigest()
            logical_reaction = LogicalReaction(
                reaction_key=f"analytics-filter-{suffix}-{index}",
                reaction_hash=digest,
            )
            session.add(logical_reaction)
            await session.flush()
            assert logical_reaction.id is not None
            logical_reaction_ids.append(logical_reaction.id)
            mapped_reaction = MappedReaction(
                logical_reaction_id=logical_reaction.id,
                mapped_reaction_key=f"analytics-filter-path-{suffix}-{index}",
                mapped_reaction_kind=MappedReactionKind.OTHER,
                mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
                mapping_hash=hashlib.sha256(f"mapping:{suffix}:{index}".encode()).hexdigest(),
            )
            session.add(mapped_reaction)
            await session.flush()
            assert mapped_reaction.id is not None
            session.add(
                MappedReactionThermodynamicProfile(
                    mapped_reaction_id=mapped_reaction.id,
                    policy_version="analytics-filter-test-v1",
                    source_key_hash=hashlib.sha256(
                        f"profile:{suffix}:{index}".encode()
                    ).hexdigest(),
                    electronic_level=["DFT", "B3LYP", "def2-SVP"],
                    thermochemistry_level=["DFT", "B3LYP", "def2-SVP"],
                    temperature_kelvin=298.15,
                    pressure_atm=1.0,
                    reactants={"fixture": index},
                    transition_state={"fixture": index},
                    products={"fixture": index},
                    reactants_enthalpy_hartree=-10.0,
                    reactants_gibbs_free_energy_hartree=-9.9,
                    reactants_entropy_cal_mol_k=10.0,
                    transition_state_enthalpy_hartree=-9.9,
                    transition_state_gibbs_free_energy_hartree=-9.8,
                    transition_state_entropy_cal_mol_k=11.0,
                    products_enthalpy_hartree=-10.1,
                    products_gibbs_free_energy_hartree=-10.0,
                    products_entropy_cal_mol_k=12.0,
                )
            )
        await session.commit()

    logical_reaction_id = logical_reaction_ids[0]
    reaction_hash = hashlib.sha256(f"analytics-filter:{suffix}:0".encode()).hexdigest()
    try:
        filter_expression = json.dumps(
            {
                "operator": "and",
                "conditions": [{"field": "reaction_hash", "value": reaction_hash}],
            }
        )

        statistics = await ReactionThermodynamicAnalyticsService.statistics(
            filter_expression=filter_expression,
        )
        export_stream = await ReactionThermodynamicAnalyticsService.export_csv(
            filter_expression=filter_expression,
        )
        rows = list(csv.DictReader(io.StringIO("".join([chunk async for chunk in export_stream]))))

        assert statistics.profile_count == 1
        assert len(rows) == statistics.profile_count
        assert {row["logical_reaction_id"] for row in rows} == {str(logical_reaction_id)}
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(LogicalReaction).where(col(LogicalReaction.id).in_(logical_reaction_ids))
            )
            await session.commit()
