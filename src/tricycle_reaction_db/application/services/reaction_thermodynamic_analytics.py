"""Visibility-scoped statistics and export for materialized reaction paths."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlmodel import col

from tricycle_reaction_db.application.dtos import (
    MappedReactionThermodynamicStatistics,
    ThermodynamicDistributionBin,
    ThermodynamicDistributionCategory,
    ThermodynamicScatterPoint,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    format_composite_level_of_theory,
)
from tricycle_reaction_db.application.services.queries import (
    _enforce_candidate_limit,
    logical_reaction_filter_expression_predicate,
)
from tricycle_reaction_db.application.services.query_visibility import (
    logical_reaction_id_is_visible,
    mapped_reaction_id_is_visible,
    query_visibility_scope,
)
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    MappedReaction,
    MappedReactionThermodynamicProfile,
)
from tricycle_reaction_db.db.session import session_factory

HISTOGRAM_BIN_COUNT = 12
MAX_SCATTER_POINTS = 1_000

_PROFILE_COLUMNS = (
    "mapped_reaction_id",
    "logical_reaction_id",
    "mapped_reaction_key",
    "mapped_reaction_kind",
    "mapped_reaction_smiles",
    "mapping_hash",
    "policy_version",
    "electronic_level",
    "thermochemistry_level",
    "level_of_theory",
    "temperature_kelvin",
    "pressure_atm",
    "reactants_running_time_seconds",
    "transition_state_running_time_seconds",
    "products_running_time_seconds",
    "total_running_time_seconds",
    "activation_enthalpy_kcal_mol",
    "activation_gibbs_free_energy_kcal_mol",
    "reaction_enthalpy_kcal_mol",
    "reaction_gibbs_free_energy_kcal_mol",
)


async def _profile_predicate(
    session: Any,
    scope: Any,
    *,
    filter_expression: str | None = None,
    has_activation_gibbs_free_energy: bool | None = None,
    has_reaction_gibbs_free_energy: bool | None = None,
) -> Any:
    mapped_visibility = mapped_reaction_id_is_visible(scope, col(MappedReaction.id))
    if (
        filter_expression is None
        and not has_activation_gibbs_free_energy
        and not has_reaction_gibbs_free_energy
    ):
        return mapped_visibility

    logical_predicates: list[Any] = [logical_reaction_id_is_visible(scope, col(LogicalReaction.id))]
    structure_predicates: list[Any] = []
    if filter_expression is not None:
        logical_predicates.append(
            logical_reaction_filter_expression_predicate(
                filter_expression,
                scope,
                structure_predicates,
            )
        )
    for structure_predicate in structure_predicates:
        await _enforce_candidate_limit(
            select(col(MappedReaction.id)).where(
                mapped_visibility,
                structure_predicate,
            ),
            label="reaction structure query",
            session=session,
        )

    if has_activation_gibbs_free_energy or has_reaction_gibbs_free_energy:
        matching_mapped_ids = select(col(MappedReaction.logical_reaction_id)).where(
            mapped_visibility
        )
        if has_activation_gibbs_free_energy:
            matching_mapped_ids = matching_mapped_ids.where(
                or_(
                    col(MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol).is_not(None),
                    col(MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol).is_not(None),
                )
            )
        if has_reaction_gibbs_free_energy:
            matching_mapped_ids = matching_mapped_ids.where(
                or_(
                    col(MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol).is_not(None),
                    col(MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol).is_not(None),
                )
            )
        logical_predicates.append(col(LogicalReaction.id).in_(matching_mapped_ids))

    matching_logical_ids = select(col(LogicalReaction.id)).where(*logical_predicates)
    return and_(
        mapped_visibility,
        col(MappedReaction.logical_reaction_id).in_(matching_logical_ids),
    )


def _level_label(electronic_level: list[Any], thermochemistry_level: list[Any]) -> str:
    return format_composite_level_of_theory(electronic_level, thermochemistry_level)


async def _histogram(
    session: Any,
    *,
    column: Any,
    minimum: float | None,
    maximum: float | None,
    predicate: Any,
) -> list[ThermodynamicDistributionBin]:
    if minimum is None or maximum is None:
        return []
    if minimum == maximum:
        count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(MappedReactionThermodynamicProfile)
                    .join(
                        MappedReaction,
                        col(MappedReaction.id)
                        == col(MappedReactionThermodynamicProfile.mapped_reaction_id),
                    )
                    .where(predicate, column == minimum)
                )
            ).scalar_one()
        )
        return [
            ThermodynamicDistributionBin(lower=float(minimum), upper=float(maximum), count=count)
        ]

    width = (maximum - minimum) / HISTOGRAM_BIN_COUNT
    bucket = func.width_bucket(column, minimum, maximum, HISTOGRAM_BIN_COUNT)
    rows = (
        await session.execute(
            select(bucket, func.count())
            .select_from(MappedReactionThermodynamicProfile)
            .join(
                MappedReaction,
                col(MappedReaction.id)
                == col(MappedReactionThermodynamicProfile.mapped_reaction_id),
            )
            .where(predicate, column.is_not(None))
            .group_by(bucket)
        )
    ).all()
    counts = [0] * HISTOGRAM_BIN_COUNT
    for raw_bucket, raw_count in rows:
        index = min(max(int(raw_bucket) - 1, 0), HISTOGRAM_BIN_COUNT - 1)
        counts[index] += int(raw_count)
    return [
        ThermodynamicDistributionBin(
            lower=round(float(minimum + width * index), 6),
            upper=round(
                float(
                    maximum if index == HISTOGRAM_BIN_COUNT - 1 else minimum + width * (index + 1)
                ),
                6,
            ),
            count=count,
        )
        for index, count in enumerate(counts)
    ]


class ReactionThermodynamicAnalyticsService:
    """Build read-only aggregates without bypassing artifact-rooted visibility."""

    @staticmethod
    async def statistics(
        project_id: UUID | None = None,
        *,
        filter_expression: str | None = None,
        has_activation_gibbs_free_energy: bool | None = None,
        has_reaction_gibbs_free_energy: bool | None = None,
    ) -> MappedReactionThermodynamicStatistics:
        scope = await query_visibility_scope(project_id=project_id)
        profile = MappedReactionThermodynamicProfile
        async with session_factory() as session:
            predicate = await _profile_predicate(
                session,
                scope,
                filter_expression=filter_expression,
                has_activation_gibbs_free_energy=has_activation_gibbs_free_energy,
                has_reaction_gibbs_free_energy=has_reaction_gibbs_free_energy,
            )
            mapped_count = int(
                (
                    await session.execute(
                        select(func.count()).select_from(MappedReaction).where(predicate)
                    )
                ).scalar_one()
            )
            aggregate = (
                await session.execute(
                    select(
                        func.count(col(profile.id)),
                        func.count(col(profile.activation_gibbs_free_energy_kcal_mol)),
                        func.count(col(profile.reaction_gibbs_free_energy_kcal_mol)),
                        func.count(col(profile.activation_gibbs_free_energy_kcal_mol)).filter(
                            col(profile.reaction_gibbs_free_energy_kcal_mol).is_not(None)
                        ),
                    )
                    .select_from(profile)
                    .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
                    .where(predicate)
                )
            ).one()
            profile_count = int(aggregate[0])
            activation_count = int(aggregate[1])
            reaction_count = int(aggregate[2])
            complete_count = int(aggregate[3])
            bounds = (
                await session.execute(
                    select(
                        func.min(col(profile.activation_gibbs_free_energy_kcal_mol)),
                        func.max(col(profile.activation_gibbs_free_energy_kcal_mol)),
                        func.min(col(profile.reaction_gibbs_free_energy_kcal_mol)),
                        func.max(col(profile.reaction_gibbs_free_energy_kcal_mol)),
                    )
                    .select_from(profile)
                    .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
                    .where(predicate)
                )
            ).one()
            activation_histogram = await _histogram(
                session,
                column=col(profile.activation_gibbs_free_energy_kcal_mol),
                minimum=float(bounds[0]) if bounds[0] is not None else None,
                maximum=float(bounds[1]) if bounds[1] is not None else None,
                predicate=predicate,
            )
            reaction_histogram = await _histogram(
                session,
                column=col(profile.reaction_gibbs_free_energy_kcal_mol),
                minimum=float(bounds[2]) if bounds[2] is not None else None,
                maximum=float(bounds[3]) if bounds[3] is not None else None,
                predicate=predicate,
            )
            level_rows = (
                await session.execute(
                    select(
                        col(profile.electronic_level),
                        col(profile.thermochemistry_level),
                        func.count(),
                    )
                    .select_from(profile)
                    .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
                    .where(predicate)
                    .group_by(col(profile.electronic_level), col(profile.thermochemistry_level))
                )
            ).all()
            temperature_rows = (
                await session.execute(
                    select(col(profile.temperature_kelvin), func.count())
                    .select_from(profile)
                    .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
                    .where(predicate)
                    .group_by(col(profile.temperature_kelvin))
                    .order_by(col(profile.temperature_kelvin))
                )
            ).all()
            scatter_rows = (
                await session.execute(
                    select(
                        col(MappedReaction.id),
                        col(MappedReaction.mapped_reaction_smiles),
                        col(profile.activation_gibbs_free_energy_kcal_mol),
                        col(profile.reaction_gibbs_free_energy_kcal_mol),
                    )
                    .select_from(profile)
                    .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
                    .where(
                        predicate,
                        col(profile.activation_gibbs_free_energy_kcal_mol).is_not(None),
                        col(profile.reaction_gibbs_free_energy_kcal_mol).is_not(None),
                    )
                    .order_by(col(MappedReaction.id), col(profile.id))
                    .limit(MAX_SCATTER_POINTS)
                )
            ).all()

        return MappedReactionThermodynamicStatistics(
            mapped_reaction_count=mapped_count,
            profile_count=profile_count,
            activation_profile_count=activation_count,
            reaction_profile_count=reaction_count,
            complete_profile_count=complete_count,
            activation_gibbs_free_energy_kcal_mol=activation_histogram,
            reaction_gibbs_free_energy_kcal_mol=reaction_histogram,
            level_of_theory=sorted(
                [
                    ThermodynamicDistributionCategory(
                        label=_level_label(list(electronic), list(thermochemistry)),
                        count=int(count),
                    )
                    for electronic, thermochemistry, count in level_rows
                ],
                key=lambda item: (-item.count, item.label),
            ),
            temperature_kelvin=[
                ThermodynamicDistributionCategory(
                    label=f"{float(temperature):g} K", count=int(count)
                )
                for temperature, count in temperature_rows
            ],
            scatter=[
                ThermodynamicScatterPoint(
                    mapped_reaction_id=mapped_id,
                    mapped_reaction_smiles=smiles,
                    activation_gibbs_free_energy_kcal_mol=float(activation),
                    reaction_gibbs_free_energy_kcal_mol=float(reaction),
                )
                for mapped_id, smiles, activation, reaction in scatter_rows
            ],
        )

    @staticmethod
    async def export_csv(
        project_id: UUID | None = None,
        *,
        filter_expression: str | None = None,
        has_activation_gibbs_free_energy: bool | None = None,
        has_reaction_gibbs_free_energy: bool | None = None,
    ) -> AsyncIterator[str]:
        """Capture request visibility before response body streaming begins."""

        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            predicate = await _profile_predicate(
                session,
                scope,
                filter_expression=filter_expression,
                has_activation_gibbs_free_energy=has_activation_gibbs_free_energy,
                has_reaction_gibbs_free_energy=has_reaction_gibbs_free_energy,
            )
        return ReactionThermodynamicAnalyticsService._export_csv_rows(predicate)

    @staticmethod
    async def _export_csv_rows(predicate: Any) -> AsyncIterator[str]:
        """Stream one CSV row for every visible materialized profile."""

        profile = MappedReactionThermodynamicProfile
        statement = (
            select(
                col(MappedReaction.id),
                col(MappedReaction.logical_reaction_id),
                col(MappedReaction.mapped_reaction_key),
                col(MappedReaction.mapped_reaction_kind),
                col(MappedReaction.mapped_reaction_smiles),
                col(MappedReaction.mapping_hash),
                col(profile.policy_version),
                col(profile.electronic_level),
                col(profile.thermochemistry_level),
                col(profile.temperature_kelvin),
                col(profile.pressure_atm),
                col(profile.reactants_running_time_seconds),
                col(profile.transition_state_running_time_seconds),
                col(profile.products_running_time_seconds),
                col(profile.total_running_time_seconds),
                col(profile.activation_enthalpy_kcal_mol),
                col(profile.activation_gibbs_free_energy_kcal_mol),
                col(profile.reaction_enthalpy_kcal_mol),
                col(profile.reaction_gibbs_free_energy_kcal_mol),
            )
            .select_from(profile)
            .join(MappedReaction, col(MappedReaction.id) == col(profile.mapped_reaction_id))
            .where(predicate)
            .order_by(col(MappedReaction.id), col(profile.id))
        )

        def encode(values: list[Any]) -> str:
            buffer = io.StringIO(newline="")
            csv.writer(buffer, lineterminator="\n").writerow(values)
            return buffer.getvalue()

        yield encode(list(_PROFILE_COLUMNS))
        async with session_factory() as session:
            result = await session.stream(statement)
            async for row in result:
                # Keep the encoder tolerant of small test doubles and older
                # callers that supplied the pre-runtime 15-column row shape.
                values = tuple(row)
                if len(values) == 15:
                    (
                        mapped_id,
                        logical_id,
                        mapped_key,
                        mapped_kind,
                        mapped_smiles,
                        mapping_hash,
                        policy_version,
                        electronic_level,
                        thermochemistry_level,
                        temperature,
                        pressure,
                        activation_enthalpy,
                        activation_gibbs,
                        reaction_enthalpy,
                        reaction_gibbs,
                    ) = values
                    reactants_runtime = transition_state_runtime = None
                    products_runtime = total_runtime = None
                else:
                    (
                        mapped_id,
                        logical_id,
                        mapped_key,
                        mapped_kind,
                        mapped_smiles,
                        mapping_hash,
                        policy_version,
                        electronic_level,
                        thermochemistry_level,
                        temperature,
                        pressure,
                        reactants_runtime,
                        transition_state_runtime,
                        products_runtime,
                        total_runtime,
                        activation_enthalpy,
                        activation_gibbs,
                        reaction_enthalpy,
                        reaction_gibbs,
                    ) = values
                yield encode(
                    [
                        mapped_id,
                        logical_id,
                        mapped_key,
                        getattr(mapped_kind, "value", mapped_kind),
                        mapped_smiles,
                        mapping_hash,
                        policy_version,
                        json.dumps(electronic_level, ensure_ascii=True, separators=(",", ":")),
                        json.dumps(thermochemistry_level, ensure_ascii=True, separators=(",", ":")),
                        _level_label(list(electronic_level), list(thermochemistry_level)),
                        temperature,
                        pressure,
                        reactants_runtime,
                        transition_state_runtime,
                        products_runtime,
                        total_runtime,
                        activation_enthalpy,
                        activation_gibbs,
                        reaction_enthalpy,
                        reaction_gibbs,
                    ]
                )


__all__ = ["ReactionThermodynamicAnalyticsService"]
