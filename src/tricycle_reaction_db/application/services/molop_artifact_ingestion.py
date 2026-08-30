"""Reusable full-frame persistence for one MolOP-parsed calculation artifact."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext, suppress
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlmodel import Session

from tricycle_reaction_db.application.dtos import (
    CalculationSegmentRecord,
    ParseRevisionCompletionRecord,
)
from tricycle_reaction_db.application.services._persistence import _attach_pending_entities
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
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
    persist_molecular_geometry,
    preload_molecular_geometry_context,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    preload_reconciliation_context,
    reconcilable_geometry_ids,
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
    ParseCompleteness,
    ScientificArrayOwnerKind,
)
from tricycle_reaction_db.ingestion import (
    MolOPFrameRecords,
    frame_records_from_molop,
    parse_revision_record_from_molop,
    protocol_record_from_molop_segment,
    segment_record_from_molop,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PersistedMolOPArtifact:
    parse_revision: ParseRevision
    frames_by_file_index: dict[int, CalculationFrame]
    frame_count: int
    array_counts: dict[str, int]
    failed_frame_count: int = 0
    parse_diagnostics: tuple[dict[str, Any], ...] = ()
    parse_completeness: ParseCompleteness = ParseCompleteness.COMPLETE


_GEOMETRY_CONTEXT_FIELDS = (
    "topologies",
    "formulas_by_hash",
    "topologies_by_identity",
    "topology_derivations_by_key",
    "geometries_by_hash",
    "exact_geometry_keys_loaded",
    "equivalent_geometry_by_key",
    "equivalent_geometry_candidates",
    "equivalent_geometry_keys_loaded",
    "geometries_to_reconcile",
    "reaction_participants_by_topology",
    "mapped_reactions_by_id",
)


def _snapshot_geometry_context(context: GeometryPersistenceContext) -> dict[str, Any]:
    return {field: copy(getattr(context, field)) for field in _GEOMETRY_CONTEXT_FIELDS}


def _restore_geometry_context(
    context: GeometryPersistenceContext,
    snapshot: dict[str, Any],
) -> None:
    for field, value in snapshot.items():
        current = getattr(context, field)
        current.clear()
        current.update(value)


def _frame_failure_diagnostic(
    *,
    file_frame_index: int | None,
    segment_index: int,
    error: Exception,
    stage: str,
) -> dict[str, Any]:
    return {
        "code": "frame_persistence_failed" if stage == "persistence" else "frame_parse_failed",
        "stage": stage,
        "segment_index": segment_index,
        "file_frame_index": file_frame_index,
        "error_type": type(error).__name__,
        "message": str(error) or type(error).__name__,
    }


def _json_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()


def _revision_record_hash(
    artifact: ArtifactFile | str,
    chem_file: Any,
    records: list[MolOPFrameRecords],
) -> str:
    artifact_sha256 = (
        artifact.content_sha256 if isinstance(artifact, ArtifactFile) else artifact
    )
    return _json_sha256(
        {
            "artifact_sha256": artifact_sha256,
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
    record_sha256: str | None = None,
    force_new_revision: bool = False,
    fast_insert: bool = False,
    parallel_frame_persistence: bool = False,
    geometry_context: GeometryPersistenceContext | None = None,
    preload_geometry_context: bool = True,
    defer_reconciliation: bool = False,
    parse_diagnostics: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> PersistedMolOPArtifact:
    """Persist every parsed segment, frame, graph, coordinate, scalar, and array."""

    if source_compression is None:
        if (
            chem_file.artifact_sha256 is not None
            and chem_file.artifact_sha256 != artifact.content_sha256
        ):
            raise ValueError("MolOP source bytes do not match the stored ArtifactFile SHA-256")
        if (
            chem_file.artifact_size_bytes is not None
            and chem_file.artifact_size_bytes != artifact.size_bytes
        ):
            raise ValueError("MolOP source byte size does not match the stored ArtifactFile")
    diagnostics: list[dict[str, Any]] = list(parse_diagnostics or ())
    if records is None:
        frame_records: list[MolOPFrameRecords] = []
        for fallback_index, frame in enumerate(chem_file):
            try:
                frame_records.append(
                    frame_records_from_molop(
                        frame,
                        export_schema_version=chem_file.schema_version,
                        fallback_index=fallback_index,
                    )
                )
            except Exception as error:
                diagnostics.append(
                    _frame_failure_diagnostic(
                        file_frame_index=int(
                            getattr(frame, "file_frame_index", fallback_index)
                            or fallback_index
                        ),
                        segment_index=int(getattr(frame, "segment_index", 0) or 0),
                        error=error,
                        stage="conversion",
                    )
                )
    else:
        frame_records = list(records)
    failed_frame_count = sum(
        item.get("code")
        in {"frame_parse_failed", "frame_persistence_failed", "ts_inference_failed"}
        for item in diagnostics
    )
    # Parallel frame persistence here means batched, revision-local writes in
    # one transaction. A shared SQLAlchemy Session cannot be forked safely;
    # client-side IDs and deferred flushes provide multi-row batching without
    # weakening rollback or idempotency guarantees.
    effective_fast_insert = fast_insert or (
        parallel_frame_persistence and not force_new_revision
    )
    previous_fast_insert = session.info.get("tricycle_fast_insert", False)
    previous_autoflush = session.autoflush
    session.info["tricycle_fast_insert"] = effective_fast_insert
    if effective_fast_insert:
        session.autoflush = False
    try:
        revision = persist_parse_revision(
            session,
            artifact,
            parse_revision_record_from_molop(
                chem_file,
                started_at=started_at,
                source_compression=source_compression,
                # MolOP's source spans and identity describe the decoded
                # stream for compressed uploads.  Only uncompressed inputs
                # can use ArtifactFile's byte identity directly.
                artifact_sha256=(
                    artifact.content_sha256 if source_compression is None else None
                ),
                artifact_size_bytes=(artifact.size_bytes if source_compression is None else None),
            ),
            force_new_revision=force_new_revision,
        )
        frames_by_file_index: dict[int, CalculationFrame] = {}
        array_counts: dict[str, int] = {}
        active_geometry_context = geometry_context or GeometryPersistenceContext()
        if preload_geometry_context:
            preload_snapshot = _snapshot_geometry_context(active_geometry_context)
            pending_snapshot = list(session.info.get("_fast_pending_entities", ()))
            try:
                preload_scope = (
                    nullcontext() if effective_fast_insert else session.begin_nested()
                )
                with preload_scope:
                    preload_molecular_geometry_context(
                        session,
                        [
                            (record.molecule, record.frame.coordinate_decimal_places)
                            for record in frame_records
                        ],
                        context=active_geometry_context,
                    )
            except Exception as error:
                # A malformed frame must not prevent the remaining frames from
                # using the regular per-frame persistence path.  Roll back
                # only the preload savepoint and discard its cache additions.
                _restore_geometry_context(active_geometry_context, preload_snapshot)
                if effective_fast_insert:
                    session.info["_fast_pending_entities"] = pending_snapshot
                diagnostics.append(
                    {
                        "code": "geometry_preload_failed",
                        "stage": "preload",
                        "error_type": type(error).__name__,
                        "message": str(error) or type(error).__name__,
                    }
                )
                logger.warning(
                    "geometry preload failed; falling back to per-frame writes",
                    exc_info=True,
                )
                preload_geometry_context = False
        records_by_segment: dict[int, list[MolOPFrameRecords]] = {}
        for record in frame_records:
            records_by_segment.setdefault(record.segment_index, []).append(record)
        source_segments = tuple(chem_file.source_segments)
        if not source_segments and frame_records:
            # MolOP omits source segments when evidence capture is disabled.
            # Keep the relational segment required by the schema, but leave all
            # source-location fields NULL rather than manufacturing offsets.
            source_segments = (None,)
        persisted_segments = []
        for source_segment in source_segments:
            protocol_record = (
                protocol_record_from_molop_segment(source_segment)
                if source_segment is not None
                else None
            )
            protocol = (
                persist_calculation_protocol(session, protocol_record)
                if protocol_record is not None
                else None
            )
            segment_record = (
                segment_record_from_molop(source_segment)
                if source_segment is not None
                else CalculationSegmentRecord(
                    segment_index=0,
                    source_frame_count=len(chem_file),
                    parse_completeness=(
                        ParseCompleteness.PARTIAL
                        if diagnostics or len(frame_records) != len(chem_file)
                        else ParseCompleteness.NOT_ASSESSED
                    ),
                    parse_diagnostics=list(diagnostics),
                    program_metadata={"source_evidence_captured": False},
                )
            )
            segment = persist_calculation_segment(
                session,
                revision,
                protocol,
                segment_record,
            )
            persisted_segments.append(segment)
            segment_frames = records_by_segment.get(
                source_segment.segment_index if source_segment is not None else 0,
                [],
            )
            segment_diagnostics = list(segment_record.parse_diagnostics)
            captured_indices = set(
                segment_record.program_metadata.get("molop_captured_frame_indices", ())
            )
            segment_diagnostics.extend(
                diagnostic
                for diagnostic in diagnostics
                if diagnostic.get("file_frame_index") in captured_indices
                and diagnostic not in segment_diagnostics
            )
            segment_failed = any(
                record.frame.parse_completeness is ParseCompleteness.PARTIAL
                for record in segment_frames
            )
            if segment_failed:
                frame_diagnostics = [
                    diagnostic
                    for record in segment_frames
                    for diagnostic in record.frame.parse_diagnostics
                ]
                segment_diagnostics.extend(frame_diagnostics)
                diagnostics.extend(frame_diagnostics)
            for record in segment_frames:
                file_frame_index = record.frame.file_frame_index
                frame_snapshot = _snapshot_geometry_context(active_geometry_context)
                pending_snapshot = list(session.info.get("_fast_pending_entities", ()))
                array_counts_snapshot = dict(array_counts)
                persisted_frame: CalculationFrame | None = None
                try:
                    # Fast ingestion defers all INSERTs to the batch flush;
                    # opening a savepoint would force SQLAlchemy to flush
                    # revision-local rows before their deferred Geometry
                    # parents.  Its pending queue is therefore the frame
                    # isolation boundary.  The regular path uses a real
                    # savepoint because each child is flushed immediately.
                    frame_scope = (
                        nullcontext() if effective_fast_insert else session.begin_nested()
                    )
                    with frame_scope:
                        if file_frame_index is None or record.frame.frame_index is None:
                            raise ValueError("MolOP frame is missing stable source indices")
                        molecule = persist_molecular_geometry(
                            session,
                            record.molecule,
                            coordinate_decimal_places=record.frame.coordinate_decimal_places,
                            context=active_geometry_context,
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
                                    "observed_coordinate_hash": (
                                        record.molecule.observed_coordinate_hash
                                    ),
                                    "observed_to_geometry_atom_indices": (
                                        molecule.observed_to_geometry_atom_indices
                                    ),
                                    "observed_to_geometry_transform": list(
                                        molecule.observed_to_geometry_transform
                                    ),
                                    "geometry_assignment_rmsd_angstrom": (
                                        molecule.coordinate_rmsd_angstrom
                                    ),
                                    "geometry_assignment_max_abs_angstrom": (
                                        molecule.coordinate_max_abs_angstrom
                                    ),
                                    "geometry_assignment_policy_version": (
                                        "geometry-internal-coordinate-match-v3"
                                    ),
                                }
                            )
                        persisted_frame = persist_calculation_frame(
                            session,
                            segment,
                            molecule.geometry,
                            molecule.topology_derivation,
                            frame_record,
                            reconcile=False,
                        )
                        frames_by_file_index[file_frame_index] = persisted_frame
                        if record.energy is not None:
                            energy = persist_frame_energy_result(
                                session, persisted_frame, record.energy
                            )
                            for observation in record.energy_observations:
                                persist_energy_observation(session, energy, observation)
                        if record.optimization is not None:
                            persist_geometry_optimization_result(
                                session, persisted_frame, record.optimization
                            )
                        if record.vibration is not None:
                            persist_vibration_result(session, persisted_frame, record.vibration)
                        if record.status is not None:
                            persist_calculation_status_result(
                                session, persisted_frame, record.status
                            )
                        _persist_extended_results(session, persisted_frame, record, array_counts)
                        if record.thermochemistry is not None:
                            persist_thermochemistry_result(
                                session,
                                persisted_frame,
                                record.thermochemistry,
                                reconcile=False,
                            )
                except Exception as error:
                    segment_failed = True
                    failed_frame_count += 1
                    frames_by_file_index.pop(file_frame_index, None)
                    if persisted_frame is not None:
                        segment_frames_collection = segment.__dict__.get("frames")
                        if isinstance(segment_frames_collection, list):
                            with suppress(ValueError):
                                segment_frames_collection.remove(persisted_frame)
                    _restore_geometry_context(active_geometry_context, frame_snapshot)
                    if effective_fast_insert:
                        session.info["_fast_pending_entities"] = pending_snapshot
                    array_counts.clear()
                    array_counts.update(array_counts_snapshot)
                    diagnostic = _frame_failure_diagnostic(
                        file_frame_index=file_frame_index,
                        segment_index=segment.segment_index,
                        error=error,
                        stage="persistence",
                    )
                    diagnostics.append(diagnostic)
                    segment_diagnostics.append(diagnostic)
                    logger.warning(
                        "skipping failed calculation frame %s in segment %s",
                        file_frame_index,
                        segment.segment_index,
                        exc_info=True,
                    )
            if (
                segment_failed
                or (
                    segment_record.source_frame_count is not None
                    and len(segment_frames) != segment_record.source_frame_count
                )
            ):
                segment.parse_completeness = ParseCompleteness.PARTIAL
                segment.parse_diagnostics = segment_diagnostics
                session.add(segment)
            elif segment.parse_completeness is ParseCompleteness.PARTIAL:
                segment.parse_diagnostics = segment_diagnostics
                session.add(segment)

        if not frames_by_file_index:
            raise ValueError("no calculation frames could be persisted from the source")

        # Batch ingestion can keep revision-local rows pending across files.
        # The shared context owns their identities, and the final reconciliation
        # flush groups each table across the complete batch.
        defer_batch_flush = defer_reconciliation and effective_fast_insert
        if not defer_batch_flush:
            _attach_pending_entities(session)
            session.flush()
        if not defer_reconciliation:
            reconcile_molop_geometry_context(session, active_geometry_context)

        partial_parse = bool(diagnostics) or any(
            segment.parse_completeness is ParseCompleteness.PARTIAL
            for segment in persisted_segments
        ) or any(
            record.frame.parse_completeness is ParseCompleteness.PARTIAL
            for record in frame_records
        )
        if partial_parse:
            revision.parse_completeness = ParseCompleteness.PARTIAL
            revision.parse_diagnostics = [*revision.parse_diagnostics, *diagnostics]
            session.add(revision)

        finalize_parse_revision(
            session,
            revision,
            ParseRevisionCompletionRecord(
                record_sha256=record_sha256 or _revision_record_hash(
                    artifact,
                    chem_file,
                    frame_records,
                ),
                completed_at=completed_at,
            ),
            defer_flush=defer_batch_flush,
        )
        return PersistedMolOPArtifact(
            parse_revision=revision,
            frames_by_file_index=frames_by_file_index,
            frame_count=len(frames_by_file_index),
            array_counts=array_counts,
            failed_frame_count=failed_frame_count,
            parse_diagnostics=tuple(diagnostics),
            parse_completeness=(
                ParseCompleteness.PARTIAL if partial_parse else ParseCompleteness.COMPLETE
            ),
        )
    finally:
        session.autoflush = previous_autoflush
        session.info["tricycle_fast_insert"] = previous_fast_insert


def reconcile_molop_geometry_context(
    session: Session,
    context: GeometryPersistenceContext,
) -> None:
    """Reconcile all geometries from a batch after their rows are flushed."""

    _attach_pending_entities(session)
    session.flush()
    reconcilable_ids = reconcilable_geometry_ids(
        session,
        set(context.geometries_to_reconcile),
    )
    # Deferred TS inference may already have populated path/node identities in
    # this cache before participant reconciliation.  Reuse it so those rows
    # remain visible to the final geometry pass; a fresh cache would discard
    # the in-memory identities and repeat work unnecessarily.
    reconciliation_cache = context.reconciliation_cache
    if not isinstance(reconciliation_cache, ReconciliationBatchCache):
        reconciliation_cache = ReconciliationBatchCache()
    reconciliation_cache.thermodynamic_property_geometry_ids.update(reconcilable_ids)
    context.reconciliation_cache = reconciliation_cache
    preload_reconciliation_context(
        session,
        {
            geometry.topology_id
            for geometry in context.geometries_to_reconcile.values()
            if geometry.id in reconcilable_ids
        },
        participants_by_topology=context.reaction_participants_by_topology,
        mapped_reactions_by_id=context.mapped_reactions_by_id,
        cache=reconciliation_cache,
    )
    previous_autoflush = session.autoflush
    session.autoflush = False
    try:
        for geometry in context.geometries_to_reconcile.values():
            geometry_id = geometry.id
            if geometry_id is not None:
                reconcile_geometry_with_reactions(
                    session,
                    geometry,
                    eligibility=geometry_id in reconcilable_ids,
                    participants_by_topology=context.reaction_participants_by_topology,
                    mapped_reactions_by_id=context.mapped_reactions_by_id,
                    cache=reconciliation_cache,
                )
        _attach_pending_entities(session)
        session.flush()
        for mapped_reaction in reconciliation_cache.affected_reactions_by_id.values():
            refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
    finally:
        session.autoflush = previous_autoflush


__all__ = [
    "PersistedMolOPArtifact",
    "persist_molop_calculation_artifact",
    "reconcile_molop_geometry_context",
]
