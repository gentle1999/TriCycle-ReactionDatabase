"""Integration coverage for abstract logical participants and strict mappings."""

import os
from hashlib import sha256

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.reactions import (
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionRecord,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_geometry,
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    _logicalize_components,
    _ResolvedComponent,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ensure_transition_state_path,
    persist_mapped_reaction_node_geometry,
    resolve_endpoint_node,
)
from tricycle_reaction_db.application.services.reaction_mapping_resolution import (
    ensure_mapped_reactions_for_concrete_topology,
    ensure_mapped_reactions_for_logical_reaction,
)
from tricycle_reaction_db.application.services.reactions import (
    mapped_smiles_for_topology,
    persist_logical_reaction,
    persist_logical_reaction_participant,
    persist_mapped_reaction,
    persist_mapped_reaction_node_geometry_mapping,
    reaction_hash_for_participants,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    assigned_stereo_features,
    persist_stereo_abstraction_projection,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MolecularTopology,
    MolecularTopologyAbstraction,
)
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionKind,
    MappedReactionNodeRole,
)
from tricycle_reaction_db.ingestion.normalization import normalize_molecule, normalize_topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _strict_stereo_topology(
    session: Session,
    smiles: str,
    *,
    method: str,
) -> MolecularTopology:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    return persist_molecular_topology(
        session,
        normalize_topology(
            molecule,
            add_hydrogens=False,
            reconstruction_method=method,
            reconstruction_version="1",
        ),
    ).topology


def _mapped_reaction_fixture(
    session: Session,
) -> tuple[MolecularTopology, MolecularTopology, MolecularTopology, MappedReaction]:
    source = _strict_stereo_topology(
        session,
        "F[C@H](Cl)[C@H](Br)I",
        method="tests/concrete-source",
    )
    target = _strict_stereo_topology(
        session,
        "F[C@@H](Cl)[C@H](Br)I",
        method="tests/concrete-target",
    )
    logical, _edge = persist_stereo_abstraction_projection(
        session,
        source,
        assigned_stereo_features(source.mol),
    )
    session.flush()

    reaction_hash = reaction_hash_for_participants(
        (
            (LogicalReactionParticipantSide.REACTANT, logical, 1),
            (LogicalReactionParticipantSide.PRODUCT, logical, 1),
        )
    )
    reaction = persist_logical_reaction(
        session,
        LogicalReactionRecord(
            reaction_key=f"tests/reaction:{reaction_hash}",
            label="concrete mapping test",
            reaction_hash=reaction_hash,
        ),
    )
    for side in LogicalReactionParticipantSide:
        persist_logical_reaction_participant(
            session,
            reaction,
            logical,
            LogicalReactionParticipantRecord(side=side, participant_index=0),
            candidate_topologies=(source,),
        )

    atom_maps = [index + 1 for index in range(source.atom_count)]
    mapped_smiles = mapped_smiles_for_topology(source, atom_maps)
    mapped_reaction_smiles = f"{mapped_smiles}>>{mapped_smiles}"
    mapping_hash = sha256(mapped_reaction_smiles.encode("utf-8")).hexdigest()
    mapped_reaction = persist_mapped_reaction(
        session,
        reaction,
        MappedReactionRecord(
            mapped_reaction_key=f"mapping:{mapping_hash}",
            label="concrete mapping test",
            mapped_reaction_kind=MappedReactionKind.OTHER,
            mapped_reaction_smiles=mapped_reaction_smiles,
            mapping_hash=mapping_hash,
        ),
        source_atom_maps_by_template={
            (side, 0): atom_maps for side in LogicalReactionParticipantSide
        },
        topology_ids_by_template={(side, 0): logical.id for side in LogicalReactionParticipantSide},
        concrete_topology_ids_by_template={
            (side, 0): source.id for side in LogicalReactionParticipantSide
        },
        precomputed_mapped_smiles_by_template={
            (side, 0): mapped_smiles for side in LogicalReactionParticipantSide
        },
    )
    session.flush()
    return source, target, logical, mapped_reaction


def test_new_concrete_topology_gets_mapping_via_logical_graph() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            _source, target, logical_topology, source_mapping = _mapped_reaction_fixture(session)

            created = ensure_mapped_reactions_for_concrete_topology(
                session,
                target,
                refresh_thermodynamics=False,
            )
            assert len(created) == 3
            target_mapping = next(
                mapping
                for mapping in created
                if all(
                    participant.concrete_topology_id == target.id
                    for participant in session.exec(
                        select(MappedReactionParticipant).where(
                            MappedReactionParticipant.mapped_reaction_id == mapping.id
                        )
                    ).all()
                )
            )
            assert target_mapping.id != source_mapping.id
            assert target_mapping.mapping_hash != source_mapping.mapping_hash
            assert target_mapping.mapped_reaction_smiles.count("[C@@H:") == 2

            participants = session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == target_mapping.id
                )
            ).all()
            assert len(participants) == 2
            assert all(
                participant.concrete_topology_id == target.id for participant in participants
            )
            assert all(
                participant.logical_reaction_participant.topology_id == logical_topology.id
                for participant in participants
            )

            memberships = session.exec(
                select(LogicalReactionParticipant).where(
                    LogicalReactionParticipant.logical_reaction_id
                    == source_mapping.logical_reaction_id
                )
            ).all()
            assert len(memberships) == 2
            assert all(
                any(
                    membership.concrete_topology_id == target.id
                    for membership in participant.concrete_topology_memberships
                )
                for participant in memberships
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_logical_reaction_expands_existing_concrete_members() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            source, target, logical_topology, source_mapping = _mapped_reaction_fixture(session)
            target_edge = session.exec(
                select(MolecularTopologyAbstraction).where(
                    MolecularTopologyAbstraction.specific_topology_id == target.id,
                    MolecularTopologyAbstraction.general_topology_id == logical_topology.id,
                )
            ).first()
            assert target_edge is not None

            created = ensure_mapped_reactions_for_logical_reaction(
                session,
                source_mapping.logical_reaction,
                refresh_thermodynamics=False,
            )
            mappings = session.exec(
                select(MappedReaction).where(
                    MappedReaction.logical_reaction_id == source_mapping.logical_reaction_id
                )
            ).all()

            assert len(created) == 3
            assert len(mappings) == 4
            assert len({mapping.mapping_hash for mapping in mappings}) == 4
            assert any(
                target.id
                in {
                    participant.concrete_topology_id
                    for participant in session.exec(
                        select(MappedReactionParticipant).where(
                            MappedReactionParticipant.mapped_reaction_id == mapping.id
                        )
                    ).all()
                }
                for mapping in mappings
                if mapping.id != source_mapping.id
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_derived_mapping_shares_source_ts_and_unchanged_endpoint_evidence() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            source, target, _logical, source_mapping = _mapped_reaction_fixture(session)
            coordinates = np.asarray(
                [
                    [0.0, 0.0, 0.0],
                    [1.4, 0.0, 0.0],
                    [0.0, 1.2, 0.0],
                    [0.0, 0.0, 1.1],
                    [1.0, 1.0, 1.0],
                    [2.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
            source_geometry = persist_molecular_geometry(
                session,
                normalize_molecule(
                    Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I"),
                    coordinates,
                    charge=0,
                    multiplicity=1,
                    reconstruction_method="tests/shared-source-endpoint",
                    reconstruction_version="1",
                ),
            ).geometry
            ts_geometry = persist_molecular_geometry(
                session,
                normalize_molecule(
                    Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I"),
                    coordinates + 0.2,
                    charge=0,
                    multiplicity=1,
                    reconstruction_method="tests/shared-source-ts",
                    reconstruction_version="1",
                ),
            ).geometry
            assert source_geometry.id is not None
            assert ts_geometry.id is not None

            source_participants = {
                participant.side: participant
                for participant in session.exec(
                    select(MappedReactionParticipant).where(
                        MappedReactionParticipant.mapped_reaction_id == source_mapping.id
                    )
                ).all()
            }
            for side in LogicalReactionParticipantSide:
                participant = source_participants[side]
                node = resolve_endpoint_node(session, source_mapping, side)
                binding = persist_mapped_reaction_node_geometry(
                    session,
                    node,
                    source_geometry,
                    MappedReactionNodeGeometryRecord(
                        component_key=f"{side.value}:0",
                        component_index=0,
                        coordinate_index=0,
                        is_primary=True,
                    ),
                    mapped_reaction_participant=participant,
                    thermodynamic_property_verified=True,
                )
                persist_mapped_reaction_node_geometry_mapping(
                    session,
                    binding,
                    MappedReactionNodeGeometryMappingRecord(
                        geometry_atom_map_numbers=list(participant.atom_map_numbers),
                        mapped_smiles=participant.mapped_smiles,
                        mapping_method="tests/shared-evidence",
                        mapping_version="1",
                        verified=True,
                    ),
                )

            ts_node = ensure_transition_state_path(session, mapped_reaction=source_mapping)
            ts_binding = persist_mapped_reaction_node_geometry(
                session,
                ts_node,
                ts_geometry,
                MappedReactionNodeGeometryRecord(
                    component_key="transition-state",
                    component_index=0,
                    coordinate_index=0,
                    is_primary=True,
                ),
                thermodynamic_property_verified=True,
            )
            atom_maps = list(
                source_participants[LogicalReactionParticipantSide.REACTANT].atom_map_numbers
            )
            persist_mapped_reaction_node_geometry_mapping(
                session,
                ts_binding,
                MappedReactionNodeGeometryMappingRecord(
                    geometry_atom_map_numbers=atom_maps,
                    mapped_smiles=mapped_smiles_for_topology(ts_geometry.topology, atom_maps),
                    mapping_method="tests/shared-evidence",
                    mapping_version="1",
                    verified=True,
                ),
            )

            ensure_mapped_reactions_for_concrete_topology(
                session,
                target,
                refresh_thermodynamics=False,
            )
            mappings = session.exec(
                select(MappedReaction).where(
                    MappedReaction.logical_reaction_id == source_mapping.logical_reaction_id
                )
            ).all()
            assert len(mappings) == 4

            for mapping in mappings:
                edges = session.exec(
                    select(MappedReactionEdge).where(
                        MappedReactionEdge.mapped_reaction_id == mapping.id,
                        col(MappedReactionEdge.transition_state_node_id).is_not(None),
                    )
                ).all()
                ts_bindings = session.exec(
                    select(MappedReactionNodeGeometry)
                    .join(MappedReactionNode)
                    .where(
                        MappedReactionNode.mapped_reaction_id == mapping.id,
                        MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
                    )
                ).all()
                assert len(edges) == 1
                assert {binding.geometry_id for binding in ts_bindings} == {ts_geometry.id}

            reactant_variant = next(
                mapping
                for mapping in mappings
                if any(
                    participant.side is LogicalReactionParticipantSide.REACTANT
                    and participant.concrete_topology_id == target.id
                    for participant in session.exec(
                        select(MappedReactionParticipant).where(
                            MappedReactionParticipant.mapped_reaction_id == mapping.id
                        )
                    ).all()
                )
                and all(
                    participant.concrete_topology_id
                    == (
                        target.id
                        if participant.side is LogicalReactionParticipantSide.REACTANT
                        else source.id
                    )
                    for participant in session.exec(
                        select(MappedReactionParticipant).where(
                            MappedReactionParticipant.mapped_reaction_id == mapping.id
                        )
                    ).all()
                )
            )
            product_participant = session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == reactant_variant.id,
                    MappedReactionParticipant.side == LogicalReactionParticipantSide.PRODUCT,
                )
            ).one()
            product_bindings = session.exec(
                select(MappedReactionNodeGeometry)
                .join(MappedReactionNode)
                .where(
                    MappedReactionNode.mapped_reaction_id == reactant_variant.id,
                    MappedReactionNode.role == MappedReactionNodeRole.PRODUCT,
                    MappedReactionNodeGeometry.mapped_reaction_participant_id
                    == product_participant.id,
                )
            ).all()
            assert {binding.geometry_id for binding in product_bindings} == {source_geometry.id}
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_geometry_arrival_uses_strict_topology_and_can_bind_new_mapping() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            source, target, _logical, source_mapping = _mapped_reaction_fixture(session)
            molecule = Chem.MolFromSmiles("F[C@@H](Cl)[C@H](Br)I")
            assert molecule is not None
            normalized = normalize_molecule(
                molecule,
                np.asarray(
                    [
                        [0.0, 0.0, 0.0],
                        [1.4, 0.0, 0.0],
                        [0.0, 1.2, 0.0],
                        [0.0, 0.0, 1.1],
                        [1.0, 1.0, 1.0],
                        [2.0, 0.0, 1.0],
                    ],
                    dtype=np.float64,
                ),
                charge=0,
                multiplicity=1,
                reconstruction_method="tests/geometry-concrete-target",
                reconstruction_version="1",
            )

            persisted = persist_molecular_geometry(session, normalized)
            assert persisted.topology.id == target.id
            target_mappings = session.exec(
                select(MappedReaction)
                .where(MappedReaction.logical_reaction_id == source_mapping.logical_reaction_id)
                .where(MappedReaction.mapping_hash != source_mapping.mapping_hash)
            ).all()
            target_mapping = next(
                mapping
                for mapping in target_mappings
                if all(
                    participant.concrete_topology_id == target.id
                    for participant in session.exec(
                        select(MappedReactionParticipant).where(
                            MappedReactionParticipant.mapped_reaction_id == mapping.id
                        )
                    ).all()
                )
            )
            target_participant = session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == target_mapping.id,
                    MappedReactionParticipant.side == LogicalReactionParticipantSide.REACTANT,
                )
            ).one()
            reactant_node = session.exec(
                select(MappedReactionNode).where(
                    MappedReactionNode.mapped_reaction_id == target_mapping.id,
                    MappedReactionNode.role == MappedReactionNodeRole.REACTANT,
                )
            ).one()

            binding = persist_mapped_reaction_node_geometry(
                session,
                reactant_node,
                persisted.geometry,
                MappedReactionNodeGeometryRecord(
                    component_key="reactant:0",
                    component_index=0,
                    coordinate_index=0,
                    is_primary=False,
                ),
                mapped_reaction_participant=target_participant,
                thermodynamic_property_verified=True,
            )
            assert binding.geometry_id == persisted.geometry.id
            assert binding.mapped_reaction_participant_id == target_participant.id
            assert (
                session.exec(
                    select(MappedReactionNodeGeometry).where(
                        MappedReactionNodeGeometry.id == binding.id,
                    )
                )
                .one()
                .geometry_id
                == persisted.geometry.id
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_inversion_projection_clears_n_related_ez_only() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            components: list[_ResolvedComponent] = []
            for side, smiles in (
                (
                    LogicalReactionParticipantSide.REACTANT,
                    "[C:1]/[C:2]=[N:3]/[C:4]",
                ),
                (
                    LogicalReactionParticipantSide.PRODUCT,
                    "[C:1][N:3]([C:2])[C:4]",
                ),
            ):
                molecule = Chem.MolFromSmiles(smiles)
                assert molecule is not None
                persisted = persist_molecular_topology(
                    session,
                    normalize_topology(
                        molecule,
                        add_hydrogens=False,
                        reconstruction_method=f"tests/inversion-{side.value}",
                        reconstruction_version="1",
                    ),
                )
                components.append(
                    _ResolvedComponent(
                        side=side,
                        template_index=0,
                        formula=persisted.formula,
                        topology=persisted.topology,
                        topology_atom_map_numbers=[
                            atom.GetAtomMapNum() for atom in molecule.GetAtoms()
                        ],
                    )
                )

            logical_components = _logicalize_components(session, components)
            reactant = logical_components[0]
            product = logical_components[1]
            assert reactant.logical_topology is not None
            assert product.logical_topology is not None
            assert assigned_stereo_features(reactant.topology.mol)
            assert assigned_stereo_features(reactant.logical_topology.mol) == ()
            assert product.logical_topology.id == product.topology.id
            assert reactant.logical_topology.id != reactant.topology.id
            assert reactant.logical_topology.is_stereo_abstraction_upstream is True
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
