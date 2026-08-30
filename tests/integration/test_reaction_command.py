import os
from datetime import UTC, datetime
from hashlib import sha256

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine, delete, func
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos import (
    CalculationFrameRecord,
    CalculationSegmentRecord,
    CreateReactionCommand,
    LogicalReactionParticipantRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
    ParseRevisionRecord,
    ThermochemistryResultRecord,
)
from tricycle_reaction_db.application.services import (
    persist_artifact_file,
    persist_calculation_frame,
    persist_calculation_segment,
    persist_logical_reaction_participant,
    persist_mapped_reaction_node,
    persist_mapped_reaction_node_geometry,
    persist_mapped_reaction_node_geometry_mapping,
    persist_molecular_geometry,
    persist_parse_revision,
    persist_thermochemistry_result,
)
from tricycle_reaction_db.application.services import reactions as reactions_module
from tricycle_reaction_db.application.services.reaction_commands import _create_reaction
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    Geometry,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.dev.reconcile_reaction_geometries import (
    reconcile_reaction_geometry_batch,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    FrameRole,
    GeometryAssignmentKind,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    MappedReactionKind,
    MappedReactionNodeRole,
    OptimizationStatus,
    SourceFormat,
    StorageStatus,
)
from tricycle_reaction_db.ingestion import artifact_record_from_path, normalize_molecule

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def test_unmapped_reaction_creates_graph_only_topologies_idempotently() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            command = CreateReactionCommand(reaction="[13CH2]1CC1>>[13CH2]=CC")
            first = _create_reaction(session, command)
            second = _create_reaction(session, command)

            assert first.mapping_complete is False
            assert first.mapped_reaction_id is None
            assert first.topologies_created == 2
            assert second.logical_reaction_id == first.logical_reaction_id
            assert second.topologies_created == 0
            logical = session.get(LogicalReaction, first.logical_reaction_id)
            assert logical is not None
            assert logical.label == "C2[13C]H6 -> C2[13C]H6 [" + logical.reaction_hash[:8] + "]"
            topologies = session.exec(
                select(MolecularTopology).where(col(MolecularTopology.id).in_(first.topology_ids))
            ).all()
            assert len(topologies) == 2
            assert all(not topology.geometries for topology in topologies)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_curated_role_completes_an_automatic_participant_without_overwriting_it() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            result = _create_reaction(session, CreateReactionCommand(reaction="C=C>>C=C"))
            participant = session.exec(
                select(LogicalReactionParticipant).where(
                    LogicalReactionParticipant.logical_reaction_id == result.logical_reaction_id,
                    LogicalReactionParticipant.side == LogicalReactionParticipantSide.REACTANT,
                    LogicalReactionParticipant.participant_index == 0,
                )
            ).first()
            assert participant is not None
            assert participant.role is None
            record = LogicalReactionParticipantRecord(
                side=participant.side,
                participant_index=participant.participant_index,
                role=LogicalReactionParticipantRole.DIENOPHILE,
            )

            completed = persist_logical_reaction_participant(
                session,
                participant.logical_reaction,
                participant.topology,
                record,
            )
            repeated = persist_logical_reaction_participant(
                session,
                participant.logical_reaction,
                participant.topology,
                record,
            )

            assert completed.id == repeated.id == participant.id
            assert completed.role is LogicalReactionParticipantRole.DIENOPHILE
            with pytest.raises(ValueError, match="different role"):
                persist_logical_reaction_participant(
                    session,
                    participant.logical_reaction,
                    participant.topology,
                    record.model_copy(update={"role": LogicalReactionParticipantRole.DIENE}),
                )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_fully_mapped_reaction_does_not_bind_topology_without_converged_frames() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            molecule = Chem.MolFromSmiles("[H][H]")
            normalized = normalize_molecule(
                molecule,
                np.asarray([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64),
                charge=0,
                multiplicity=1,
                reconstruction_method="test",
                reconstruction_version="1",
            )
            persisted = persist_molecular_geometry(session, normalized)
            geometry_count = session.exec(select(func.count()).select_from(Geometry)).one()

            first = _create_reaction(
                session,
                CreateReactionCommand(
                    reaction="[H:1][H:2]>>[H:1][H:2]",
                    mapped_reaction_key="curated-h2",
                    label="first declaration",
                ),
            )
            second = _create_reaction(
                session,
                CreateReactionCommand(
                    reaction="[H:1][H:2]>>[H:1][H:2]",
                    mapped_reaction_key="automatic-h2",
                    label="later declaration",
                ),
            )

            assert first.mapping_complete is True
            assert first.mapped_reaction_id is not None
            assert first.reactant_node_id is not None
            assert first.product_node_id is not None
            assert first.topologies_created == 0
            assert first.topology_ids == [persisted.topology.id, persisted.topology.id]
            assert second.logical_reaction_id == first.logical_reaction_id
            assert second.mapped_reaction_id == first.mapped_reaction_id
            assert second.mapped_reaction_created is False
            assert session.exec(select(func.count()).select_from(Geometry)).one() == geometry_count
            node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == first.mapped_reaction_id)
            ).all()
            assert node_geometries == []
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_unoptimized_geometry_does_not_link_to_preexisting_reaction_endpoint() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            reaction = _create_reaction(
                session,
                CreateReactionCommand(reaction="[H:1][H:2]>>[H:1][H:2]"),
            )
            assert reaction.mapped_reaction_id is not None
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReactionNodeGeometry)
                    .join(MappedReactionNode)
                    .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
                ).one()
                == 0
            )

            normalized = normalize_molecule(
                Chem.MolFromSmiles("[H][H]"),
                np.asarray([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64),
                charge=0,
                multiplicity=1,
                reconstruction_method="test",
                reconstruction_version="1",
            )
            persisted = persist_molecular_geometry(session, normalized)
            duplicate = persist_molecular_geometry(session, normalized)
            bindings = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
            ).all()

            assert duplicate.geometry.id == persisted.geometry.id
            assert bindings == []
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_node_geometry_identity_uses_geometry_not_display_coordinate_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            first = persist_molecular_geometry(
                session,
                normalize_molecule(
                    Chem.MolFromSmiles("[H][H]"),
                    np.asarray([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64),
                    charge=0,
                    multiplicity=1,
                    reconstruction_method="test",
                    reconstruction_version="1",
                ),
            )
            second = persist_molecular_geometry(
                session,
                normalize_molecule(
                    Chem.MolFromSmiles("[H][H]"),
                    np.asarray([[0.0, 0.0, 0.0], [0.80, 0.0, 0.0]], dtype=np.float64),
                    charge=0,
                    multiplicity=1,
                    reconstruction_method="test",
                    reconstruction_version="1",
                ),
            )
            assert first.geometry.id != second.geometry.id

            created = _create_reaction(
                session,
                CreateReactionCommand(reaction="[H:1][H:2]>>[H:1][H:2]"),
            )
            assert created.mapped_reaction_id is not None
            mapped_reaction = session.get(MappedReaction, created.mapped_reaction_id)
            assert mapped_reaction is not None
            node = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapped_reaction.id,
                    MappedReactionNode.role == MappedReactionNodeRole.REACTANT,
                )
            ).one()
            participant = session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == mapped_reaction.id,
                    MappedReactionParticipant.side == LogicalReactionParticipantSide.REACTANT,
                    MappedReactionParticipant.template_index == 0,
                )
            ).one()
            with pytest.raises(
                ValueError,
                match="requires at least one thermodynamic property",
            ):
                persist_mapped_reaction_node_geometry(
                    session,
                    node,
                    first.geometry,
                    MappedReactionNodeGeometryRecord(
                        component_key="reactant:0",
                        component_index=0,
                        coordinate_index=0,
                        is_primary=False,
                    ),
                    mapped_reaction_participant=participant,
                )
            monkeypatch.setattr(
                reactions_module,
                "require_geometry_thermodynamic_property",
                lambda _session, _geometry: None,
            )
            for coordinate_index, geometry in enumerate((first.geometry, second.geometry)):
                binding = persist_mapped_reaction_node_geometry(
                    session,
                    node,
                    geometry,
                    MappedReactionNodeGeometryRecord(
                        component_key="reactant:0",
                        component_index=0,
                        coordinate_index=coordinate_index,
                        is_primary=False,
                    ),
                    mapped_reaction_participant=participant,
                )
                persist_mapped_reaction_node_geometry_mapping(
                    session,
                    binding,
                    MappedReactionNodeGeometryMappingRecord(
                        geometry_atom_map_numbers=[1, 2],
                        mapped_smiles="[H:1][H:2]",
                        mapping_method="test",
                        mapping_version="test-v1",
                        verified=True,
                    ),
                )

            bindings = session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.mapped_reaction_node_id == node.id,
                    MappedReactionNodeGeometry.mapped_reaction_participant_id == participant.id,
                )
            ).all()
            assert len(bindings) == 2
            assert not any(binding.is_primary for binding in bindings)
            primary_slot_binding = next(
                binding for binding in bindings if binding.coordinate_index == 0
            )
            curated = next(binding for binding in bindings if binding.id != primary_slot_binding.id)
            assert curated.geometry_id in {first.geometry.id, second.geometry.id}

            reused = persist_mapped_reaction_node_geometry(
                session,
                node,
                curated.geometry,
                MappedReactionNodeGeometryRecord(
                    component_key=curated.component_key,
                    component_index=curated.component_index,
                    # This slot is currently assigned to another conformer.
                    coordinate_index=primary_slot_binding.coordinate_index,
                    is_primary=True,
                ),
                mapped_reaction_participant=participant,
            )
            repeated = persist_mapped_reaction_node_geometry(
                session,
                node,
                curated.geometry,
                MappedReactionNodeGeometryRecord(
                    component_key=curated.component_key,
                    component_index=curated.component_index,
                    coordinate_index=99,
                    is_primary=True,
                ),
                mapped_reaction_participant=participant,
            )
            assert reused.id == repeated.id == curated.id

            refreshed = session.exec(
                select(MappedReactionNodeGeometry).where(
                    MappedReactionNodeGeometry.mapped_reaction_node_id == node.id,
                    MappedReactionNodeGeometry.mapped_reaction_participant_id == participant.id,
                )
            ).all()
            assert len(refreshed) == 2
            assert sum(binding.is_primary for binding in refreshed) == 1
            assert next(binding for binding in refreshed if binding.is_primary).id == curated.id
            assert {binding.coordinate_index for binding in refreshed} == {
                binding.coordinate_index for binding in bindings
            }

            existing_mapping = curated.mapping_bindings[0]
            reused_mapping = persist_mapped_reaction_node_geometry_mapping(
                session,
                curated,
                MappedReactionNodeGeometryMappingRecord(
                    geometry_atom_map_numbers=existing_mapping.geometry_atom_map_numbers,
                    mapped_smiles=existing_mapping.mapped_smiles,
                    mapping_method="different-source-order",
                    mapping_version="test-v1",
                    verified=True,
                ),
            )
            assert reused_mapping.id == existing_mapping.id
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_existing_node_key_can_adopt_curated_path_order() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            created = _create_reaction(
                session,
                CreateReactionCommand(reaction="[H:1][H:2]>>[H:1][H:2]"),
            )
            assert created.mapped_reaction_id is not None
            mapped_reaction = session.get(MappedReaction, created.mapped_reaction_id)
            assert mapped_reaction is not None
            product = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == mapped_reaction.id,
                    MappedReactionNode.node_key == "products",
                )
            ).one()
            transition_state = persist_mapped_reaction_node(
                session,
                mapped_reaction,
                MappedReactionNodeRecord(
                    node_key="transition-state",
                    node_index=1,
                    role=MappedReactionNodeRole.TRANSITION_STATE,
                ),
            )
            product_id = product.id
            transition_state_id = transition_state.id

            repeated_transition_state = persist_mapped_reaction_node(
                session,
                mapped_reaction,
                MappedReactionNodeRecord(
                    node_key="transition-state",
                    node_index=1,
                    role=MappedReactionNodeRole.TRANSITION_STATE,
                ),
            )
            reordered_product = persist_mapped_reaction_node(
                session,
                mapped_reaction,
                MappedReactionNodeRecord(
                    node_key="product",
                    node_index=2,
                    role=MappedReactionNodeRole.PRODUCT,
                ),
            )

            assert repeated_transition_state.id == transition_state_id
            assert reordered_product.id == product_id
            assert repeated_transition_state.node_index == 1
            assert reordered_product.node_index == 2
            assert reordered_product.node_key == "products"
            assert [
                node.node_key
                for node in session.exec(
                    select(MappedReactionNode)
                    .where(MappedReactionNode.mapped_reaction_id == mapped_reaction.id)
                    .order_by(col(MappedReactionNode.node_index))
                ).all()
            ] == ["reactants", "transition-state", "products"]
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_partially_mapped_reaction_is_rejected() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with (
            Session(bind=connection, join_transaction_mode="create_savepoint") as session,
            pytest.raises(ValueError, match="either absent or complete"),
        ):
            _create_reaction(
                session,
                CreateReactionCommand(reaction="[H:1][H]>>[H:1][H]"),
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_creation_method_does_not_change_mapped_reaction_identity() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            inferred_command = CreateReactionCommand(
                reaction="[H:1][H:2]>>[H:1][H:2]",
                mapped_reaction_kind=MappedReactionKind.OTHER,
            )
            curated_command = CreateReactionCommand(
                reaction="[H:1][H:2]>>[H:1][H:2]",
                mapped_reaction_kind=MappedReactionKind.CURATED,
            )
            first = _create_reaction(session, inferred_command)
            second = _create_reaction(session, curated_command)

            assert first.mapped_reaction_id is not None
            assert second.logical_reaction_id == first.logical_reaction_id
            assert second.mapped_reaction_id == first.mapped_reaction_id
            assert second.logical_reaction_created is False
            assert second.mapped_reaction_created is False
            mapped = session.get(MappedReaction, first.mapped_reaction_id)
            assert mapped is not None
            assert mapped.mapped_reaction_kind is MappedReactionKind.OTHER
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_logical_reaction_removes_mapping_and_keeps_distinct_mappings() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            identity_mapping = "[O:1]=[C:2]=[C:3]=[S:4]>>[O:1]=[C:2]=[C:3]=[S:4]"
            crossed_mapping = "[O:1]=[C:2]=[C:3]=[S:4]>>[O:1]=[C:3]=[C:2]=[S:4]"
            renumbered_mapping = "[O:11]=[C:12]=[C:13]=[S:14]>>[O:11]=[C:12]=[C:13]=[S:14]"

            first = _create_reaction(session, CreateReactionCommand(reaction=identity_mapping))
            second = _create_reaction(session, CreateReactionCommand(reaction=crossed_mapping))
            renumbered = _create_reaction(
                session,
                CreateReactionCommand(reaction=renumbered_mapping),
            )
            repeated = _create_reaction(session, CreateReactionCommand(reaction=crossed_mapping))

            assert {
                first.logical_reaction_id,
                second.logical_reaction_id,
                renumbered.logical_reaction_id,
            } == {first.logical_reaction_id}
            assert first.mapped_reaction_id is not None
            assert second.mapped_reaction_id is not None
            assert renumbered.mapped_reaction_id is not None
            assert (
                len(
                    {
                        first.mapped_reaction_id,
                        second.mapped_reaction_id,
                        renumbered.mapped_reaction_id,
                    }
                )
                == 3
            )
            assert repeated.mapped_reaction_id == second.mapped_reaction_id
            assert repeated.mapped_reaction_created is False
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReaction)
                    .where(MappedReaction.logical_reaction_id == first.logical_reaction_id)
                ).one()
                == 3
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_later_converged_frame_links_to_preexisting_reaction(tmp_path) -> None:
    source_path = tmp_path / "later-converged.log"
    source_payload = b"terminal optimized frame\ncoordinates\n"
    source_path.write_bytes(source_payload)
    source_hash = sha256(source_payload).hexdigest()
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            reaction = _create_reaction(
                session,
                CreateReactionCommand(reaction="[2H:1][3H:2]>>[2H:1][3H:2]"),
            )
            assert reaction.mapped_reaction_id is not None
            assert (
                session.exec(
                    select(func.count())
                    .select_from(MappedReactionNodeGeometry)
                    .join(MappedReactionNode)
                    .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
                ).one()
                == 0
            )

            normalized = normalize_molecule(
                Chem.MolFromSmiles("[2H][3H]"),
                np.asarray([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64),
                charge=0,
                multiplicity=1,
                reconstruction_method="test",
                reconstruction_version="1",
            )
            persisted = persist_molecular_geometry(session, normalized)
            node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
            ).all()
            assert node_geometries == []

            artifact = persist_artifact_file(
                session,
                artifact_record_from_path(
                    source_path,
                    bucket="reaction-reconciliation-test",
                    artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
                    storage_status=StorageStatus.AVAILABLE,
                ),
            )
            revision = persist_parse_revision(
                session,
                artifact,
                ParseRevisionRecord(
                    export_schema_version="reaction-reconciliation-v1",
                    parser_id="fixture.parser",
                    parser_version="v1",
                    molop_version="v1",
                    rdkit_version="2025.09.6",
                    parser_provenance={
                        "parser_id": "fixture.parser",
                        "parser_version": "v1",
                        "molop_version": "v1",
                        "molgr_version": None,
                        "rdkit_version": "2025.09.6",
                        "effective_config_sha256": "2" * 64,
                    },
                    parser_provenance_hash="1" * 64,
                    parser_config_hash="2" * 64,
                    reconstruction_config_hash="3" * 64,
                    source_format=SourceFormat.GAUSSIAN_LOG,
                    source_encoding="ascii",
                    started_at=datetime.now(UTC),
                ),
            )
            segment = persist_calculation_segment(
                session,
                revision,
                None,
                CalculationSegmentRecord(
                    segment_index=0,
                    source_start_byte=0,
                    source_end_byte=len(source_payload),
                    source_start_line=1,
                    source_end_line=3,
                    source_block_sha256=source_hash,
                ),
            )
            frame = persist_calculation_frame(
                session,
                segment,
                persisted.geometry,
                persisted.topology_derivation,
                CalculationFrameRecord(
                    frame_index=0,
                    file_frame_index=0,
                    frame_role=FrameRole.INTERMEDIATE,
                    optimization_status=OptimizationStatus.NOT_CONVERGED,
                    source_start_byte=0,
                    source_end_byte=len(source_payload),
                    source_start_line=1,
                    source_end_line=3,
                    source_block_sha256=source_hash,
                    charge=0,
                    multiplicity=1,
                    geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
                    observed_coordinates=normalized.observed_coordinates,
                    observed_coordinate_hash=normalized.observed_coordinate_hash,
                    observed_to_geometry_atom_indices=(
                        normalized.observed_to_geometry_atom_indices
                    ),
                    observed_to_geometry_transform=normalized.observed_to_geometry_transform,
                    geometry_assignment_rmsd_angstrom=(
                        normalized.geometry_assignment_rmsd_angstrom
                    ),
                    geometry_assignment_max_abs_angstrom=(
                        normalized.geometry_assignment_max_abs_angstrom
                    ),
                    geometry_assignment_policy_version=("geometry-internal-coordinate-match-v3"),
                ),
            )
            assert frame.geometry_id == persisted.geometry.id
            node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
            ).all()
            assert node_geometries == []

            converged_frame = persist_calculation_frame(
                session,
                segment,
                persisted.geometry,
                persisted.topology_derivation,
                CalculationFrameRecord(
                    frame_index=1,
                    file_frame_index=1,
                    frame_role=FrameRole.TERMINAL,
                    optimization_status=OptimizationStatus.CONVERGED,
                    source_start_byte=0,
                    source_end_byte=len(source_payload),
                    source_start_line=1,
                    source_end_line=3,
                    source_block_sha256=source_hash,
                    charge=0,
                    multiplicity=1,
                    geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
                    observed_coordinates=normalized.observed_coordinates,
                    observed_coordinate_hash=normalized.observed_coordinate_hash,
                    observed_to_geometry_atom_indices=(
                        normalized.observed_to_geometry_atom_indices
                    ),
                    observed_to_geometry_transform=normalized.observed_to_geometry_transform,
                    geometry_assignment_rmsd_angstrom=(
                        normalized.geometry_assignment_rmsd_angstrom
                    ),
                    geometry_assignment_max_abs_angstrom=(
                        normalized.geometry_assignment_max_abs_angstrom
                    ),
                    geometry_assignment_policy_version=("geometry-internal-coordinate-match-v3"),
                    frequency_count=1,
                    negative_frequency_count=0,
                    lowest_frequency_cm1=100.0,
                ),
            )
            assert converged_frame.geometry_id == persisted.geometry.id
            node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
            ).all()
            assert node_geometries == []

            persist_thermochemistry_result(
                session,
                converged_frame,
                ThermochemistryResultRecord(
                    temperature_kelvin=298.15,
                    pressure_atm=1.0,
                    gibbs_free_energy_hartree=-1.0,
                    source_schema_version=revision.export_schema_version,
                ),
            )
            node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == reaction.mapped_reaction_id)
            ).all()
            assert len(node_geometries) == 2
            assert {binding.geometry_id for binding in node_geometries} == {persisted.geometry.id}

            backfilled = _create_reaction(
                session,
                CreateReactionCommand(
                    reaction="[2H:11][3H:12]>>[2H:11][3H:12]",
                    mapped_reaction_key="later-converged-backfill",
                ),
            )
            assert backfilled.mapped_reaction_id is not None
            assert backfilled.mapped_reaction_id != reaction.mapped_reaction_id
            backfilled_node_geometries = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(MappedReactionNode.mapped_reaction_id == backfilled.mapped_reaction_id)
            ).all()
            assert len(backfilled_node_geometries) == 2
            assert {binding.geometry_id for binding in backfilled_node_geometries} == {
                persisted.geometry.id
            }

            binding_ids = {
                binding.id for binding in backfilled_node_geometries if binding.id is not None
            }
            session.exec(
                delete(MappedReactionNodeGeometry).where(
                    col(MappedReactionNodeGeometry.id).in_(binding_ids)
                )
            )
            session.flush()

            repaired = reconcile_reaction_geometry_batch(
                session,
                batch_size=1,
                mapped_reaction_id=backfilled.mapped_reaction_id,
            )
            assert repaired.scanned_reactions == 1
            assert repaired.reconciled_reactions == 1
            assert repaired.created_bindings == 2
            assert repaired.failed_reactions == 0

            repeated = reconcile_reaction_geometry_batch(
                session,
                batch_size=1,
                mapped_reaction_id=backfilled.mapped_reaction_id,
            )
            assert repeated.scanned_reactions == 1
            assert repeated.reconciled_reactions == 1
            assert repeated.matched_bindings == 2
            assert repeated.created_bindings == 0
            assert repeated.failed_reactions == 0
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
