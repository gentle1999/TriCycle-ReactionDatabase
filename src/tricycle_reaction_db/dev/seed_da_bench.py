"""Seed the development stack with the checked DA benchmark fixture."""

from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from molop import AutoParser, molopconfig
from rdkit.Chem import rdChemReactions
from sqlalchemy import create_engine, func
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import (
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    MappedReactionEdgeRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
    MappedReactionRecord,
    NormalizedMoleculeRecord,
    ParseRevisionCompletionRecord,
)
from tricycle_reaction_db.application.services import (
    atom_maps_from_source_order,
    finalize_parse_revision,
    mapped_smiles_for_topology,
    persist_artifact_file,
    persist_atomic_population_series,
    persist_bond_order_result,
    persist_calculation_frame,
    persist_calculation_protocol,
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
    persist_logical_reaction,
    persist_logical_reaction_participant,
    persist_mapped_reaction,
    persist_mapped_reaction_edge,
    persist_mapped_reaction_node,
    persist_mapped_reaction_node_geometry,
    persist_mapped_reaction_node_geometry_mapping,
    persist_molecular_geometry,
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
    persist_transition_state_endpoints_from_molop_frame,
    persist_vibration_result,
    reaction_hash_for_participants,
    reconcile_mapped_reaction_with_geometries,
    validate_logical_reaction,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    CalculationStatusResult,
    ElectronicState,
    EnergyObservation,
    FrameEnergyResult,
    Geometry,
    GeometryOptimizationResult,
    LogicalReaction,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularFormula,
    MolecularTopology,
    ParseRevision,
    ScientificArray,
    ThermochemistryResult,
    VibrationResult,
    WorkflowManifest,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ElectronicStateSetKind,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    ScientificArrayOwnerKind,
    StorageStatus,
)
from tricycle_reaction_db.ingestion import (
    MolOPFrameRecords,
    artifact_record_from_path,
    frame_records_from_molop,
    parse_revision_record_from_molop,
    protocol_record_from_molop_segment,
    segment_record_from_molop,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings


@dataclass(frozen=True, slots=True)
class SeedResult:
    logical_reaction_id: UUID
    mapped_reaction_id: UUID
    edge_id: UUID
    node_ids: dict[str, UUID]
    artifact_ids: dict[str, UUID]
    frame_counts: dict[str, int]
    array_counts: dict[str, int]
    row_counts: dict[str, int]


def _required_id(entity: object, label: str) -> UUID:
    entity_id = getattr(entity, "id", None)
    if not isinstance(entity_id, UUID):
        raise RuntimeError(f"database did not assign {label}.id")
    return entity_id


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
                        observation.model_dump(mode="json")
                        for observation in record.energy_observations
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
                        series.model_dump(mode="json") for series in record.atomic_population_series
                    ],
                    "polarizability": (
                        record.polarizability.model_dump(mode="json")
                        if record.polarizability
                        else None
                    ),
                    "nmr": record.nmr.model_dump(mode="json") if record.nmr else None,
                    "nmr_shielding_tensors": [
                        tensor.model_dump(mode="json") for tensor in record.nmr_shielding_tensors
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
                        array.model_dump(mode="json", exclude={"data"}) for array in record.arrays
                    ],
                    "array_assignments": [
                        assignment.model_dump(mode="json")
                        for assignment in record.array_assignments
                    ],
                }
                for record in records
            ],
        }
    )


def _remove_legacy_seed_manifest(
    session: Session,
    store: RustFSObjectStore,
    *,
    manifest_key: str,
) -> None:
    existing = session.exec(
        select(WorkflowManifest).where(
            WorkflowManifest.manifest_key == manifest_key,
            WorkflowManifest.revision == 1,
        )
    ).first()
    if existing is None:
        return
    if not existing.validation_metadata.get("development_seed"):
        raise ValueError("refusing to remove a non-development workflow manifest")
    old_artifact = existing.artifact_file
    object_key = old_artifact.object_key
    session.delete(existing)
    session.flush()
    session.delete(old_artifact)
    session.flush()
    if store.exists(object_key):
        store.delete(object_key)


def _extract_logs(
    fixture_root: Path,
    manifest: dict[str, Any],
    output_root: Path,
) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for entry in manifest["logs"]:
        compressed_path = fixture_root / entry["relative_path"]
        compressed_payload = compressed_path.read_bytes()
        if sha256(compressed_payload).hexdigest() != entry["gzip_sha256"]:
            raise ValueError(f"compressed fixture hash mismatch: {compressed_path}")
        output_path = output_root / Path(entry["relative_path"]).with_suffix("")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(gzip.decompress(compressed_payload))
        payload = output_path.read_bytes()
        if len(payload) != entry["source_size_bytes"]:
            raise ValueError(f"fixture size mismatch: {compressed_path}")
        if sha256(payload).hexdigest() != entry["source_sha256"]:
            raise ValueError(f"source fixture hash mismatch: {compressed_path}")
        if payload.count(b"Normal termination of Gaussian") != entry["normal_termination_count"]:
            raise ValueError(f"Gaussian termination count mismatch: {compressed_path}")
        paths[entry["role"]] = output_path
    return paths


def _validate_parsed_records(
    manifest: dict[str, Any],
    parsed_records: dict[str, list[NormalizedMoleculeRecord]],
) -> None:
    expected_by_role = {entry["role"]: entry for entry in manifest["logs"]}
    if parsed_records.keys() != expected_by_role.keys():
        raise ValueError("parsed log roles do not match the fixture manifest")
    for role, records in parsed_records.items():
        expected = expected_by_role[role]
        if len(records) != expected["frame_count"]:
            raise ValueError(f"MolOP frame count mismatch for {role}")
        if any(record.geometry.atom_count != expected["atom_count"] for record in records):
            raise ValueError(f"MolOP atom count mismatch for {role}")
        observed_smiles = {record.topology.canonical_isomeric_smiles for record in records}
        if observed_smiles != {expected["final_topology_smiles"]}:
            rendered_smiles = sorted(smiles or "<unavailable>" for smiles in observed_smiles)
            raise ValueError(f"MolOP topology mismatch for {role}: {rendered_smiles!r}")
        if records[-1].formula.hill_formula != expected["final_formula"]:
            raise ValueError(f"MolOP formula mismatch for {role}")


def _persist_verified_artifact(
    session: Session,
    store: RustFSObjectStore,
    path: Path,
    *,
    artifact_kind: ArtifactKind,
) -> ArtifactFile:
    pending_record = artifact_record_from_path(
        path,
        bucket=store.settings.bucket,
        artifact_kind=artifact_kind,
        storage_status=StorageStatus.PENDING,
    )
    artifact = persist_artifact_file(session, pending_record)
    if artifact.bucket != store.settings.bucket:
        raise ValueError(f"Artifact identity resolved to a different RustFS bucket for {path}")
    if artifact.artifact_kind is not artifact_kind:
        raise ValueError(f"Artifact identity resolved to a different kind for {path}")
    # Content hash is the artifact identity.  A previous upload can legitimately
    # use a time-partitioned key while the fixture seed uses the stable raw key.
    # Reuse the verified object location and validate its bytes before proceeding.
    payload = path.read_bytes()
    if store.exists(artifact.object_key):
        stored = store.head(artifact.object_key)
    else:
        stored = store.put_bytes(
            key=artifact.object_key,
            payload=payload,
            content_type=artifact.media_type,
            metadata={"source": "da-bench-minimal-seed"},
        )
    if stored.size != artifact.size_bytes or stored.sha256 != artifact.content_sha256:
        raise ValueError(f"RustFS metadata mismatch for {path}")
    if store.get_bytes(artifact.object_key) != payload:
        raise ValueError(f"RustFS byte verification failed for {path}")
    artifact.bucket = stored.bucket
    artifact.version_id = stored.version_id
    artifact.etag = stored.etag
    artifact.storage_verified_at = stored.last_modified
    artifact.storage_status = StorageStatus.AVAILABLE
    session.add(artifact)
    session.flush()
    return artifact


def _row_counts(session: Session) -> dict[str, int]:
    return {
        "artifact_file": session.exec(select(func.count()).select_from(ArtifactFile)).one(),
        "molecular_formula": session.exec(select(func.count()).select_from(MolecularFormula)).one(),
        "molecular_topology": session.exec(
            select(func.count()).select_from(MolecularTopology)
        ).one(),
        "geometry": session.exec(select(func.count()).select_from(Geometry)).one(),
        "calculation_protocol": session.exec(
            select(func.count()).select_from(CalculationProtocol)
        ).one(),
        "parse_revision": session.exec(select(func.count()).select_from(ParseRevision)).one(),
        "calculation_segment": session.exec(
            select(func.count()).select_from(CalculationSegment)
        ).one(),
        "calculation_frame": session.exec(select(func.count()).select_from(CalculationFrame)).one(),
        "calculation_status_result": session.exec(
            select(func.count()).select_from(CalculationStatusResult)
        ).one(),
        "energy_observation": session.exec(
            select(func.count()).select_from(EnergyObservation)
        ).one(),
        "frame_energy_result": session.exec(
            select(func.count()).select_from(FrameEnergyResult)
        ).one(),
        "geometry_optimization_result": session.exec(
            select(func.count()).select_from(GeometryOptimizationResult)
        ).one(),
        "workflow_manifest": session.exec(select(func.count()).select_from(WorkflowManifest)).one(),
        "manifest_artifact_binding": session.exec(
            select(func.count()).select_from(ManifestArtifactBinding)
        ).one(),
        "logical_reaction": session.exec(select(func.count()).select_from(LogicalReaction)).one(),
        "logical_reaction_participant": session.exec(
            select(func.count()).select_from(LogicalReactionParticipant)
        ).one(),
        "mapped_reaction": session.exec(select(func.count()).select_from(MappedReaction)).one(),
        "mapped_reaction_participant": session.exec(
            select(func.count()).select_from(MappedReactionParticipant)
        ).one(),
        "mapped_reaction_node": session.exec(
            select(func.count()).select_from(MappedReactionNode)
        ).one(),
        "mapped_reaction_node_geometry": session.exec(
            select(func.count()).select_from(MappedReactionNodeGeometry)
        ).one(),
        "mapped_reaction_node_geometry_mapping": session.exec(
            select(func.count()).select_from(MappedReactionNodeGeometryMapping)
        ).one(),
        "mapped_reaction_edge": session.exec(
            select(func.count()).select_from(MappedReactionEdge)
        ).one(),
        "scientific_array": session.exec(select(func.count()).select_from(ScientificArray)).one(),
        "thermochemistry_result": session.exec(
            select(func.count()).select_from(ThermochemistryResult)
        ).one(),
        "vibration_result": session.exec(select(func.count()).select_from(VibrationResult)).one(),
    }


def _persist_latest_molop_results(
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
        population_result = persist_charge_spin_population_result(
            session, frame, record.charge_spin_populations
        )
        for series_record in record.atomic_population_series:
            series = persist_atomic_population_series(session, population_result, series_record)
            owners[
                (ScientificArrayOwnerKind.ATOMIC_POPULATION_SERIES, series_record.series_key)
            ] = series
    if record.polarizability is not None:
        owners[(ScientificArrayOwnerKind.POLARIZABILITY_RESULT, None)] = (
            persist_polarizability_result(session, frame, record.polarizability)
        )
    if record.nmr is not None:
        nmr_result = persist_nmr_result(session, frame, record.nmr)
        owners[(ScientificArrayOwnerKind.NMR_RESULT, None)] = nmr_result
        for tensor_record in record.nmr_shielding_tensors:
            tensor = persist_nmr_shielding_tensor(session, nmr_result, tensor_record)
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
        state_set_record.kind: persist_electronic_state_set(session, frame, state_set_record)
        for state_set_record in record.electronic_state_sets
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
    for configuration_record in record.electronic_configurations:
        persist_electronic_configuration(
            session,
            states[(configuration_record.set_kind, configuration_record.state_ordinal)],
            configuration_record,
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
    for assignment_record in record.array_assignments:
        owner = owners[(assignment_record.owner_kind, assignment_record.owner_key)]
        array = arrays[(assignment_record.array_kind, assignment_record.array_ordinal)]
        persist_scientific_array_assignment(session, array, owner, assignment_record)


def seed_da_bench_fixture(
    session: Session,
    store: RustFSObjectStore,
    fixture_root: Path,
) -> SeedResult:
    """Persist the minimal checked DA path and return its stable database identities."""

    fixture_manifest_path = fixture_root / "manifest.json"
    manifest_payload: dict[str, Any] = json.loads(fixture_manifest_path.read_text())
    workflow = manifest_payload["workflow"]
    workflow["manifest_key"] = f"dev-seed:{workflow['manifest_key']}"
    molopconfig.show_progress_bar = False

    with TemporaryDirectory(prefix="tricycle-da-seed-") as directory:
        temporary_root = Path(directory)
        log_paths = _extract_logs(fixture_root, manifest_payload, temporary_root)
        parsed_files: dict[str, Any] = {
            role: AutoParser(
                str(path),
                n_jobs=1,
                capture_source_evidence=True,
                release_file_content=True,
            )[0]
            for role, path in log_paths.items()
        }
        parsed_frame_records = {
            role: [
                frame_records_from_molop(frame, export_schema_version=chem_file.schema_version)
                for frame in chem_file
            ]
            for role, chem_file in parsed_files.items()
        }
        parsed_records = {
            role: [record.molecule for record in records]
            for role, records in parsed_frame_records.items()
        }
        _validate_parsed_records(manifest_payload, parsed_records)
        for entry in manifest_payload["logs"]:
            role = entry["role"]
            chem_file = parsed_files[role]
            if not chem_file.source_complete or len(chem_file) != entry["frame_count"]:
                raise ValueError(f"MolOP source capture is incomplete for {role}")
            if chem_file.artifact_sha256 != entry["source_sha256"]:
                raise ValueError(f"MolOP artifact identity mismatch for {role}")
        artifacts: dict[str, ArtifactFile] = {}
        for role, path in log_paths.items():
            artifacts[role] = _persist_verified_artifact(
                session, store, path, artifact_kind=ArtifactKind.CALCULATION_OUTPUT
            )
        persisted_participants = {}
        for declaration in workflow["participants"]:
            role = declaration["log_role"]
            persisted_participants[role] = persist_molecular_geometry(
                session, parsed_records[role][-1]
            )
        selected_frames: dict[tuple[str, int, int], CalculationFrame] = {}
        frame_counts: dict[str, int] = {}
        array_counts: dict[str, int] = {}
        for log_role in log_paths:
            artifact = artifacts[log_role]
            chem_file = parsed_files[log_role]
            records = parsed_frame_records[log_role]
            if (
                chem_file.artifact_sha256 != artifact.content_sha256
                or chem_file.artifact_size_bytes != artifact.size_bytes
            ):
                raise ValueError(
                    f"MolOP artifact identity does not match ArtifactFile for {log_role}"
                )
            revision = persist_parse_revision(
                session,
                artifact,
                parse_revision_record_from_molop(
                    chem_file,
                    started_at=artifact.storage_verified_at,
                ),
            )
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
                    frame
                    for frame in chem_file
                    if frame.segment_index == source_segment.segment_index
                ]
                for frame in segment_frames:
                    if frame.file_frame_index is None or frame.segment_frame_index is None:
                        raise ValueError("MolOP frame is missing stable source indices")
                    record = records[frame.file_frame_index]
                    persisted_molecule = persist_molecular_geometry(
                        session,
                        record.molecule,
                    )
                    geometry = persisted_molecule.geometry
                    persisted_frame = persist_calculation_frame(
                        session,
                        segment,
                        geometry,
                        persisted_molecule.topology_derivation,
                        record.frame,
                    )
                    selected_frames[
                        (
                            log_role,
                            source_segment.segment_index,
                            frame.segment_frame_index,
                        )
                    ] = persisted_frame
                    if record.energy is not None:
                        energy_result = persist_frame_energy_result(
                            session,
                            persisted_frame,
                            record.energy,
                        )
                        for observation in record.energy_observations:
                            persist_energy_observation(session, energy_result, observation)
                    if record.optimization is not None:
                        persist_geometry_optimization_result(
                            session,
                            persisted_frame,
                            record.optimization,
                        )
                    if record.vibration is not None:
                        persist_vibration_result(session, persisted_frame, record.vibration)
                    if log_role == "transition_state" and frame.is_TS is True:
                        persist_transition_state_endpoints_from_molop_frame(
                            session,
                            calculation_frame=persisted_frame,
                            source_frame=frame,
                        )
                    if record.status is not None:
                        persist_calculation_status_result(session, persisted_frame, record.status)
                    _persist_latest_molop_results(
                        session,
                        persisted_frame,
                        record,
                        array_counts,
                    )
                    if record.thermochemistry is not None:
                        persist_thermochemistry_result(
                            session,
                            persisted_frame,
                            record.thermochemistry,
                        )
            completed_at = artifact.storage_verified_at
            if completed_at is None:
                raise RuntimeError(f"Artifact storage verification is missing for {log_role}")
            finalize_parse_revision(
                session,
                revision,
                ParseRevisionCompletionRecord(
                    record_sha256=_revision_record_hash(artifact, chem_file, records),
                    completed_at=completed_at,
                ),
            )
            frame_counts[log_role] = len(chem_file)

        participant_payloads = []
        identities = []
        for declaration in workflow["participants"]:
            persisted = persisted_participants[declaration["log_role"]]
            atom_maps = atom_maps_from_source_order(
                persisted.geometry,
                declaration["source_atom_map_numbers"],
                persisted.observed_to_geometry_atom_indices,
            )
            side = LogicalReactionParticipantSide(declaration["side"])
            identities.append((side, persisted.topology, 1))
            participant_payloads.append((declaration, persisted, side, atom_maps))
        reaction = persist_logical_reaction(
            session,
            LogicalReactionRecord(
                reaction_key=workflow["reaction_key"],
                label="DA bench 4+2 cycloaddition",
                cycloaddition_pattern="4+2",
                reaction_hash=reaction_hash_for_participants(identities),
            ),
        )
        reaction_participants = {}
        for declaration, persisted, side, _atom_maps in participant_payloads:
            participant = persist_logical_reaction_participant(
                session,
                reaction,
                persisted.topology,
                LogicalReactionParticipantRecord(
                    side=side,
                    participant_index=declaration["participant_index"],
                    role=LogicalReactionParticipantRole(declaration["role"]),
                ),
            )
            reaction_participants[(side.value, declaration["participant_index"])] = participant
        validate_logical_reaction(reaction)
        if len(workflow["mapped_reaction_smiles"]) != 1:
            raise ValueError("each seeded MappedReaction requires exactly one mapped SMILES")
        definition = rdChemReactions.ReactionFromSmarts(
            workflow["mapped_reaction_smiles"][0], useSmiles=True
        )
        if definition is None:
            raise ValueError("RDKit could not parse seeded mapped reaction SMILES")
        canonical_mapped_smiles = rdChemReactions.ReactionToSmiles(definition, True)
        mapping_hash = sha256(canonical_mapped_smiles.encode("utf-8")).hexdigest()
        mapped_reaction = persist_mapped_reaction(
            session,
            reaction,
            MappedReactionRecord(
                mapped_reaction_key=workflow["path_key"],
                label=workflow["path_key"],
                mapped_reaction_kind=MappedReactionKind.CURATED,
                mapped_reaction_smiles=canonical_mapped_smiles,
                mapping_hash=mapping_hash,
            ),
        )
        # A reaction declaration sees only participant Geometries with at
        # least one converged optimization.  Curated selectors below then
        # promote their chosen conformers without duplicating them.
        reconcile_mapped_reaction_with_geometries(session, mapped_reaction)
        mapped_participants = {
            participant.logical_reaction_participant_id: participant
            for participant in mapped_reaction.participants
        }
        nodes = {}
        for declaration in workflow["nodes"]:
            node = persist_mapped_reaction_node(
                session,
                mapped_reaction,
                MappedReactionNodeRecord(
                    node_key=declaration["node_key"],
                    node_index=declaration["node_index"],
                    role=MappedReactionNodeRole(declaration["role"]),
                ),
            )
            nodes[declaration["node_key"]] = node
            for component in declaration["components"]:
                component_participant: MappedReactionParticipant | None = None
                if "participant_side" in component:
                    logical_participant = reaction_participants[
                        (component["participant_side"], component["participant_index"])
                    ]
                    component_participant = mapped_participants[
                        _required_id(logical_participant, "LogicalReactionParticipant")
                    ]
                authority_selector = component["geometry_authority"]
                authority_frame = selected_frames[
                    (
                        component["log_role"],
                        authority_selector["segment_index"],
                        authority_selector["frame_index"],
                    )
                ]
                topology_atom_maps = atom_maps_from_source_order(
                    authority_frame.geometry,
                    component["source_atom_map_numbers"],
                    authority_frame.observed_to_geometry_atom_indices,
                )
                node_geometry = persist_mapped_reaction_node_geometry(
                    session,
                    node,
                    authority_frame.geometry,
                    MappedReactionNodeGeometryRecord(
                        component_key=component["component_key"],
                        component_index=component["component_index"],
                        coordinate_index=0,
                        is_primary=True,
                    ),
                    mapped_reaction_participant=component_participant,
                )
                persist_mapped_reaction_node_geometry_mapping(
                    session,
                    node_geometry,
                    MappedReactionNodeGeometryMappingRecord(
                        geometry_atom_map_numbers=topology_atom_maps,
                        mapped_smiles=mapped_smiles_for_topology(
                            authority_frame.geometry.topology,
                            topology_atom_maps,
                        ),
                        mapping_method="manifest-explicit",
                        mapping_version="coordinate-map-v1",
                        verified=True,
                    ),
                )
        edge_declaration = workflow["edge"]
        edge = persist_mapped_reaction_edge(
            session,
            mapped_reaction,
            nodes[edge_declaration["source_node_key"]],
            nodes[edge_declaration["target_node_key"]],
            MappedReactionEdgeRecord(
                edge_key=edge_declaration["edge_key"],
                edge_kind=MappedReactionEdgeKind(edge_declaration["edge_kind"]),
            ),
            transition_state_node=nodes[edge_declaration["transition_state_node_key"]],
        )
        # The edge identifies which transition-state node contributes the
        # activation profile, so refresh only after the complete path exists.
        refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
        _remove_legacy_seed_manifest(
            session,
            store,
            manifest_key=workflow["manifest_key"],
        )
        session.flush()
        return SeedResult(
            logical_reaction_id=_required_id(reaction, "LogicalReaction"),
            mapped_reaction_id=_required_id(mapped_reaction, "MappedReaction"),
            edge_id=_required_id(edge, "MappedReactionEdge"),
            node_ids={key: _required_id(node, "MappedReactionNode") for key, node in nodes.items()},
            artifact_ids={
                key: _required_id(artifact, "ArtifactFile") for key, artifact in artifacts.items()
            },
            frame_counts=frame_counts,
            array_counts=array_counts,
            row_counts=_row_counts(session),
        )


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=Path("tests/fixtures/da_bench_minimal"),
    )
    args = parser.parse_args()
    fixture_root = args.fixture_root.resolve()
    if not (fixture_root / "manifest.json").is_file():
        parser.error(f"fixture manifest not found under {fixture_root}")
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with RustFSObjectStore(RustFSSettings()) as store:
            store.ensure_bucket()
            with Session(engine) as session:
                result = seed_da_bench_fixture(session, store, fixture_root)
                session.commit()
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=_json_default))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
