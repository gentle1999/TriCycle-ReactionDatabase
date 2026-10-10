import csv
import hashlib
import io
import json
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, update
from sqlmodel import Session, col, select
from test_domain_query_filters import _create_domain_sample, _delete_domain_sample

from tricycle_reaction_db.application.services import ReactionThermodynamicAnalyticsService
from tricycle_reaction_db.core.chemistry_config import MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    MappedReaction,
    MappedReactionThermodynamicProfile,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import MappedReactionKind
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID

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
    statistics = await ReactionThermodynamicAnalyticsService.statistics(
        project_id=SYSTEM_PROJECT_ID,
    )
    export_stream = await ReactionThermodynamicAnalyticsService.export_csv(
        project_id=SYSTEM_PROJECT_ID,
    )
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
    source_sample: tuple[object, ...] | None = None
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        # A profile is only visible when its state points at project-visible
        # source evidence. Reuse the canonical domain fixture so this test
        # exercises the analytics filter rather than constructing an
        # intentionally unprovenanced profile.
        with Session(engine, expire_on_commit=False) as source_session:
            source_sample = _create_domain_sample(source_session)
            source_geometry = source_sample[4]
            assert source_geometry.id is not None
            profile_state = {"topologies": [{"geometry_id": str(source_geometry.id)}]}

        async with session_factory() as session:
            for index in range(2):
                digest = hashlib.sha256(f"analytics-filter:{suffix}:{index}".encode()).hexdigest()
                logical_reaction = LogicalReaction(
                    project_id=SYSTEM_PROJECT_ID,
                    reaction_key=f"analytics-filter-{suffix}-{index}",
                    reaction_hash=digest,
                )
                session.add(logical_reaction)
                await session.flush()
                assert logical_reaction.id is not None
                logical_reaction_ids.append(logical_reaction.id)
                mapped_reaction = MappedReaction(
                    logical_reaction_id=logical_reaction.id,
                    project_id=SYSTEM_PROJECT_ID,
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
                        policy_version=MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
                        source_key_hash=hashlib.sha256(
                            f"profile:{suffix}:{index}".encode()
                        ).hexdigest(),
                        electronic_level=["DFT", "B3LYP", "def2-SVP"],
                        thermochemistry_level=["DFT", "B3LYP", "def2-SVP"],
                        temperature_kelvin=298.15,
                        pressure_atm=1.0,
                        reactants=profile_state,
                        transition_state=profile_state,
                        products=profile_state,
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
        filter_expression = json.dumps(
            {
                "operator": "and",
                "conditions": [{"field": "reaction_hash", "value": reaction_hash}],
            }
        )

        statistics = await ReactionThermodynamicAnalyticsService.statistics(
            project_id=SYSTEM_PROJECT_ID,
            filter_expression=filter_expression,
        )
        export_stream = await ReactionThermodynamicAnalyticsService.export_csv(
            project_id=SYSTEM_PROJECT_ID,
            filter_expression=filter_expression,
        )
        rows = list(csv.DictReader(io.StringIO("".join([chunk async for chunk in export_stream]))))

        assert statistics.profile_count == 1
        assert len(rows) == statistics.profile_count
        assert {row["logical_reaction_id"] for row in rows} == {str(logical_reaction_id)}

        # Old policy materializations must not reappear in either read path.
        async with session_factory() as session:
            await session.execute(
                update(MappedReactionThermodynamicProfile)
                .values(policy_version="legacy-test")
                .where(
                    col(MappedReactionThermodynamicProfile.policy_version)
                    == MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
                )
                .where(
                    col(MappedReactionThermodynamicProfile.mapped_reaction_id).in_(
                        select(col(MappedReaction.id)).where(
                            col(MappedReaction.logical_reaction_id).in_(logical_reaction_ids)
                        )
                    )
                )
            )
            await session.commit()
        stale_statistics = await ReactionThermodynamicAnalyticsService.statistics(
            project_id=SYSTEM_PROJECT_ID,
            filter_expression=filter_expression,
        )
        stale_export = await ReactionThermodynamicAnalyticsService.export_csv(
            project_id=SYSTEM_PROJECT_ID,
            filter_expression=filter_expression,
        )
        assert stale_statistics.profile_count == 0
        assert (
            list(csv.DictReader(io.StringIO("".join([chunk async for chunk in stale_export]))))
            == []
        )
    finally:
        async with session_factory() as session:
            if logical_reaction_ids:
                await session.execute(
                    delete(LogicalReaction).where(col(LogicalReaction.id).in_(logical_reaction_ids))
                )
            await session.commit()
        if source_sample is not None:
            with Session(engine) as source_session:
                _delete_domain_sample(source_session, source_sample)
        engine.dispose()


@pytest.mark.asyncio
async def test_export_filters_each_mapping_and_profile_within_one_logical_reaction(
    development_query_principal: object,
) -> None:
    del development_query_principal
    suffix = uuid4().hex
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    logical_id = uuid4()
    mapped_ids = [uuid4() for _ in range(3)]
    source_sample = None
    try:
        with Session(engine, expire_on_commit=False) as source_session:
            source_sample = _create_domain_sample(source_session)
            source_geometry = source_sample[4]
            state = {"topologies": [{"geometry_id": str(source_geometry.id)}]}
        async with session_factory() as session:
            session.add(
                LogicalReaction(
                    id=logical_id,
                    project_id=SYSTEM_PROJECT_ID,
                    reaction_key=f"export-siblings:{suffix}",
                    reaction_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                )
            )
            await session.flush()
            for index, smiles in enumerate(
                [
                    "[CH4:1]>>[CH4:1]",
                    "[NH3:1]>>[NH3:1]",
                    "[CH4:1]>>[CH4:1]",
                ]
            ):
                session.add(
                    MappedReaction(
                        id=mapped_ids[index],
                        project_id=SYSTEM_PROJECT_ID,
                        logical_reaction_id=logical_id,
                        mapped_reaction_key=f"export-siblings:{suffix}:{index}",
                        label=f"export-siblings:{suffix}:{index}",
                        mapped_reaction_kind=MappedReactionKind.OTHER,
                        mapped_reaction_smiles=smiles,
                        mapping_hash=hashlib.sha256(f"{suffix}:{index}".encode()).hexdigest(),
                    )
                )
                await session.flush()
                # Two profiles of the first mapping straddle the energy range.
                # The other mappings lack different energy fields.
                for ordinal in range(2 if index == 0 else 1):
                    session.add(
                        MappedReactionThermodynamicProfile(
                            mapped_reaction_id=mapped_ids[index],
                            policy_version=MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION,
                            source_key_hash=hashlib.sha256(
                                f"profile:{suffix}:{index}:{ordinal}".encode()
                            ).hexdigest(),
                            electronic_level=["test"],
                            thermochemistry_level=["test"],
                            temperature_kelvin=298.15 + ordinal,
                            pressure_atm=1.0,
                            reactants=state,
                            transition_state=state if index != 1 else None,
                            products=state if index != 2 else None,
                            reactants_gibbs_free_energy_hartree=-10.0,
                            transition_state_gibbs_free_energy_hartree=-9.9 if index != 1 else None,
                            products_gibbs_free_energy_hartree=(
                                None
                                if index == 2
                                else -10.1
                                if index == 0 and ordinal == 0
                                else -9.7
                            ),
                        )
                    )
            await session.commit()

        carbon = {"field": "reaction_smarts", "value": "C>>C"}
        nitrogen = {"field": "reaction_smarts", "value": "N>>N"}
        identity = {"field": "reaction_hash", "value": hashlib.sha256(suffix.encode()).hexdigest()}

        async def check(
            condition: dict[str, object] | None,
            expected: set[UUID],
            expected_rows: int,
            **options: bool,
        ) -> None:
            expression = json.dumps(
                {
                    "operator": "and",
                    "conditions": [identity, *([condition] if condition is not None else [])],
                }
            )
            stream = await ReactionThermodynamicAnalyticsService.export_csv(
                SYSTEM_PROJECT_ID,
                filter_expression=expression,
                **options,
            )
            rows = list(csv.DictReader(io.StringIO("".join([chunk async for chunk in stream]))))
            stats = await ReactionThermodynamicAnalyticsService.statistics(
                SYSTEM_PROJECT_ID,
                filter_expression=expression,
                **options,
            )
            assert len(rows) == stats.profile_count == expected_rows
            assert {UUID(row["mapped_reaction_id"]) for row in rows} == expected
            assert stats.mapped_reaction_count == len(expected)

        await check(carbon, {mapped_ids[0], mapped_ids[2]}, 3)
        await check({"operator": "and", "conditions": [carbon, nitrogen]}, set(), 0)
        await check({"operator": "not", "conditions": [carbon]}, {mapped_ids[1]}, 1)
        await check({**carbon, "negated": True}, {mapped_ids[1]}, 1)
        await check({"operator": "or", "conditions": [carbon, nitrogen]}, set(mapped_ids), 4)
        await check(
            None,
            {mapped_ids[0]},
            2,
            has_activation_gibbs_free_energy=True,
            has_reaction_gibbs_free_energy=True,
        )
        await check(
            {
                "operator": "and",
                "conditions": [
                    carbon,
                    {
                        "field": "minimum_reaction_gibbs_free_energy_kcal_mol",
                        "value": 0,
                    },
                ],
            },
            {mapped_ids[0]},
            1,
        )
        await check(
            {
                "operator": "and",
                "conditions": [
                    carbon,
                    {
                        "field": "minimum_reaction_gibbs_free_energy_kcal_mol",
                        "value": 0,
                    },
                    {
                        "field": "maximum_reaction_gibbs_free_energy_kcal_mol",
                        "value": 100,
                    },
                ],
            },
            set(),
            0,
        )
        await check(
            {"field": "has_activation_gibbs_free_energy", "value": False}, {mapped_ids[1]}, 1
        )
        await check({"field": "label", "value": f"export-siblings:{suffix}:1"}, {mapped_ids[1]}, 1)
    finally:
        async with session_factory() as session:
            await session.execute(delete(LogicalReaction).where(LogicalReaction.id == logical_id))
            await session.commit()
        if source_sample is not None:
            with Session(engine) as source_session:
                _delete_domain_sample(source_session, source_sample)
        engine.dispose()
