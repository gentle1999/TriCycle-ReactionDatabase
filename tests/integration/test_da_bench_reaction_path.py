import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from molop import AutoParser, molopconfig
from rdkit.Chem import rdChemReactions
from sqlalchemy import create_engine
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import (
    CalculationFrameRecord,
    CalculationSegmentRecord,
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    ManifestArtifactBindingRecord,
    MappedReactionEdgeRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
    MappedReactionRecord,
    NormalizedMoleculeRecord,
    ParseRevisionRecord,
    ThermochemistryResultRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services import (
    atom_maps_from_source_order,
    mapped_smiles_for_topology,
    persist_artifact_file,
    persist_calculation_frame,
    persist_calculation_protocol,
    persist_calculation_segment,
    persist_logical_reaction,
    persist_logical_reaction_participant,
    persist_manifest_artifact_binding,
    persist_mapped_reaction,
    persist_mapped_reaction_edge,
    persist_mapped_reaction_node,
    persist_mapped_reaction_node_geometry,
    persist_mapped_reaction_node_geometry_mapping,
    persist_molecular_geometry,
    persist_parse_revision,
    persist_thermochemistry_result,
    persist_workflow_manifest,
    reaction_hash_for_participants,
    validate_logical_reaction,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import ArtifactFile, LogicalReaction, WorkflowManifest
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactResolutionStatus,
    FrameRole,
    GeometryAssignmentKind,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    OptimizationStatus,
    QMSoftware,
    SCFStatus,
    SourceFormat,
    StorageStatus,
    TerminationStatus,
    WorkflowManifestStatus,
)
from tricycle_reaction_db.ingestion import (
    artifact_record_from_path,
    calculation_protocol_record,
    normalize_molop_frame,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _exact_geometry_assignment(molecule: NormalizedMoleculeRecord) -> dict[str, object]:
    return {
        "geometry_assignment_kind": GeometryAssignmentKind.PARSED_EXACT,
        "observed_coordinates": molecule.observed_coordinates,
        "observed_coordinate_hash": molecule.observed_coordinate_hash,
        "observed_to_geometry_atom_indices": molecule.observed_to_geometry_atom_indices,
        "observed_to_geometry_transform": molecule.observed_to_geometry_transform,
        "geometry_assignment_rmsd_angstrom": molecule.geometry_assignment_rmsd_angstrom,
        "geometry_assignment_max_abs_angstrom": molecule.geometry_assignment_max_abs_angstrom,
        "geometry_assignment_policy_version": "geometry-internal-coordinate-match-v3",
    }


def _frequency_summary(frame: Any) -> tuple[int | None, int | None, float | None]:
    vibrations = frame.vibrations
    if vibrations is None or vibrations.frequencies is None:
        return None, None, None
    frequencies = [float(value) for value in vibrations.frequencies.to("1 / centimeter").magnitude]
    if not frequencies:
        return 0, 0, None
    return len(frequencies), sum(value < 0 for value in frequencies), min(frequencies)


def _thermochemistry_record(frame: Any) -> ThermochemistryResultRecord | None:
    thermal = frame.thermal_informations
    if thermal is None or thermal.G_T is None:
        return None
    return ThermochemistryResultRecord(
        temperature_kelvin=float(frame.temperature.to("kelvin").magnitude),
        pressure_atm=float(frame.pressure.to("standard_atmosphere").magnitude),
        gibbs_free_energy_hartree=float(thermal.G_T.to("hartree / particle").magnitude),
        source_schema_version="molop-calculation-0.1.0",
    )


SELECTED_CALCULATIONS: dict[str, dict[int, dict[str, Any]]] = {
    "ene": {
        1: {
            "segment_span": (79945, 132919, 79945, 132919, 1407, 2264),
            "segment_sha256": "c64492731ccbcada5178413f05096ff9d969381a58afa1f4bcfb8ccca9611bec",
            "task_requests": ["freq"],
            "frames": {
                0: {
                    "file_frame_index": 4,
                    "frame_role": FrameRole.TERMINAL,
                    "span": (79945, 132919, 79945, 132919, 1407, 2264),
                    "sha256": "c64492731ccbcada5178413f05096ff9d969381a58afa1f4bcfb8ccca9611bec",
                }
            },
        }
    },
    "diene": {
        1: {
            "segment_span": (187249, 292687, 187249, 292687, 3065, 4615),
            "segment_sha256": "676ddc7defe2fa8375a51006470119ecaaab13e292ea850ba40c98c83a9739b5",
            "task_requests": ["freq"],
            "frames": {
                0: {
                    "file_frame_index": 6,
                    "frame_role": FrameRole.TERMINAL,
                    "span": (187249, 292687, 187249, 292687, 3065, 4615),
                    "sha256": "676ddc7defe2fa8375a51006470119ecaaab13e292ea850ba40c98c83a9739b5",
                }
            },
        }
    },
    "transition_state": {
        2: {
            "segment_span": (856761, 1018294, 856761, 1018294, 13700, 16014),
            "segment_sha256": "32d0a6cd5dbc3c1b020db784ecf67007d355ef299ae693a547faeee48974294b",
            "task_requests": ["freq"],
            "frames": {
                0: {
                    "file_frame_index": 22,
                    "frame_role": FrameRole.TERMINAL,
                    "span": (856761, 1018294, 856761, 1018294, 13700, 16014),
                    "sha256": "32d0a6cd5dbc3c1b020db784ecf67007d355ef299ae693a547faeee48974294b",
                }
            },
        },
    },
    "product": {
        1: {
            "segment_span": (354714, 515319, 354714, 515319, 5714, 8020),
            "segment_sha256": "45de526b916ed1c1003379c49fd957b5b03d220c960108eb8d5c486156df9494",
            "task_requests": ["freq"],
            "frames": {
                0: {
                    "file_frame_index": 9,
                    "frame_role": FrameRole.TERMINAL,
                    "span": (354714, 515319, 354714, 515319, 5714, 8020),
                    "sha256": "45de526b916ed1c1003379c49fd957b5b03d220c960108eb8d5c486156df9494",
                }
            },
        }
    },
}


@pytest.fixture(scope="module")
def parsed_da_path_frames(da_bench_log_paths: dict[str, Path]) -> dict[str, list[Any]]:
    molopconfig.show_progress_bar = False
    return {
        role: list(AutoParser(str(path), n_jobs=1)[0].frames)
        for role, path in da_bench_log_paths.items()
    }


def _span_values(span: tuple[int, int, int, int, int, int]) -> dict[str, int]:
    return dict(
        zip(
            (
                "source_start_byte",
                "source_end_byte",
                "source_start_char",
                "source_end_char",
                "source_start_line",
                "source_end_line",
            ),
            span,
            strict=True,
        )
    )


def test_real_da_subset_round_trips_manifest_reaction_path_and_frame_bindings(
    tmp_path: Path,
    da_bench_manifest: dict[str, Any],
    da_bench_log_paths: dict[str, Path],
    parsed_da_path_frames: dict[str, list[Any]],
) -> None:
    integration_manifest = deepcopy(da_bench_manifest)
    workflow = integration_manifest["workflow"]
    map_offset = 1_000 + uuid4().int % 1_000_000
    definition = rdChemReactions.ReactionFromSmarts(
        workflow["mapped_reaction_smiles"][0], useSmiles=True
    )
    assert definition is not None
    for templates in (definition.GetReactants(), definition.GetProducts()):
        for template in templates:
            for atom in template.GetAtoms():
                atom.SetAtomMapNum(atom.GetAtomMapNum() + map_offset)
    workflow["mapped_reaction_smiles"] = [rdChemReactions.ReactionToSmiles(definition, True)]
    for declaration in workflow["participants"]:
        declaration["source_atom_map_numbers"] = [
            atom_map + map_offset for atom_map in declaration["source_atom_map_numbers"]
        ]
    for node in workflow["nodes"]:
        for component in node["components"]:
            component["source_atom_map_numbers"] = [
                atom_map + map_offset for atom_map in component["source_atom_map_numbers"]
            ]
    workflow["manifest_key"] = f"integration-test:{workflow['manifest_key']}:{map_offset}"
    workflow["path_key"] = f"integration-test:{workflow['path_key']}:{map_offset}"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(integration_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    parsed_records = {
        role: [normalize_molop_frame(frame) for frame in frames]
        for role, frames in parsed_da_path_frames.items()
    }
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            manifest_artifact = persist_artifact_file(
                session,
                artifact_record_from_path(
                    manifest_path,
                    bucket="tricycle-da-test",
                    artifact_kind=ArtifactKind.WORKFLOW_MANIFEST,
                    storage_status=StorageStatus.AVAILABLE,
                ),
            )
            manifest_artifact_id = manifest_artifact.id
            manifest = persist_workflow_manifest(
                session,
                manifest_artifact,
                WorkflowManifestRecord(
                    manifest_key=workflow["manifest_key"],
                    revision=1,
                    schema_version=integration_manifest["schema_version"],
                    payload_sha256=manifest_artifact.content_sha256,
                    qc_policy_version="cycloaddition-qc-v1",
                    status=WorkflowManifestStatus.VALIDATED,
                    validation_metadata={"fixture": "da-bench-minimal"},
                ),
            )

            persisted_participants = {}
            for declaration in workflow["participants"]:
                role = declaration["log_role"]
                persisted_participants[role] = persist_molecular_geometry(
                    session,
                    parsed_records[role][-1],
                )

            calculation_artifacts = {}
            selected_frames = {}
            for log_role, segment_specs in SELECTED_CALCULATIONS.items():
                source_path = da_bench_log_paths[log_role]
                payload = source_path.read_bytes()
                artifact = persist_artifact_file(
                    session,
                    artifact_record_from_path(
                        source_path,
                        bucket="tricycle-da-test",
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        storage_status=StorageStatus.AVAILABLE,
                    ),
                )
                calculation_artifacts[log_role] = artifact
                revision = persist_parse_revision(
                    session,
                    artifact,
                    ParseRevisionRecord(
                        export_schema_version="molop-calculation-0.1.0",
                        parser_id="fixture.parser",
                        parser_version="0.2.1",
                        molop_version="0.2.1",
                        parser_commit="8d9573169478f795ce828a5cec75fd3a28bbc066",
                        rdkit_version="2025.09.6",
                        parser_provenance={
                            "parser_id": "fixture.parser",
                            "parser_version": "0.2.1",
                            "molop_version": "0.2.1",
                            "rdkit_version": "2025.09.6",
                            "effective_config": {},
                            "effective_config_sha256": "2" * 64,
                        },
                        parser_provenance_hash="1" * 64,
                        parser_config_hash="2" * 64,
                        reconstruction_config_hash="3" * 64,
                        source_format=SourceFormat.GAUSSIAN_LOG,
                        source_encoding="utf-8",
                        started_at=datetime.now(UTC),
                    ),
                )
                for segment_index, segment_spec in segment_specs.items():
                    protocol = persist_calculation_protocol(
                        session,
                        calculation_protocol_record(
                            qm_software=QMSoftware.GAUSSIAN,
                            qm_software_version="ES64L-G16",
                            method_family="DFT",
                            method="B3LYP",
                            functional="B3LYP-D3BJ",
                            basis_set="def2SVP",
                            task_requests=segment_spec["task_requests"],
                            normalized_spec={
                                "fixture": "da-bench-minimal",
                                "log_role": log_role,
                                "segment_index": segment_index,
                            },
                        ),
                    )
                    segment_span = _span_values(segment_spec["segment_span"])
                    segment_slice = payload[
                        segment_span["source_start_byte"] : segment_span["source_end_byte"]
                    ]
                    assert sha256(segment_slice).hexdigest() == segment_spec["segment_sha256"]
                    segment = persist_calculation_segment(
                        session,
                        revision,
                        protocol,
                        CalculationSegmentRecord(
                            segment_index=segment_index,
                            source_block_sha256=segment_spec["segment_sha256"],
                            termination_status=TerminationStatus.NORMAL,
                            scf_status=SCFStatus.CONVERGED,
                            **segment_span,
                        ),
                    )
                    for frame_index, frame_spec in segment_spec["frames"].items():
                        file_frame_index = frame_spec["file_frame_index"]
                        normalized_record = parsed_records[log_role][file_frame_index]
                        molop_frame = parsed_da_path_frames[log_role][file_frame_index]
                        frequency_count, negative_frequency_count, lowest_frequency = (
                            _frequency_summary(molop_frame)
                        )
                        persisted_molecule = persist_molecular_geometry(
                            session,
                            normalized_record,
                        )
                        geometry = persisted_molecule.geometry
                        frame_span = _span_values(frame_spec["span"])
                        frame_slice = payload[
                            frame_span["source_start_byte"] : frame_span["source_end_byte"]
                        ]
                        assert sha256(frame_slice).hexdigest() == frame_spec["sha256"]
                        frame = persist_calculation_frame(
                            session,
                            segment,
                            geometry,
                            persisted_molecule.topology_derivation,
                            CalculationFrameRecord(
                                frame_index=frame_index,
                                file_frame_index=file_frame_index,
                                frame_role=frame_spec["frame_role"],
                                source_block_sha256=frame_spec["sha256"],
                                charge=0,
                                multiplicity=1,
                                scf_status=SCFStatus.CONVERGED,
                                optimization_status=(
                                    OptimizationStatus.CONVERGED
                                    if frame_spec["frame_role"] is FrameRole.TERMINAL
                                    else OptimizationStatus.NOT_CONVERGED
                                ),
                                frequency_count=frequency_count,
                                negative_frequency_count=negative_frequency_count,
                                lowest_frequency_cm1=lowest_frequency,
                                **_exact_geometry_assignment(normalized_record),
                                **frame_span,
                            ),
                        )
                        thermochemistry = _thermochemistry_record(molop_frame)
                        if thermochemistry is not None:
                            persist_thermochemistry_result(
                                session,
                                frame,
                                thermochemistry,
                            )
                        selected_frames[(log_role, segment_index, frame_index)] = frame

            participant_identities = []
            participant_payloads = []
            for declaration in workflow["participants"]:
                persisted = persisted_participants[declaration["log_role"]]
                atom_maps = atom_maps_from_source_order(
                    persisted.geometry,
                    declaration["source_atom_map_numbers"],
                    persisted.observed_to_geometry_atom_indices,
                )
                side = LogicalReactionParticipantSide(declaration["side"])
                participant_identities.append((side, persisted.topology, 1))
                participant_payloads.append((declaration, persisted, side, atom_maps))

            reaction = persist_logical_reaction(
                session,
                LogicalReactionRecord(
                    reaction_key=workflow["reaction_key"],
                    label="DA bench 4+2 cycloaddition",
                    cycloaddition_pattern="4+2",
                    reaction_hash=reaction_hash_for_participants(participant_identities),
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
            canonical_mapped_smiles = rdChemReactions.ReactionToSmiles(definition, True)
            mapping_hash = sha256(canonical_mapped_smiles.encode("utf-8")).hexdigest()
            mapped_reaction = persist_mapped_reaction(
                session,
                reaction,
                MappedReactionRecord(
                    mapped_reaction_key=workflow["path_key"],
                    label="conf-00 to product-03",
                    mapped_reaction_kind=MappedReactionKind.CURATED,
                    mapped_reaction_smiles=canonical_mapped_smiles,
                    mapping_hash=mapping_hash,
                ),
                source_atom_maps_by_template={
                    (side, declaration["participant_index"]): atom_maps
                    for declaration, _persisted, side, atom_maps in participant_payloads
                },
                topology_ids_by_template={
                    (side, declaration["participant_index"]): persisted.topology.id
                    for declaration, persisted, side, _atom_maps in participant_payloads
                },
            )
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
                    selector = component["geometry_authority"]
                    calculation_frame = selected_frames[
                        (
                            component["log_role"],
                            selector["segment_index"],
                            selector["frame_index"],
                        )
                    ]
                    participant = None
                    if "participant_side" in component:
                        logical_participant = reaction_participants[
                            (component["participant_side"], component["participant_index"])
                        ]
                        assert logical_participant.id is not None
                        participant = mapped_participants[logical_participant.id]
                    node_geometry = persist_mapped_reaction_node_geometry(
                        session,
                        node,
                        calculation_frame.geometry,
                        MappedReactionNodeGeometryRecord(
                            component_key=component["component_key"],
                            component_index=component["component_index"],
                            coordinate_index=0,
                            is_primary=True,
                        ),
                        mapped_reaction_participant=participant,
                    )
                    topology_maps = atom_maps_from_source_order(
                        calculation_frame.geometry,
                        component["source_atom_map_numbers"],
                        calculation_frame.observed_to_geometry_atom_indices,
                    )
                    persist_mapped_reaction_node_geometry_mapping(
                        session,
                        node_geometry,
                        MappedReactionNodeGeometryMappingRecord(
                            geometry_atom_map_numbers=topology_maps,
                            mapped_smiles=mapped_smiles_for_topology(
                                calculation_frame.geometry.topology,
                                topology_maps,
                            ),
                            mapping_method="manifest-explicit",
                            mapping_version="coordinate-map-v1",
                            verified=True,
                        ),
                    )
                    artifact = calculation_artifacts[component["log_role"]]
                    persist_manifest_artifact_binding(
                        session,
                        manifest,
                        ManifestArtifactBindingRecord(
                            artifact_key=(
                                f"{declaration['node_key']}:"
                                f"{component['component_key']}:geometry_authority"
                            ),
                            expected_content_sha256=artifact.content_sha256,
                            artifact_role=ManifestArtifactRole.GAUSSIAN_OPT_FREQ,
                            reaction_key=workflow["reaction_key"],
                            path_key=workflow["path_key"],
                            node_key=declaration["node_key"],
                            segment_index=selector["segment_index"],
                            frame_index=selector["frame_index"],
                            resolution_status=ArtifactResolutionStatus.RESOLVED,
                        ),
                        artifact_file=artifact,
                    )

            edge_declaration = workflow["edge"]
            edge_record = MappedReactionEdgeRecord(
                edge_key=edge_declaration["edge_key"],
                edge_kind=MappedReactionEdgeKind(edge_declaration["edge_kind"]),
            )
            edge = persist_mapped_reaction_edge(
                session,
                mapped_reaction,
                nodes[edge_declaration["source_node_key"]],
                nodes[edge_declaration["target_node_key"]],
                edge_record,
                transition_state_node=nodes[edge_declaration["transition_state_node_key"]],
            )
            assert (
                persist_mapped_reaction_edge(
                    session,
                    mapped_reaction,
                    nodes[edge_declaration["source_node_key"]],
                    nodes[edge_declaration["target_node_key"]],
                    edge_record,
                    transition_state_node=nodes[edge_declaration["transition_state_node_key"]],
                ).id
                == edge.id
            )
            session.commit()
            manifest_id = manifest.id
            reaction_id = reaction.id
            assert reaction_id is not None

        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            loaded_manifest = session.exec(
                select(WorkflowManifest).where(WorkflowManifest.id == manifest_id)
            ).one()
            loaded_reaction = session.exec(
                select(LogicalReaction).where(LogicalReaction.id == reaction_id)
            ).one()
            loaded_path = next(
                path
                for path in loaded_reaction.mapped_reactions
                if path.mapped_reaction_key == workflow["path_key"]
            )
            loaded_edge = loaded_path.edges[0]

            assert loaded_manifest.artifact_file.id == manifest_artifact_id
            assert len(loaded_manifest.artifact_bindings) == 4
            assert len(loaded_reaction.participants) == 3
            assert len(loaded_path.participants) == 3
            assert [
                node.node_key for node in sorted(loaded_path.nodes, key=lambda row: row.node_index)
            ] == [
                "reactants",
                "transition-state",
                "product",
            ]
            assert loaded_edge.source_node.node_key == "reactants"
            assert loaded_edge.target_node.node_key == "product"
            assert loaded_edge.transition_state_node is not None
            assert loaded_edge.transition_state_node.node_key == "transition-state"
            geometry_bindings = [
                binding for node in loaded_path.nodes for binding in node.geometry_bindings
            ]
            primary_bindings = [binding for binding in geometry_bindings if binding.is_primary]
            assert len(geometry_bindings) >= 4
            assert len(primary_bindings) == 4
            assert all(
                frame.geometry_id == geometry_binding.geometry_id
                for node in loaded_path.nodes
                for geometry_binding in node.geometry_bindings
                for frame in geometry_binding.geometry.calculation_frames
            )
            session.delete(loaded_manifest)
            session.flush()
            assert (
                session.exec(
                    select(LogicalReaction.id).where(LogicalReaction.id == reaction_id)
                ).first()
                == reaction_id
            )
            assert session.get(ArtifactFile, manifest_artifact_id) is not None
    finally:
        transaction.rollback()
        connection.close()
        database_engine.dispose()


def test_manifest_revision_and_orca_source_relationships_round_trip(tmp_path: Path) -> None:
    paths = {
        "manifest_v1": tmp_path / "manifest-v1.json",
        "manifest_v2": tmp_path / "manifest-v2.json",
        "gaussian": tmp_path / "authority.log",
        "orca": tmp_path / "single-point.out",
    }
    for name, path in paths.items():
        path.write_text(f"{name}\n")

    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            manifests = []
            for revision in (1, 2):
                artifact = persist_artifact_file(
                    session,
                    artifact_record_from_path(
                        paths[f"manifest_v{revision}"],
                        bucket="tricycle-manifest-revision-test",
                        artifact_kind=ArtifactKind.WORKFLOW_MANIFEST,
                        storage_status=StorageStatus.AVAILABLE,
                    ),
                )
                manifests.append(
                    persist_workflow_manifest(
                        session,
                        artifact,
                        WorkflowManifestRecord(
                            manifest_key="revision-series",
                            revision=revision,
                            schema_version="manifest-v1",
                            payload_sha256=artifact.content_sha256,
                            qc_policy_version="qc-v1",
                            status=WorkflowManifestStatus.VALIDATED,
                        ),
                        supersedes=manifests[-1] if manifests else None,
                    )
                )

            calculation_artifacts = {}
            for role in ("gaussian", "orca"):
                calculation_artifacts[role] = persist_artifact_file(
                    session,
                    artifact_record_from_path(
                        paths[role],
                        bucket="tricycle-manifest-revision-test",
                        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                        storage_status=StorageStatus.AVAILABLE,
                    ),
                )

            gaussian_binding = persist_manifest_artifact_binding(
                session,
                manifests[1],
                ManifestArtifactBindingRecord(
                    artifact_key="gaussian-authority",
                    expected_content_sha256=calculation_artifacts["gaussian"].content_sha256,
                    artifact_role=ManifestArtifactRole.GAUSSIAN_OPT_FREQ,
                    reaction_key="reaction",
                    path_key="path",
                    node_key="ts",
                    segment_index=0,
                    frame_index=7,
                    resolution_status=ArtifactResolutionStatus.RESOLVED,
                ),
                artifact_file=calculation_artifacts["gaussian"],
            )
            orca_binding = persist_manifest_artifact_binding(
                session,
                manifests[1],
                ManifestArtifactBindingRecord(
                    artifact_key="orca-single-point",
                    expected_content_sha256=calculation_artifacts["orca"].content_sha256,
                    artifact_role=ManifestArtifactRole.ORCA_SINGLE_POINT,
                    reaction_key="reaction",
                    path_key="path",
                    node_key="ts",
                    segment_index=0,
                    frame_index=0,
                    source_geometry_artifact_key="gaussian-authority",
                    resolution_status=ArtifactResolutionStatus.RESOLVED,
                ),
                artifact_file=calculation_artifacts["orca"],
                source_geometry_binding=gaussian_binding,
            )

            assert manifests[1].manifest_key == "revision-series"
            assert manifests[1].supersedes is manifests[0]
            assert orca_binding.workflow_manifest_id == manifests[1].id
            assert orca_binding.source_geometry_binding is gaussian_binding
            assert gaussian_binding.dependent_bindings == [orca_binding]

            session.delete(manifests[1])
            session.flush()
            assert session.get(WorkflowManifest, manifests[0].id) is not None
    finally:
        transaction.rollback()
        connection.close()
        database_engine.dispose()
