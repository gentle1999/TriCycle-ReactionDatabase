"""Integration coverage for abstract logical participants and strict mappings."""

import os
from hashlib import sha256
from typing import Any

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.reactions import (
    CreateReactionCommand,
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    source_atom_order_authoritative,
)
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    canonical_reaction_identity,
)
from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    parse_mapped_reaction_smiles,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_geometry as _persist_molecular_geometry_impl,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_topology as _persist_molecular_topology_impl,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    _create_reaction,
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
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
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
from tricycle_reaction_db.domain.identity import SYSTEM_PROJECT_ID
from tricycle_reaction_db.ingestion.normalization import (
    normalize_molecule,
    normalize_topology,
    normalize_topology_with_mapping,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def persist_molecular_geometry(session: Session, record: Any, **kwargs: Any) -> Any:
    """Keep legacy mapping fixtures inside an explicit project scope."""

    kwargs.setdefault("context", GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID))
    return _persist_molecular_geometry_impl(session, record, **kwargs)


def persist_molecular_topology(session: Session, record: Any, **kwargs: Any) -> Any:
    """Keep legacy topology fixtures inside an explicit project scope."""

    kwargs.setdefault("context", GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID))
    return _persist_molecular_topology_impl(session, record, **kwargs)


def _strict_stereo_topology(
    session: Session,
    smiles: str,
    *,
    method: str,
) -> MolecularTopology:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    molecule = Chem.AddHs(molecule)
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
        context=GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID),
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
        project_id=SYSTEM_PROJECT_ID,
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
    identity = canonical_reaction_identity(
        {side: [(source.mol, atom_maps)] for side in LogicalReactionParticipantSide}
    )
    atom_maps = [identity.source_map_to_canonical[number] for number in atom_maps]
    mapped_smiles = mapped_smiles_for_topology(source, atom_maps)
    mapped_reaction_smiles = identity.smiles
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
        source_mapped_reaction_smiles=identity.smiles,
        canonical_identity=identity,
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
            parsed = parse_mapped_reaction_smiles(target_mapping.mapped_reaction_smiles)
            for molecule in (*parsed.GetReactants(), *parsed.GetProducts()):
                for atom in molecule.GetAtoms():
                    atom.SetAtomMapNum(0)
                assert Chem.MolToSmiles(Chem.RemoveHs(molecule)) == Chem.MolToSmiles(
                    Chem.RemoveHs(target.mol)
                )

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


@pytest.mark.parametrize("trusted_source_mapping", [False, True])
def test_derived_stereoisomer_keeps_endpoints_without_borrowing_source_ts(
    trusted_source_mapping: bool,
) -> None:
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
                    [1.4, -1.0, 0.0],
                    [0.2, 0.4, 2.0],
                ],
                dtype=np.float64,
            )
            source_geometry = persist_molecular_geometry(
                session,
                normalize_molecule(
                    Chem.AddHs(Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I")),
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
                    Chem.AddHs(Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I")),
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
                    mapped_smiles=mapped_smiles_for_topology(
                        ts_geometry.topology,
                        atom_maps,
                        include_stereochemistry=False,
                    ),
                    mapping_method=(
                        REACTION_TS_GEOMETRY_LINK_METHOD
                        if trusted_source_mapping
                        else "tests/shared-evidence"
                    ),
                    mapping_version=(
                        REACTION_TS_GEOMETRY_LINK_POLICY_VERSION if trusted_source_mapping else "1"
                    ),
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
                # A concrete stereoisomer does not have the source TS's inferred
                # endpoints. Sharing a logical graph alone is not TS evidence,
                # even when the source association uses the current policy.
                expected = {ts_geometry.id} if mapping.id == source_mapping.id else set()
                assert {binding.geometry_id for binding in ts_bindings} == expected

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
            molecule = Chem.AddHs(molecule)
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
                        [1.4, -1.0, 0.0],
                        [0.2, 0.4, 2.0],
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
            ensure_mapped_reactions_for_concrete_topology(
                session,
                target,
                refresh_thermodynamics=True,
            )
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
                molecule = Chem.AddHs(molecule)
                next_map = max(atom.GetAtomMapNum() for atom in molecule.GetAtoms()) + 1
                for atom in molecule.GetAtoms():
                    if not atom.GetAtomMapNum():
                        atom.SetAtomMapNum(next_map)
                        next_map += 1
                record, source_to_topology = normalize_topology_with_mapping(
                    molecule,
                    add_hydrogens=False,
                    reconstruction_method=f"tests/inversion-{side.value}",
                    reconstruction_version="1",
                )
                topology_maps = [0] * molecule.GetNumAtoms()
                for index, atom in enumerate(molecule.GetAtoms()):
                    topology_maps[source_to_topology[index]] = atom.GetAtomMapNum()
                persisted = persist_molecular_topology(session, record)
                components.append(
                    _ResolvedComponent(
                        side=side,
                        template_index=0,
                        formula=persisted.formula,
                        topology=persisted.topology,
                        topology_atom_map_numbers=topology_maps,
                    )
                )

            logical_components = _logicalize_components(
                session,
                components,
                topology_context=GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID),
            )
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


def test_source_authoritative_reaction_uses_product_flip_centres_for_precursor() -> None:
    reaction_smiles = (
        "[H:11][O:12][C:13]([H:14])([H:15])/[C:16]([H:17])="
        "[C:18](/[H:19])[C:20]([H:21])([H:22])[O:23][H:24]."
        "[H:1][O:2]/[N:3]=[C:4]([H:5])/[C:6]([H:7])="
        "[N:8]/[O:9][H:10]>>"
        "[H:1][O:2][N:3]1[C:4]([H:5])=[C:6]([H:7])[N:8]([O:9][H:10])"
        "[C@:18]([H:19])([C:20]([H:21])([H:22])[O:23][H:24])"
        "[C@@:16]1([C:13]([O:12][H:11])([H:14])[H:15])[H:17]"
    )
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            with source_atom_order_authoritative(session):
                reaction = _create_reaction(
                    session,
                    CreateReactionCommand(reaction=reaction_smiles),
                    topology_context=GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID),
                )

            assert reaction.mapped_reaction_id is not None
            mapped_participants = session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == reaction.mapped_reaction_id,
                    MappedReactionParticipant.side == LogicalReactionParticipantSide.REACTANT,
                )
            ).all()
            oxime = next(
                participant
                for participant in mapped_participants
                if sum(
                    atom.GetAtomicNum() == 7
                    for atom in Chem.MolFromSmiles(participant.mapped_smiles).GetAtoms()
                )
                == 2
            )
            logical_participant = session.get(
                LogicalReactionParticipant,
                oxime.logical_reaction_participant_id,
            )
            assert logical_participant is not None
            logical_topology = session.get(MolecularTopology, logical_participant.topology_id)
            concrete_topology = session.get(MolecularTopology, oxime.concrete_topology_id)
            assert logical_topology is not None
            assert concrete_topology is not None

            def imine_ez_count(molecule: Chem.Mol) -> int:
                return sum(
                    bond.GetBondType() == Chem.BondType.DOUBLE
                    and bond.GetStereo() != Chem.BondStereo.STEREONONE
                    and {
                        bond.GetBeginAtom().GetAtomicNum(),
                        bond.GetEndAtom().GetAtomicNum(),
                    }
                    == {6, 7}
                    for bond in molecule.GetBonds()
                )

            assert imine_ez_count(concrete_topology.mol) == 2
            assert imine_ez_count(logical_topology.mol) == 0
            assert logical_topology.id != concrete_topology.id
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_inversion_projection_clears_sulfur_chirality() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
            components: list[_ResolvedComponent] = []
            for side, smiles in (
                (
                    LogicalReactionParticipantSide.REACTANT,
                    "[CH3:1][S@+:2]([O-:3])[CH2:4][Cl:5]",
                ),
                (
                    LogicalReactionParticipantSide.PRODUCT,
                    "[CH3:1][S+:2]([O-:3])[CH2:4][Cl:5]",
                ),
            ):
                molecule = Chem.MolFromSmiles(smiles)
                assert molecule is not None
                molecule = Chem.AddHs(molecule)
                next_map = max(atom.GetAtomMapNum() for atom in molecule.GetAtoms()) + 1
                for atom in molecule.GetAtoms():
                    if not atom.GetAtomMapNum():
                        atom.SetAtomMapNum(next_map)
                        next_map += 1
                record, source_to_topology = normalize_topology_with_mapping(
                    molecule,
                    add_hydrogens=False,
                    reconstruction_method=f"tests/inversion-{side.value}",
                    reconstruction_version="1",
                )
                topology_maps = [0] * molecule.GetNumAtoms()
                for index, atom in enumerate(molecule.GetAtoms()):
                    topology_maps[source_to_topology[index]] = atom.GetAtomMapNum()
                persisted = persist_molecular_topology(session, record)
                components.append(
                    _ResolvedComponent(
                        side=side,
                        template_index=0,
                        formula=persisted.formula,
                        topology=persisted.topology,
                        topology_atom_map_numbers=topology_maps,
                    )
                )

            logical_components = _logicalize_components(
                session,
                components,
                topology_context=GeometryPersistenceContext(project_id=SYSTEM_PROJECT_ID),
            )
            reactant = logical_components[0]
            product = logical_components[1]
            assert reactant.logical_topology is not None
            assert product.logical_topology is not None
            assert assigned_stereo_features(reactant.topology.mol)
            assert assigned_stereo_features(reactant.logical_topology.mol) == ()
            assert product.logical_topology.id == product.topology.id
            assert reactant.logical_topology.id != reactant.topology.id
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
