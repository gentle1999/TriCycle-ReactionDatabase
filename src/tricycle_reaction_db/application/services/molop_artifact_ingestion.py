"""Reusable full-frame persistence for one MolOP-parsed calculation artifact."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlmodel import Session

from tricycle_reaction_db.application.dtos import ParseRevisionCompletionRecord
from tricycle_reaction_db.application.services.calculations import (
    finalize_parse_revision,
    persist_atomic_population_series,
    persist_bond_order_result,
    persist_calculation_frame,
    persist_calculation_segment,
    persist_calculation_status_result,
    persist_charge_spin_population_result,
    persist_electronic_configuration,
    persist_electronic_state,
    persist_electronic_state_set,
    persist_energy_observation,
    persist_frame_energy_result,
    persist_geometry_optimization_result,
    persist_implicit_solvation_result,
    persist_molecular_orbital_result,
    persist_multireference_result,
    persist_nmr_result,
    persist_nmr_shielding_tensor,
    persist_parse_revision,
    persist_polarizability_result,
    persist_scientific_array,
    persist_scientific_array_assignment,
    persist_single_point_property_result,
    persist_thermochemistry_result,
    persist_total_spin_result,
    persist_vibration_result,
)
from tricycle_reaction_db.application.services.catalog import persist_calculation_protocol
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
    persist_molecular_geometry,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    reconcile_geometry_with_reactions,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    ElectronicState,
    ParseRevision,
)
from tricycle_reaction_db.domain.enums import (
    ElectronicStateSetKind,
    GeometryAssignmentKind,
    ScientificArrayOwnerKind,
)
from tricycle_reaction_db.ingestion import (
    MolOPFrameRecords,
    frame_records_from_molop,
    parse_revision_record_from_molop,
    protocol_record_from_molop_segment,
    segment_record_from_molop,
)


@dataclass(frozen=True, slots=True)
class PersistedMolOPArtifact:
    parse_revision: ParseRevision
    frames_by_file_index: dict[int, CalculationFrame]
    frame_count: int
    array_counts: dict[str, int]


def _json_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()


def _revision_record_hash(
    artifact: ArtifactFile,
    chem_file: Any,
    records: list[MolOPFrameRecords],
) -> str:
    return _json_sha256(
        {
            "artifact_sha256": artifact.content_sha256,
            "export_schema_version": chem_file.schema_version,
            "segments": [
                segment.model_dump(mode="json", exclude_none=True)
                for segment in chem_file.source_segments
            ],
            "frames": [
                {
                    "frame": record.frame.model_dump(
                        mode="json",
                        exclude={"observed_coordinates"},
                    ),
                    "geometry_hash": record.molecule.geometry.geometry_hash,
                    "energy": record.energy.model_dump(mode="json") if record.energy else None,
                    "energy_observations": [
                        item.model_dump(mode="json") for item in record.energy_observations
                    ],
                    "optimization": (
                        record.optimization.model_dump(mode="json") if record.optimization else None
                    ),
                    "vibration": (
                        record.vibration.model_dump(mode="json") if record.vibration else None
                    ),
                    "thermochemistry": (
                        record.thermochemistry.model_dump(mode="json")
                        if record.thermochemistry
                        else None
                    ),
                    "status": record.status.model_dump(mode="json") if record.status else None,
                    "molecular_orbitals": (
                        record.molecular_orbitals.model_dump(mode="json")
                        if record.molecular_orbitals
                        else None
                    ),
                    "charge_spin_populations": (
                        record.charge_spin_populations.model_dump(mode="json")
                        if record.charge_spin_populations
                        else None
                    ),
                    "atomic_population_series": [
                        item.model_dump(mode="json") for item in record.atomic_population_series
                    ],
                    "polarizability": (
                        record.polarizability.model_dump(mode="json")
                        if record.polarizability
                        else None
                    ),
                    "nmr": record.nmr.model_dump(mode="json") if record.nmr else None,
                    "nmr_shielding_tensors": [
                        item.model_dump(mode="json") for item in record.nmr_shielding_tensors
                    ],
                    "bond_orders": (
                        record.bond_orders.model_dump(mode="json") if record.bond_orders else None
                    ),
                    "total_spin": (
                        record.total_spin.model_dump(mode="json") if record.total_spin else None
                    ),
                    "single_point_properties": (
                        record.single_point_properties.model_dump(mode="json")
                        if record.single_point_properties
                        else None
                    ),
                    "electronic_state_sets": [
                        item.model_dump(mode="json") for item in record.electronic_state_sets
                    ],
                    "electronic_states": [
                        item.model_dump(mode="json") for item in record.electronic_states
                    ],
                    "electronic_configurations": [
                        item.model_dump(mode="json") for item in record.electronic_configurations
                    ],
                    "multireference": (
                        record.multireference.model_dump(mode="json")
                        if record.multireference
                        else None
                    ),
                    "implicit_solvation": (
                        record.implicit_solvation.model_dump(mode="json")
                        if record.implicit_solvation
                        else None
                    ),
                    "arrays": [
                        item.model_dump(mode="json", exclude={"data"}) for item in record.arrays
                    ],
                    "array_assignments": [
                        item.model_dump(mode="json") for item in record.array_assignments
                    ],
                }
                for record in records
            ],
        }
    )


def _persist_extended_results(
    session: Session,
    frame: CalculationFrame,
    record: MolOPFrameRecords,
    array_counts: dict[str, int],
) -> None:
    owners: dict[tuple[ScientificArrayOwnerKind, str | None], Any] = {}
    if record.molecular_orbitals is not None:
        owners[(ScientificArrayOwnerKind.MOLECULAR_ORBITAL_RESULT, None)] = (
            persist_molecular_orbital_result(session, frame, record.molecular_orbitals)
        )
    if record.charge_spin_populations is not None:
        population = persist_charge_spin_population_result(
            session, frame, record.charge_spin_populations
        )
        for series_record in record.atomic_population_series:
            series = persist_atomic_population_series(session, population, series_record)
            owners[
                (ScientificArrayOwnerKind.ATOMIC_POPULATION_SERIES, series_record.series_key)
            ] = series
    if record.polarizability is not None:
        owners[(ScientificArrayOwnerKind.POLARIZABILITY_RESULT, None)] = (
            persist_polarizability_result(session, frame, record.polarizability)
        )
    if record.nmr is not None:
        nmr = persist_nmr_result(session, frame, record.nmr)
        owners[(ScientificArrayOwnerKind.NMR_RESULT, None)] = nmr
        for tensor_record in record.nmr_shielding_tensors:
            tensor = persist_nmr_shielding_tensor(session, nmr, tensor_record)
            owners[(ScientificArrayOwnerKind.NMR_SHIELDING_TENSOR, str(tensor.atom_index))] = tensor
    if record.bond_orders is not None:
        owners[(ScientificArrayOwnerKind.BOND_ORDER_RESULT, None)] = persist_bond_order_result(
            session, frame, record.bond_orders
        )
    if record.total_spin is not None:
        persist_total_spin_result(session, frame, record.total_spin)
    if record.single_point_properties is not None:
        owners[(ScientificArrayOwnerKind.SINGLE_POINT_PROPERTY_RESULT, None)] = (
            persist_single_point_property_result(session, frame, record.single_point_properties)
        )

    state_sets = {
        item.kind: persist_electronic_state_set(session, frame, item)
        for item in record.electronic_state_sets
    }
    states: dict[tuple[ElectronicStateSetKind, int], ElectronicState] = {}
    for state_record in record.electronic_states:
        state = persist_electronic_state(session, state_sets[state_record.set_kind], state_record)
        states[(state_record.set_kind, state_record.state_ordinal)] = state
        owners[
            (
                ScientificArrayOwnerKind.ELECTRONIC_STATE,
                f"{state_record.set_kind.value}:{state_record.state_ordinal}",
            )
        ] = state
    for configuration in record.electronic_configurations:
        persist_electronic_configuration(
            session,
            states[(configuration.set_kind, configuration.state_ordinal)],
            configuration,
        )
    if record.multireference is not None:
        persist_multireference_result(
            session,
            frame,
            record.multireference,
            state_sets.get(ElectronicStateSetKind.MULTIREFERENCE),
        )
    if record.implicit_solvation is not None:
        persist_implicit_solvation_result(session, frame, record.implicit_solvation)

    arrays = {}
    for array_record in record.arrays:
        array = persist_scientific_array(session, frame, array_record)
        arrays[(array_record.kind, array_record.ordinal)] = array
        key = array_record.kind.value
        array_counts[key] = array_counts.get(key, 0) + 1
    for assignment in record.array_assignments:
        persist_scientific_array_assignment(
            session,
            arrays[(assignment.array_kind, assignment.array_ordinal)],
            owners[(assignment.owner_kind, assignment.owner_key)],
            assignment,
        )


def persist_molop_calculation_artifact(
    session: Session,
    *,
    artifact: ArtifactFile,
    chem_file: Any,
    started_at: datetime,
    completed_at: datetime,
    records: list[MolOPFrameRecords] | None = None,
    source_compression: str | None = None,
    force_new_revision: bool = False,
    fast_insert: bool = False,
) -> PersistedMolOPArtifact:
    """Persist every parsed segment, frame, graph, coordinate, scalar, and array."""

    if source_compression is None:
        if chem_file.artifact_sha256 != artifact.content_sha256:
            raise ValueError("MolOP source bytes do not match the stored ArtifactFile SHA-256")
        if chem_file.artifact_size_bytes != artifact.size_bytes:
            raise ValueError("MolOP source byte size does not match the stored ArtifactFile")
    frame_records = records or [
        frame_records_from_molop(frame, export_schema_version=chem_file.schema_version)
        for frame in chem_file
    ]
    previous_fast_insert = session.info.get("tricycle_fast_insert", False)
    previous_autoflush = session.autoflush
    session.info["tricycle_fast_insert"] = fast_insert
    if fast_insert:
        session.autoflush = False
    try:
        revision = persist_parse_revision(
            session,
            artifact,
            parse_revision_record_from_molop(
                chem_file,
                started_at=started_at,
                source_compression=source_compression,
            ),
            force_new_revision=force_new_revision,
        )
        frames_by_file_index: dict[int, CalculationFrame] = {}
        array_counts: dict[str, int] = {}
        geometry_context = GeometryPersistenceContext()
        for source_segment in chem_file.source_segments:
            protocol_record = protocol_record_from_molop_segment(source_segment)
            protocol = (
                persist_calculation_protocol(session, protocol_record)
                if protocol_record is not None
                else None
            )
            segment = persist_calculation_segment(
                session,
                revision,
                protocol,
                segment_record_from_molop(source_segment),
            )
            segment_frames = [
                frame for frame in chem_file if frame.segment_index == source_segment.segment_index
            ]
            for source_frame in segment_frames:
                file_frame_index = source_frame.file_frame_index
                if file_frame_index is None or source_frame.segment_frame_index is None:
                    raise ValueError("MolOP frame is missing stable source indices")
                record = frame_records[file_frame_index]
                molecule = persist_molecular_geometry(
                    session,
                    record.molecule,
                    coordinate_decimal_places=record.frame.coordinate_decimal_places,
                    context=geometry_context,
                )
                frame_record = record.frame
                if (
                    molecule.geometry_assignment_kind
                    is GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
                ):
                    frame_record = frame_record.model_copy(
                        update={
                            "geometry_assignment_kind": (
                                GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
                            ),
                            "observed_coordinate_hash": record.molecule.observed_coordinate_hash,
                            "observed_to_geometry_atom_indices": (
                                molecule.observed_to_geometry_atom_indices
                            ),
                            "observed_to_geometry_transform": list(
                                molecule.observed_to_geometry_transform
                            ),
                            "geometry_assignment_rmsd_angstrom": molecule.coordinate_rmsd_angstrom,
                            "geometry_assignment_max_abs_angstrom": (
                                molecule.coordinate_max_abs_angstrom
                            ),
                            "geometry_assignment_policy_version": (
                                "geometry-internal-coordinate-match-v3"
                            ),
                        }
                    )
                frame = persist_calculation_frame(
                    session,
                    segment,
                    molecule.geometry,
                    molecule.topology_derivation,
                    frame_record,
                    reconcile=False,
                )
                frames_by_file_index[file_frame_index] = frame
                if record.energy is not None:
                    energy = persist_frame_energy_result(session, frame, record.energy)
                    for observation in record.energy_observations:
                        persist_energy_observation(session, energy, observation)
                if record.optimization is not None:
                    persist_geometry_optimization_result(session, frame, record.optimization)
                if record.vibration is not None:
                    persist_vibration_result(session, frame, record.vibration)
                if record.status is not None:
                    persist_calculation_status_result(session, frame, record.status)
                _persist_extended_results(session, frame, record, array_counts)
                if record.thermochemistry is not None:
                    persist_thermochemistry_result(
                        session,
                        frame,
                        record.thermochemistry,
                        reconcile=False,
                    )

        # One ordered flush lets SQLAlchemy group revision-local rows into
        # executemany batches before reconciliation queries inspect them.
        session.flush()
        # Reconciliation performs identity lookups while adding several
        # conformers for one reaction component.  Fast insert keeps
        # autoflush disabled for frame-local batches, but these lookups must
        # see bindings created earlier in this same transaction.
        session.autoflush = True
        try:
            for geometry in geometry_context.geometries_to_reconcile.values():
                reconcile_geometry_with_reactions(session, geometry)
        finally:
            session.autoflush = previous_autoflush

        finalize_parse_revision(
            session,
            revision,
            ParseRevisionCompletionRecord(
                record_sha256=_revision_record_hash(artifact, chem_file, frame_records),
                completed_at=completed_at,
            ),
        )
        return PersistedMolOPArtifact(
            parse_revision=revision,
            frames_by_file_index=frames_by_file_index,
            frame_count=len(frame_records),
            array_counts=array_counts,
        )
    finally:
        session.autoflush = previous_autoflush
        session.info["tricycle_fast_insert"] = previous_fast_insert


__all__ = ["PersistedMolOPArtifact", "persist_molop_calculation_artifact"]
