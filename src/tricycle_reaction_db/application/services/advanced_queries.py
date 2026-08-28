"""Read-only projections for advanced frame calculation results."""

import json
from typing import Any, cast
from uuid import UUID

from nexusx import UseCaseService, query  # type: ignore[import-untyped]
from sqlalchemy import exists, func, or_, select
from sqlmodel import col

from tricycle_reaction_db.application.dtos import (
    AtomicPopulationSeriesView,
    BondOrderResultView,
    CalculationResultDetail,
    CalculationResultPage,
    CalculationResultSummary,
    ChargeSpinPopulationResultView,
    ElectronicConfigurationView,
    ElectronicStateSetView,
    ElectronicStateView,
    ImplicitSolvationResultView,
    MolecularOrbitalResultView,
    MultireferenceResultView,
    NMRResultView,
    NMRShieldingTensorView,
    PageInfo,
    PolarizabilityResultView,
    ScientificArraySummary,
    SinglePointPropertyResultView,
    TotalSpinResultView,
)
from tricycle_reaction_db.application.services.queries import (
    PageLimit,
    PageOffset,
    _array_assignment_owner,
    _enum_value,
    _frame_select,
    _frame_summary,
    _required_uuid,
)
from tricycle_reaction_db.application.services.query_visibility import (
    calculation_frame_is_visible,
    frame_id_is_visible,
    query_visibility_scope,
)
from tricycle_reaction_db.db.models import (
    AtomicPopulationSeries,
    BondOrderResult,
    CalculationFrame,
    ChargeSpinPopulationResult,
    ElectronicConfiguration,
    ElectronicState,
    ElectronicStateSet,
    Geometry,
    ImplicitSolvationResult,
    MolecularOrbitalResult,
    MultireferenceResult,
    NMRResult,
    NMRShieldingTensor,
    ParseRevision,
    PolarizabilityResult,
    ScientificArray,
    ScientificArrayAssignment,
    SinglePointPropertyResult,
    TotalSpinResult,
)
from tricycle_reaction_db.db.session import session_factory

_RESULT_MODELS: tuple[tuple[str, type[Any]], ...] = (
    ("molecular_orbitals", MolecularOrbitalResult),
    ("charge_spin_populations", ChargeSpinPopulationResult),
    ("polarizability", PolarizabilityResult),
    ("nmr", NMRResult),
    ("bond_orders", BondOrderResult),
    ("total_spin", TotalSpinResult),
    ("single_point_properties", SinglePointPropertyResult),
    ("electronic_state_sets", ElectronicStateSet),
    ("multireference", MultireferenceResult),
    ("implicit_solvation", ImplicitSolvationResult),
)
ADVANCED_RESULT_KINDS = frozenset(kind for kind, _ in _RESULT_MODELS)


def _result_exists(model: type[Any]) -> Any:
    return exists().where(col(model.frame_id) == col(CalculationFrame.id))


def _result_flag_columns() -> list[Any]:
    return [_result_exists(model).label(kind) for kind, model in _RESULT_MODELS]


def _result_kinds(flags: tuple[bool, ...]) -> list[str]:
    return [kind for (kind, _), present in zip(_RESULT_MODELS, flags, strict=True) if present]


def _array_view(
    array: ScientificArray,
    assignment: ScientificArrayAssignment | None,
) -> ScientificArraySummary:
    owner_kind, owner_id = _array_assignment_owner(assignment)
    return ScientificArraySummary(
        id=_required_uuid(array.id, "ScientificArray"),
        kind=_enum_value(array.kind),
        ordinal=array.ordinal,
        unit=array.unit,
        dtype=array.dtype,
        shape=list(array.shape),
        array_nbytes=array.array_nbytes,
        payload_sha256=array.payload_sha256,
        owner_kind=owner_kind,
        owner_id=owner_id,
        slot=assignment.slot if assignment is not None else None,
        slot_ordinal=assignment.slot_ordinal if assignment is not None else None,
    )


class CalculationResultQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect normalized advanced result containers owned by calculation frames."""

    @query  # type: ignore[untyped-decorator]
    async def list_calculation_results(
        cls,
        frame_id: UUID | None = None,
        artifact_file_id: UUID | None = None,
        geometry_id: UUID | None = None,
        topology_id: UUID | None = None,
        result_kind: str | None = None,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> CalculationResultPage:
        """List frames that own at least one advanced result container."""

        scope = await query_visibility_scope()
        predicates: list[Any] = [
            calculation_frame_is_visible(scope, col(CalculationFrame.parse_revision_id))
        ]
        if frame_id is not None:
            predicates.append(col(CalculationFrame.id) == frame_id)
        if artifact_file_id is not None:
            revision_ids = select(col(ParseRevision.id)).where(
                col(ParseRevision.artifact_file_id) == artifact_file_id
            )
            predicates.append(col(CalculationFrame.parse_revision_id).in_(revision_ids))
        if geometry_id is not None:
            predicates.append(col(CalculationFrame.geometry_id) == geometry_id)
        if topology_id is not None:
            geometry_ids = select(col(Geometry.id)).where(col(Geometry.topology_id) == topology_id)
            predicates.append(col(CalculationFrame.geometry_id).in_(geometry_ids))

        result_predicates = [_result_exists(model) for _, model in _RESULT_MODELS]
        if result_kind is not None:
            if result_kind not in ADVANCED_RESULT_KINDS:
                choices = ", ".join(sorted(ADVANCED_RESULT_KINDS))
                raise ValueError(f"unsupported result_kind; expected one of: {choices}")
            selected_model = dict(_RESULT_MODELS)[result_kind]
            predicates.append(_result_exists(selected_model))
        else:
            predicates.append(or_(*result_predicates))

        count_statement = select(func.count()).select_from(CalculationFrame).where(*predicates)
        statement = (
            _frame_select()
            .add_columns(*_result_flag_columns())
            .where(*predicates)
            .order_by(col(CalculationFrame.created_at), col(CalculationFrame.id))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        flag_count = len(_RESULT_MODELS)
        return CalculationResultPage(
            items=[
                CalculationResultSummary(
                    frame=_frame_summary(*row[:-flag_count]),
                    result_kinds=_result_kinds(cast(tuple[bool, ...], row[-flag_count:])),
                )
                for row in rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_calculation_results(
        cls,
        frame_id: UUID,
    ) -> CalculationResultDetail | None:
        """Return all normalized advanced results and array metadata for one frame."""

        scope = await query_visibility_scope()
        async with session_factory() as session:
            frame_row = (
                await session.execute(
                    _frame_select().where(
                        col(CalculationFrame.id) == frame_id,
                        frame_id_is_visible(scope, col(CalculationFrame.id)),
                    )
                )
            ).first()
            if frame_row is None:
                return None

            molecular_orbitals = (
                await session.execute(
                    select(MolecularOrbitalResult).where(
                        col(MolecularOrbitalResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            populations = (
                await session.execute(
                    select(ChargeSpinPopulationResult).where(
                        col(ChargeSpinPopulationResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            population_series = (
                (
                    await session.execute(
                        select(AtomicPopulationSeries)
                        .where(
                            col(AtomicPopulationSeries.result_id)
                            == _required_uuid(populations.id, "ChargeSpinPopulationResult")
                        )
                        .order_by(col(AtomicPopulationSeries.series_key))
                    )
                )
                .scalars()
                .all()
                if populations is not None
                else []
            )
            polarizability = (
                await session.execute(
                    select(PolarizabilityResult).where(
                        col(PolarizabilityResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            nmr = (
                await session.execute(select(NMRResult).where(col(NMRResult.frame_id) == frame_id))
            ).scalar_one_or_none()
            shielding_tensors = (
                (
                    await session.execute(
                        select(NMRShieldingTensor)
                        .where(
                            col(NMRShieldingTensor.result_id) == _required_uuid(nmr.id, "NMRResult")
                        )
                        .order_by(col(NMRShieldingTensor.atom_index))
                    )
                )
                .scalars()
                .all()
                if nmr is not None
                else []
            )
            bond_orders = (
                await session.execute(
                    select(BondOrderResult).where(col(BondOrderResult.frame_id) == frame_id)
                )
            ).scalar_one_or_none()
            total_spin = (
                await session.execute(
                    select(TotalSpinResult).where(col(TotalSpinResult.frame_id) == frame_id)
                )
            ).scalar_one_or_none()
            single_point = (
                await session.execute(
                    select(SinglePointPropertyResult).where(
                        col(SinglePointPropertyResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            state_sets = (
                (
                    await session.execute(
                        select(ElectronicStateSet)
                        .where(col(ElectronicStateSet.frame_id) == frame_id)
                        .order_by(col(ElectronicStateSet.kind))
                    )
                )
                .scalars()
                .all()
            )
            state_set_ids = [
                _required_uuid(state_set.id, "ElectronicStateSet") for state_set in state_sets
            ]
            states = (
                (
                    await session.execute(
                        select(ElectronicState)
                        .where(col(ElectronicState.state_set_id).in_(state_set_ids))
                        .order_by(
                            col(ElectronicState.state_set_id),
                            col(ElectronicState.state_ordinal),
                        )
                    )
                )
                .scalars()
                .all()
                if state_set_ids
                else []
            )
            state_ids = [_required_uuid(state.id, "ElectronicState") for state in states]
            configurations = (
                (
                    await session.execute(
                        select(ElectronicConfiguration)
                        .where(col(ElectronicConfiguration.electronic_state_id).in_(state_ids))
                        .order_by(
                            col(ElectronicConfiguration.electronic_state_id),
                            col(ElectronicConfiguration.configuration_ordinal),
                        )
                    )
                )
                .scalars()
                .all()
                if state_ids
                else []
            )
            multireference = (
                await session.execute(
                    select(MultireferenceResult).where(
                        col(MultireferenceResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            implicit_solvation = (
                await session.execute(
                    select(ImplicitSolvationResult).where(
                        col(ImplicitSolvationResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            array_rows = (
                await session.execute(
                    select(ScientificArray, ScientificArrayAssignment)
                    .outerjoin(
                        ScientificArrayAssignment,
                        col(ScientificArrayAssignment.scientific_array_id)
                        == col(ScientificArray.id),
                    )
                    .where(col(ScientificArray.frame_id) == frame_id)
                    .order_by(col(ScientificArray.kind), col(ScientificArray.ordinal))
                )
            ).all()

        arrays_by_owner: dict[tuple[str, UUID], list[ScientificArraySummary]] = {}
        for array, assignment in array_rows:
            owner_kind, owner_id = _array_assignment_owner(assignment)
            if owner_kind is not None and owner_id is not None:
                arrays_by_owner.setdefault((owner_kind, owner_id), []).append(
                    _array_view(array, assignment)
                )

        def owner_arrays(owner_kind: str, owner_id: UUID | None) -> list[ScientificArraySummary]:
            return arrays_by_owner.get((owner_kind, owner_id), []) if owner_id is not None else []

        series_by_result: dict[UUID, list[AtomicPopulationSeries]] = {}
        for series in population_series:
            series_by_result.setdefault(series.result_id, []).append(series)
        states_by_set: dict[UUID, list[ElectronicState]] = {}
        for state in states:
            states_by_set.setdefault(state.state_set_id, []).append(state)
        configurations_by_state: dict[UUID, list[ElectronicConfiguration]] = {}
        for configuration in configurations:
            configurations_by_state.setdefault(configuration.electronic_state_id, []).append(
                configuration
            )

        result_kinds = [
            kind
            for kind, present in (
                ("molecular_orbitals", molecular_orbitals is not None),
                ("charge_spin_populations", populations is not None),
                ("polarizability", polarizability is not None),
                ("nmr", nmr is not None),
                ("bond_orders", bond_orders is not None),
                ("total_spin", total_spin is not None),
                ("single_point_properties", single_point is not None),
                ("electronic_state_sets", bool(state_sets)),
                ("multireference", multireference is not None),
                ("implicit_solvation", implicit_solvation is not None),
            )
            if present
        ]
        frame_summary = _frame_summary(*frame_row)
        return CalculationResultDetail(
            frame=frame_summary,
            result_kinds=result_kinds,
            molecular_orbitals=(
                MolecularOrbitalResultView(
                    id=_required_uuid(molecular_orbitals.id, "MolecularOrbitalResult"),
                    electronic_state=molecular_orbitals.electronic_state,
                    alpha_orbital_count=molecular_orbitals.alpha_orbital_count,
                    beta_orbital_count=molecular_orbitals.beta_orbital_count,
                    coefficient_count=molecular_orbitals.coefficient_count,
                    alpha_occupancies=list(molecular_orbitals.alpha_occupancies),
                    beta_occupancies=list(molecular_orbitals.beta_occupancies),
                    alpha_symmetries=list(molecular_orbitals.alpha_symmetries),
                    beta_symmetries=list(molecular_orbitals.beta_symmetries),
                    source_schema_version=molecular_orbitals.source_schema_version,
                    scientific_arrays=owner_arrays(
                        "molecular_orbital_result", molecular_orbitals.id
                    ),
                )
                if molecular_orbitals is not None
                else None
            ),
            charge_spin_populations=(
                ChargeSpinPopulationResultView(
                    id=_required_uuid(populations.id, "ChargeSpinPopulationResult"),
                    series_count=populations.series_count,
                    source_schema_version=populations.source_schema_version,
                    series=[
                        AtomicPopulationSeriesView(
                            id=_required_uuid(series.id, "AtomicPopulationSeries"),
                            series_key=series.series_key,
                            scheme=series.scheme,
                            quantity=series.quantity,
                            value_count=series.value_count,
                            spin_channel=series.spin_channel,
                            source_label=series.source_label,
                            series_metadata_json=json.dumps(
                                series.series_metadata,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            scientific_arrays=owner_arrays("atomic_population_series", series.id),
                        )
                        for series in series_by_result.get(
                            _required_uuid(populations.id, "ChargeSpinPopulationResult"), []
                        )
                    ],
                )
                if populations is not None
                else None
            ),
            polarizability=(
                PolarizabilityResultView(
                    id=_required_uuid(polarizability.id, "PolarizabilityResult"),
                    electronic_spatial_extent_bohr2=(
                        polarizability.electronic_spatial_extent_bohr2
                    ),
                    isotropic_polarizability_bohr3=(polarizability.isotropic_polarizability_bohr3),
                    anisotropic_polarizability_bohr3=(
                        polarizability.anisotropic_polarizability_bohr3
                    ),
                    source_schema_version=polarizability.source_schema_version,
                    scientific_arrays=owner_arrays("polarizability_result", polarizability.id),
                )
                if polarizability is not None
                else None
            ),
            nmr=(
                NMRResultView(
                    id=_required_uuid(nmr.id, "NMRResult"),
                    gauge=nmr.gauge,
                    shielding_count=nmr.shielding_count,
                    coupling_atom_indices=list(nmr.coupling_atom_indices),
                    source_schema_version=nmr.source_schema_version,
                    shielding_tensors=[
                        NMRShieldingTensorView(
                            id=_required_uuid(tensor.id, "NMRShieldingTensor"),
                            atom_index=tensor.atom_index,
                            atom_symbol=tensor.atom_symbol,
                            isotropic_ppm=tensor.isotropic_ppm,
                            anisotropy_ppm=tensor.anisotropy_ppm,
                            anisotropy_convention=tensor.anisotropy_convention,
                            orientation=tensor.orientation,
                            scientific_arrays=owner_arrays("nmr_shielding_tensor", tensor.id),
                        )
                        for tensor in shielding_tensors
                    ],
                    scientific_arrays=owner_arrays("nmr_result", nmr.id),
                )
                if nmr is not None
                else None
            ),
            bond_orders=(
                BondOrderResultView(
                    id=_required_uuid(bond_orders.id, "BondOrderResult"),
                    matrix_count=bond_orders.matrix_count,
                    source_schema_version=bond_orders.source_schema_version,
                    scientific_arrays=owner_arrays("bond_order_result", bond_orders.id),
                )
                if bond_orders is not None
                else None
            ),
            total_spin=(
                TotalSpinResultView(
                    id=_required_uuid(total_spin.id, "TotalSpinResult"),
                    spin_square=total_spin.spin_square,
                    spin_quantum_number=total_spin.spin_quantum_number,
                    source_schema_version=total_spin.source_schema_version,
                )
                if total_spin is not None
                else None
            ),
            single_point_properties=(
                SinglePointPropertyResultView(
                    id=_required_uuid(single_point.id, "SinglePointPropertyResult"),
                    vertical_ionization_potential_ev=(
                        single_point.vertical_ionization_potential_ev
                    ),
                    vertical_electron_affinity_ev=(single_point.vertical_electron_affinity_ev),
                    global_electrophilicity_index_ev=(
                        single_point.global_electrophilicity_index_ev
                    ),
                    source_schema_version=single_point.source_schema_version,
                    scientific_arrays=owner_arrays("single_point_property_result", single_point.id),
                )
                if single_point is not None
                else None
            ),
            electronic_state_sets=[
                ElectronicStateSetView(
                    id=state_set_id,
                    kind=_enum_value(state_set.kind),
                    state_count=state_set.state_count,
                    source_schema_version=state_set.source_schema_version,
                    states=[
                        ElectronicStateView(
                            id=state_id,
                            state_ordinal=state.state_ordinal,
                            state_index=state.state_index,
                            root=state.root,
                            label=state.label,
                            multiplicity=state.multiplicity,
                            spin=state.spin,
                            irrep=state.irrep,
                            method=state.method,
                            energy_hartree=state.energy_hartree,
                            excitation_energy_ev=state.excitation_energy_ev,
                            oscillator_strength=state.oscillator_strength,
                            state_properties_json=json.dumps(
                                state.state_properties,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            source=state.source,
                            configurations=[
                                ElectronicConfigurationView(
                                    id=_required_uuid(configuration.id, "ElectronicConfiguration"),
                                    configuration_ordinal=(configuration.configuration_ordinal),
                                    label=configuration.label,
                                    coefficient=configuration.coefficient,
                                    weight=configuration.weight,
                                    occupation=list(configuration.occupation),
                                    orbital_indices=list(configuration.orbital_indices),
                                    raw=configuration.raw,
                                )
                                for configuration in configurations_by_state.get(state_id, [])
                            ],
                            scientific_arrays=owner_arrays("electronic_state", state.id),
                        )
                        for state in states_by_set.get(state_set_id, [])
                        for state_id in [_required_uuid(state.id, "ElectronicState")]
                    ],
                )
                for state_set in state_sets
                for state_set_id in [_required_uuid(state_set.id, "ElectronicStateSet")]
            ],
            multireference=(
                MultireferenceResultView(
                    id=_required_uuid(multireference.id, "MultireferenceResult"),
                    electronic_state_set_id=multireference.electronic_state_set_id,
                    method=multireference.method,
                    reference_method=multireference.reference_method,
                    ci_type=multireference.ci_type,
                    active_space_electrons=multireference.active_space_electrons,
                    active_space_orbitals=multireference.active_space_orbitals,
                    active_space_roots=multireference.active_space_roots,
                    active_orbitals=list(multireference.active_orbitals),
                    inactive_orbitals=list(multireference.inactive_orbitals),
                    frozen_orbitals=list(multireference.frozen_orbitals),
                    active_space_raw=multireference.active_space_raw,
                    active_space_options_json=json.dumps(
                        multireference.active_space_options,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    corrections_json=json.dumps(
                        multireference.corrections,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    diagnostics=list(multireference.diagnostics),
                    result_properties_json=json.dumps(
                        multireference.result_properties,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    source_schema_version=multireference.source_schema_version,
                )
                if multireference is not None
                else None
            ),
            implicit_solvation=(
                ImplicitSolvationResultView(
                    id=_required_uuid(implicit_solvation.id, "ImplicitSolvationResult"),
                    solvent=implicit_solvation.solvent,
                    solvent_model=implicit_solvation.solvent_model,
                    atomic_radii=implicit_solvation.atomic_radii,
                    solvent_epsilon=implicit_solvation.solvent_epsilon,
                    solvent_epsilon_infinite=implicit_solvation.solvent_epsilon_infinite,
                    source_schema_version=implicit_solvation.source_schema_version,
                )
                if implicit_solvation is not None
                else None
            ),
        )


__all__ = ["ADVANCED_RESULT_KINDS", "CalculationResultQueryService"]
