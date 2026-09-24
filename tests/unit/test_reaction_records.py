from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from rdkit import Chem
from sqlmodel import Session

import tricycle_reaction_db.application.services.reactions as reaction_persistence
from tricycle_reaction_db.application.dtos import (
    CreateReactionCommand,
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    ManifestArtifactBindingRecord,
    MappedReactionNodeGeometryMappingRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    LEGACY_BULK_IMPORT_SESSION_INFO_KEY,
    _queue_fast_pending_entity,
)
from tricycle_reaction_db.application.services.reaction_commands import (
    _atom_maps_in_persisted_topology_order,
    _mapped_reaction_smiles_from_components,
)
from tricycle_reaction_db.application.services.reactions import (
    _canonical_mapped_reaction_smiles,
    _canonical_mapped_topology_identity,
    _logical_map_numbers_for_reaction,
    _mapped_reaction_from_smiles,
    _mapping_assignment_for_topology,
    _validate_source_mapped_reaction_smiles,
    atom_maps_from_source_order,
    mapped_reaction_concrete_identity,
    mapped_smiles_for_geometry,
    mapped_smiles_for_topology,
    persist_logical_reaction,
    persist_logical_reaction_participant,
    persist_mapped_reaction_node_geometry_mapping,
)
from tricycle_reaction_db.db.models import (
    Geometry,
    MappedReaction,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    MappedReactionNodeRole,
    WorkflowManifestStatus,
)
from tricycle_reaction_db.ingestion.normalization import normalize_topology_with_mapping


def test_manifest_record_requires_a_timezone_aware_publication_timestamp() -> None:
    common = {
        "manifest_key": "da-bench:ene_diene:00",
        "revision": 1,
        "schema_version": "da-bench-manifest-v1",
        "payload_sha256": "1" * 64,
        "qc_policy_version": "cycloaddition-qc-v1",
    }

    with pytest.raises(ValidationError, match="requires published_at"):
        WorkflowManifestRecord(**common, status=WorkflowManifestStatus.PUBLISHED)
    with pytest.raises(ValidationError, match="timezone info"):
        WorkflowManifestRecord(
            **common,
            status=WorkflowManifestStatus.PUBLISHED,
            published_at=datetime(2026, 7, 13),
        )
    with pytest.raises(ValidationError, match="cannot have published_at"):
        WorkflowManifestRecord(
            **common,
            status=WorkflowManifestStatus.VALIDATED,
            published_at=datetime.now(UTC),
        )


def test_create_reaction_command_requires_only_a_reaction_representation() -> None:
    command = CreateReactionCommand(reaction="C1CC1>>C=CC")

    assert command.reaction == "C1CC1>>C=CC"
    assert command.reaction_class is None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateReactionCommand(
            reaction="C1CC1>>C=CC",
            reactants=[{"topology_id": "00000000-0000-0000-0000-000000000000"}],
        )


def test_source_order_mapped_reaction_serialization_is_preserved() -> None:
    source_smiles = "[CH3:1][OH:2]>>[CH2:1]=[O:2]"
    topology_smiles = {
        (LogicalReactionParticipantSide.REACTANT, 0): "[OH:2][CH3:1]",
        (LogicalReactionParticipantSide.PRODUCT, 0): "[O:2]=[CH2:1]",
    }
    expected_maps = {
        LogicalReactionParticipantSide.REACTANT: {1, 2},
        LogicalReactionParticipantSide.PRODUCT: {1, 2},
    }

    selected_serialization = _mapped_reaction_smiles_from_components(
        source_smiles,
        topology_smiles,
        preserve_source_serialization=True,
    )
    serialized = _validate_source_mapped_reaction_smiles(
        selected_serialization,
        expected_atom_maps_by_side=expected_maps,
    )

    assert serialized == source_smiles
    assert (
        _mapped_reaction_smiles_from_components(
            source_smiles,
            topology_smiles,
            preserve_source_serialization=False,
        )
        == "[OH:2][CH3:1]>>[O:2]=[CH2:1]"
    )


def test_source_reaction_components_follow_aligned_participant_indices() -> None:
    source_smiles = "[NH2:3][CH3:4].[CH3:1][OH:2]>>[CH2:3]=[O:4].[NH:1]=[CH2:2]"
    atom_maps = {
        (LogicalReactionParticipantSide.REACTANT, 0): [1, 2],
        (LogicalReactionParticipantSide.REACTANT, 1): [3, 4],
        (LogicalReactionParticipantSide.PRODUCT, 0): [1, 2],
        (LogicalReactionParticipantSide.PRODUCT, 1): [3, 4],
    }

    selected = _mapped_reaction_smiles_from_components(
        source_smiles,
        {},
        preserve_source_serialization=True,
        source_atom_maps_by_template=atom_maps,
    )

    assert selected == ("[CH3:1][OH:2].[NH2:3][CH3:4]>>[NH:1]=[CH2:2].[CH2:3]=[O:4]")
    assert (
        _validate_source_mapped_reaction_smiles(
            selected,
            expected_atom_maps_by_side={
                LogicalReactionParticipantSide.REACTANT: {1, 2, 3, 4},
                LogicalReactionParticipantSide.PRODUCT: {1, 2, 3, 4},
            },
        )
        == selected
    )


def test_fast_batch_reuses_pending_logical_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session()
    session.info["tricycle_fast_insert"] = True
    project_id = UUID("00000000-0000-7000-8000-000000000030")
    record = LogicalReactionRecord(
        reaction_key="same-reaction",
        reaction_hash="a" * 64,
    )
    select_count = 0
    next_id = UUID("00000000-0000-7000-8000-000000000031")

    def fake_exec(_statement: object) -> SimpleNamespace:
        nonlocal select_count
        select_count += 1
        return SimpleNamespace(first=lambda: None)

    def fake_new_entity(_session: Session, entity_type: type, **fields: object) -> object:
        return entity_type(id=next_id, **fields)

    monkeypatch.setattr(session, "exec", fake_exec)
    monkeypatch.setattr(reaction_persistence, "_acquire_identity_locks", lambda *_args: None)
    monkeypatch.setattr(reaction_persistence, "_new_entity", fake_new_entity)
    monkeypatch.setattr(
        reaction_persistence,
        "_flush_new_entity",
        lambda target_session, entity, **_kwargs: _queue_fast_pending_entity(
            target_session, entity
        ),
    )

    first = persist_logical_reaction(session, record, project_id=project_id)
    repeated = persist_logical_reaction(session, record, project_id=project_id)

    assert repeated is first
    assert select_count == 1


def test_fast_batch_reuses_pending_logical_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session()
    session.info[LEGACY_BULK_IMPORT_SESSION_INFO_KEY] = True
    project_id = UUID("00000000-0000-7000-8000-000000000040")
    topology_id = UUID("00000000-0000-7000-8000-000000000041")
    existing = SimpleNamespace(
        side=LogicalReactionParticipantSide.REACTANT,
        participant_index=0,
        topology_id=topology_id,
        role=None,
        stoichiometric_coefficient=1,
    )
    reaction = SimpleNamespace(
        id=UUID("00000000-0000-7000-8000-000000000042"),
        project_id=project_id,
        participants=[existing],
    )
    topology = SimpleNamespace(id=topology_id, project_id=project_id)
    record = LogicalReactionParticipantRecord(
        side=LogicalReactionParticipantSide.REACTANT,
        participant_index=0,
    )

    monkeypatch.setattr(reaction_persistence, "_acquire_identity_locks", lambda *_args: None)
    monkeypatch.setattr(
        session,
        "exec",
        lambda _statement: pytest.fail("pending participant should be reused from its parent"),
    )

    participant = persist_logical_reaction_participant(
        session,
        cast(reaction_persistence.LogicalReaction, reaction),
        cast(MolecularTopology, topology),
        record,
    )

    assert participant is existing


def test_atom_maps_are_translated_to_reused_topology_atom_order() -> None:
    source_mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert source_mol is not None
    for index, atom in enumerate(source_mol.GetAtoms(), start=1):
        atom.SetAtomMapNum(index)
    normalized, source_to_topology = normalize_topology_with_mapping(
        source_mol,
        add_hydrogens=False,
        reconstruction_method="unit-test/source-order",
        reconstruction_version="1",
    )
    source_maps = [0] * normalized.topology.atom_count
    for source_index, topology_index in enumerate(source_to_topology):
        source_maps[topology_index] = source_mol.GetAtomWithIdx(source_index).GetAtomMapNum()

    source_atom_properties = {
        source_maps[index]: (
            atom.GetAtomicNum(),
            atom.GetIsotope(),
        )
        for index, atom in enumerate(normalized.topology.mol.GetAtoms())
    }
    reordered_mol = Chem.RenumberAtoms(
        normalized.topology.mol,
        list(reversed(range(normalized.topology.atom_count))),
    )
    persisted_topology = cast(
        MolecularTopology,
        SimpleNamespace(
            mol=reordered_mol,
            atom_count=reordered_mol.GetNumAtoms(),
            stereo_status=normalized.topology.stereo_status,
        ),
    )

    aligned_maps = _atom_maps_in_persisted_topology_order(
        normalized.topology,
        persisted_topology,
        source_maps,
    )

    assert mapped_smiles_for_topology(persisted_topology, aligned_maps) == (
        mapped_smiles_for_topology(cast(MolecularTopology, normalized.topology), source_maps)
    )
    assert {
        aligned_maps[index]: (atom.GetAtomicNum(), atom.GetIsotope())
        for index, atom in enumerate(reordered_mol.GetAtoms())
    } == source_atom_properties


@pytest.mark.parametrize(
    ("source_smiles", "expected_maps", "message"),
    [
        (
            "[CH3:1][OH:2]>>[CH2:1]=[O:3]",
            {
                LogicalReactionParticipantSide.REACTANT: {1, 2},
                LogicalReactionParticipantSide.PRODUCT: {1, 2},
            },
            "atom-map sets must match",
        ),
        (
            "[CH3:1][OH:1]>>[CH2:1]=[O:2]",
            {
                LogicalReactionParticipantSide.REACTANT: {1, 2},
                LogicalReactionParticipantSide.PRODUCT: {1, 2},
            },
            "unique per side",
        ),
        (
            "[CH3:1][OH:2]>>[CH2:1]=[O:2]",
            {
                LogicalReactionParticipantSide.REACTANT: {1, 3},
                LogicalReactionParticipantSide.PRODUCT: {1, 3},
            },
            "do not match topology bindings",
        ),
    ],
)
def test_source_order_mapped_reaction_serialization_validates_bindings(
    source_smiles: str,
    expected_maps: dict[LogicalReactionParticipantSide, set[int]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_source_mapped_reaction_smiles(
            source_smiles,
            expected_atom_maps_by_side=expected_maps,
        )


def test_logical_map_numbers_include_fast_pending_participants() -> None:
    """Fast-path reaction validation must see participants before microbatch flush."""

    mapped_reaction_id = UUID("00000000-0000-0000-0000-000000000101")
    pending_participant = MappedReactionParticipant(
        mapped_reaction_id=mapped_reaction_id,
        logical_reaction_participant_id=UUID("00000000-0000-0000-0000-000000000102"),
        side=LogicalReactionParticipantSide.REACTANT,
        template_index=0,
        atom_map_numbers=[7, 8],
        mapped_smiles="[CH2:7]=[CH2:8]",
    )
    mapped_reaction = cast(
        MappedReaction,
        SimpleNamespace(id=mapped_reaction_id, participants=[]),
    )

    class _Session:
        info = {"_fast_pending_entities": (pending_participant,)}
        new: tuple[object, ...] = ()

        def exec(self, _statement: object) -> "_Session":
            return self

        def all(self) -> list[MappedReactionParticipant]:
            return []

    atom_maps = _logical_map_numbers_for_reaction(
        mapped_reaction,
        session=cast(Session, _Session()),
    )

    assert atom_maps == frozenset({7, 8})


def test_binding_record_requires_complete_selectors_without_software_authority() -> None:
    common = {
        "artifact_key": "orca-ts-sp",
        "artifact_role": ManifestArtifactRole.ORCA_SINGLE_POINT,
        "reaction_key": "reaction-0",
        "path_key": "path-0",
        "node_key": "ts",
    }

    declared = ManifestArtifactBindingRecord(**common)
    assert declared.source_geometry_artifact_key is None
    with pytest.raises(ValidationError, match="must either both be set"):
        ManifestArtifactBindingRecord(
            **common,
            source_geometry_artifact_key="gaussian-ts",
            segment_index=1,
        )
    with pytest.raises(ValidationError, match="requires an expected hash"):
        ManifestArtifactBindingRecord(
            **common,
            source_geometry_artifact_key="gaussian-ts",
            segment_index=1,
            frame_index=18,
            resolution_status=ArtifactResolutionStatus.RESOLVED,
        )


def test_logical_participant_record_rejects_invalid_stoichiometry_and_side_roles() -> None:
    common = {
        "side": LogicalReactionParticipantSide.REACTANT,
        "participant_index": 0,
    }

    with pytest.raises(ValidationError, match="coefficient=1"):
        LogicalReactionParticipantRecord(
            **common,
            stoichiometric_coefficient=2,
        )
    with pytest.raises(ValidationError, match="product role requires"):
        LogicalReactionParticipantRecord(
            **common,
            role=LogicalReactionParticipantRole.PRODUCT,
        )


def test_coordinate_mapping_record_requires_unique_positive_geometry_maps() -> None:
    common = {
        "mapped_smiles": "[C:1][O:2]",
        "mapping_method": "manifest-explicit",
        "mapping_version": "coordinate-map-v1",
        "verified": True,
    }
    with pytest.raises(ValidationError, match="positive"):
        MappedReactionNodeGeometryMappingRecord(
            **common,
            geometry_atom_map_numbers=[0, 2],
        )
    with pytest.raises(ValidationError, match="unique"):
        MappedReactionNodeGeometryMappingRecord(
            **common,
            geometry_atom_map_numbers=[1, 1],
        )


def test_ts_mapping_smiles_uses_geometry_atom_order_not_reused_topology_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology_mol = Chem.AddHs(Chem.MolFromSmiles("CO"))
    assert topology_mol is not None
    geometry_mol = Chem.RenumberAtoms(
        topology_mol,
        list(reversed(range(topology_mol.GetNumAtoms()))),
    )
    atom_count = topology_mol.GetNumAtoms()
    topology = SimpleNamespace(
        mol=topology_mol,
        atom_count=atom_count,
        stereo_status=None,
    )
    geometry = SimpleNamespace(mol=geometry_mol, atom_count=atom_count, topology=topology)
    geometry_maps = list(reversed(range(1, atom_count + 1)))
    mapped_smiles = mapped_smiles_for_geometry(
        cast(Geometry, geometry),
        geometry_maps,
        include_stereochemistry=False,
    )
    node = SimpleNamespace(
        role=MappedReactionNodeRole.TRANSITION_STATE,
        mapped_reaction=SimpleNamespace(id=UUID("00000000-0000-7000-8000-000000000090")),
    )
    node_geometry = SimpleNamespace(
        id=UUID("00000000-0000-7000-8000-000000000091"),
        mapped_reaction_node=node,
        geometry=geometry,
        mapped_reaction_participant=None,
    )
    session = Session()
    monkeypatch.setattr(
        reaction_persistence,
        "_logical_map_numbers_for_reaction",
        lambda *_args, **_kwargs: frozenset(geometry_maps),
    )
    monkeypatch.setattr(
        reaction_persistence,
        "_new_entity",
        lambda _session, _entity_type, **fields: SimpleNamespace(**fields),
    )
    monkeypatch.setattr(
        reaction_persistence,
        "_flush_new_entity",
        lambda *_args, **_kwargs: None,
    )

    record = MappedReactionNodeGeometryMappingRecord(
        geometry_atom_map_numbers=geometry_maps,
        mapped_smiles=mapped_smiles,
        mapping_method="unit-test",
        mapping_version="test-v1",
        verified=True,
    )
    persisted = persist_mapped_reaction_node_geometry_mapping(
        session,
        cast(reaction_persistence.MappedReactionNodeGeometry, node_geometry),
        record,
        identity_is_new=True,
    )

    assert persisted.mapped_smiles == mapped_smiles
    topology_order_smiles = mapped_smiles_for_topology(
        cast(MolecularTopology, topology),
        geometry_maps,
        include_stereochemistry=False,
    )
    assert topology_order_smiles != mapped_smiles
    with pytest.raises(ValueError, match="does not match the converted coordinate mapping"):
        persist_mapped_reaction_node_geometry_mapping(
            session,
            cast(reaction_persistence.MappedReactionNodeGeometry, node_geometry),
            record.model_copy(update={"mapped_smiles": topology_order_smiles}),
            identity_is_new=True,
        )


def test_source_maps_are_permuted_into_geometry_atom_order() -> None:
    geometry = SimpleNamespace(atom_count=3)

    assert atom_maps_from_source_order(cast(Geometry, geometry), [11, 22, 33], [2, 0, 1]) == [
        22,
        33,
        11,
    ]

    with pytest.raises(ValueError, match="full permutation"):
        atom_maps_from_source_order(cast(Geometry, geometry), [11, 22, 33], [0, 0, 2])


def test_ts_mapping_rejects_symmetric_map_swap_on_same_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("C"))
    assert molecule is not None
    topology = SimpleNamespace(
        mol=molecule,
        atom_count=molecule.GetNumAtoms(),
        stereo_status=None,
    )
    geometry = SimpleNamespace(
        mol=molecule,
        atom_count=molecule.GetNumAtoms(),
        topology=topology,
    )
    old_maps = [1, 2, 3, 4, 5]
    swapped_maps = [1, 3, 2, 4, 5]
    old_smiles = mapped_smiles_for_geometry(
        cast(Geometry, geometry),
        old_maps,
        include_stereochemistry=False,
    )
    swapped_smiles = mapped_smiles_for_geometry(
        cast(Geometry, geometry),
        swapped_maps,
        include_stereochemistry=False,
    )
    assert swapped_smiles == old_smiles

    node = SimpleNamespace(
        role=MappedReactionNodeRole.TRANSITION_STATE,
        mapped_reaction=SimpleNamespace(id=UUID("00000000-0000-7000-8000-000000000092")),
    )
    node_geometry = SimpleNamespace(
        id=UUID("00000000-0000-7000-8000-000000000093"),
        mapped_reaction_node=node,
        geometry=geometry,
        mapped_reaction_participant=None,
    )
    existing = SimpleNamespace(
        geometry_atom_map_numbers=old_maps,
        mapped_smiles=old_smiles,
        mapping_method="source-atom-order",
        mapping_version="reaction-ts-geometry-link-v1",
    )
    session = Session()
    monkeypatch.setattr(
        reaction_persistence,
        "_logical_map_numbers_for_reaction",
        lambda *_args, **_kwargs: frozenset(old_maps),
    )
    monkeypatch.setattr(reaction_persistence, "_acquire_identity_locks", lambda *_args: None)
    monkeypatch.setattr(
        session,
        "exec",
        lambda _statement: SimpleNamespace(first=lambda: existing),
    )

    record = MappedReactionNodeGeometryMappingRecord(
        geometry_atom_map_numbers=swapped_maps,
        mapped_smiles=swapped_smiles,
        mapping_method="source-atom-order",
        mapping_version="reaction-ts-geometry-link-v2",
        verified=True,
    )
    with pytest.raises(ValueError, match="incompatible reaction mapping"):
        persist_mapped_reaction_node_geometry_mapping(
            session,
            cast(reaction_persistence.MappedReactionNodeGeometry, node_geometry),
            record,
        )


def test_mapped_reaction_uses_rdkit_reaction_parser_with_agents() -> None:
    reaction = _mapped_reaction_from_smiles(
        "[CH3:1][OH:2]>O>[CH2:1]=[O:2]",
    )

    assert reaction.GetNumReactantTemplates() == 1
    assert reaction.GetNumAgentTemplates() == 1
    assert reaction.GetNumProductTemplates() == 1

    with pytest.raises(ValueError, match="RDKit could not parse"):
        _mapped_reaction_from_smiles("not-a-reaction")


def test_mapped_reaction_canonicalization_is_stable_for_metal_stereo() -> None:
    reaction = (
        "[Cl-:1]->[Ru@OH17+2]([Cl-:2])"
        "([P:3]([H:4])([H:5])[H:6])=[C:7]([H:8])[H:9]"
        ">>"
        "[Cl-:1]->[Ru@OH17+2]([Cl-:2])"
        "([P:3]([H:4])([H:5])[H:6])=[C:7]([H:8])[H:9]"
    )

    canonical = _canonical_mapped_reaction_smiles(_mapped_reaction_from_smiles(reaction))
    reparsed = _canonical_mapped_reaction_smiles(_mapped_reaction_from_smiles(canonical))

    assert canonical == reparsed
    assert "[Ru@" not in canonical


def test_mapping_assignment_uses_source_order_without_topology_normalization() -> None:
    """Mapped endpoint atom positions stay in their calculation-frame order."""

    benzene = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1"))
    template = Chem.RenumberAtoms(benzene, [7, 4, 10, 1, 8, 2, 11, 0, 9, 3, 6, 5])
    for atom_index, atom in enumerate(template.GetAtoms(), start=1):
        atom.SetAtomMapNum(atom_index)
    normalized, _ = normalize_topology_with_mapping(
        template,
        add_hydrogens=False,
        reconstruction_method="molgr/test",
        reconstruction_version="test",
        reconstruction_metadata={"topology_source_trusted": True},
    )
    topology = SimpleNamespace(
        atom_count=normalized.topology.atom_count,
        identity_schema_version=normalized.topology.identity_schema_version,
        graph_hash=normalized.topology.graph_hash,
        mol=normalized.topology.mol,
    )

    atom_maps, mapped_smiles = _mapping_assignment_for_topology(
        template,
        topology,
        source_atom_map_numbers=list(range(1, topology.atom_count + 1)),
    )

    assert atom_maps == list(range(1, topology.atom_count + 1))
    expected = Chem.Mol(topology.mol)
    for atom_index, atom in enumerate(expected.GetAtoms(), start=1):
        atom.SetAtomMapNum(atom_index)
    assert mapped_smiles == Chem.MolToSmiles(
        expected,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )


def test_mapping_assignment_allows_endpoint_graph_change() -> None:
    """Template and persisted endpoint need not have the same bond topology."""

    template = Chem.MolFromSmiles("[CH3:1][CH3:2]")
    endpoint = Chem.MolFromSmiles("[CH2:1]=[CH2:2]")
    topology = SimpleNamespace(atom_count=2, mol=endpoint)

    atom_maps, mapped_smiles = _mapping_assignment_for_topology(template, topology)

    assert atom_maps == [1, 2]
    assert mapped_smiles == "[CH2:1]=[CH2:2]"


def test_mapping_assignment_preserves_global_maps_for_endpoint_fragments() -> None:
    """A fragment keeps the calculation frame's global atom-map numbers."""

    template = Chem.MolFromSmiles("[CH3:7][CH3:8]")
    endpoint = Chem.MolFromSmiles("[CH2]=[CH2]")
    topology = SimpleNamespace(atom_count=2, mol=endpoint)

    atom_maps, mapped_smiles = _mapping_assignment_for_topology(
        template,
        topology,
        source_atom_map_numbers=[7, 8],
    )

    assert atom_maps == [7, 8]
    assert mapped_smiles == "[CH2:7]=[CH2:8]"


def test_concrete_mapping_identity_collapses_symmetric_atom_assignments() -> None:
    """Equivalent source automorphisms do not create a second mapping row."""

    molecule = Chem.MolFromSmiles("C1CC1")
    assert molecule is not None
    topology_id = UUID("00000000-0000-0000-0000-000000000001")
    topology = SimpleNamespace(id=topology_id, atom_count=3, mol=molecule)

    class _Session:
        info: dict[str, object] = {}
        new: tuple[object, ...] = ()

        def get(self, _model: object, value: object) -> object | None:
            return topology if value == topology_id else None

    logical_participant = SimpleNamespace(topology_id=topology_id, topology=topology)

    def participant(atom_maps: tuple[int, ...]) -> MappedReactionParticipant:
        return cast(
            MappedReactionParticipant,
            SimpleNamespace(
                side=LogicalReactionParticipantSide.REACTANT,
                template_index=0,
                concrete_topology_id=topology_id,
                logical_reaction_participant=logical_participant,
                logical_reaction_participant_id=UUID("00000000-0000-0000-0000-000000000002"),
                atom_map_numbers=list(atom_maps),
            ),
        )

    first = cast(
        MappedReaction,
        SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000003")),
    )
    second = cast(
        MappedReaction,
        SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000004")),
    )
    session = _Session()

    first_identity = mapped_reaction_concrete_identity(
        cast(Session, session),
        first,
        participants=(participant((1, 2, 3)),),
    )
    second_identity = mapped_reaction_concrete_identity(
        cast(Session, session),
        second,
        participants=(participant((2, 3, 1)),),
    )
    assert first_identity == second_identity


def test_canonical_mapped_topology_handles_highly_symmetric_topology() -> None:
    """Canonicalization must not enumerate every self-graph automorphism."""

    molecule = Chem.MolFromSmiles(".".join(["C"] * 12))
    assert molecule is not None
    topology = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000005"),
        atom_count=12,
        mol=molecule,
    )

    class _Session:
        info: dict[str, object] = {}

    session = _Session()
    canonical = _canonical_mapped_topology_identity(
        cast(Session, session),
        topology,
        tuple(range(12, 0, -1)),
    )

    assert '"schema_version":"mapped-topology-symmetry-v1"' in canonical
    assert all(str(map_number) in canonical for map_number in range(1, 13))


def test_canonical_mapped_topology_collapses_meso_symmetric_centers() -> None:
    """Equivalent meso centers must not create a second mapping identity."""

    molecule = Chem.MolFromSmiles("C[C@H](Br)[C@@H](C)Br")
    assert molecule is not None
    topology = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000007"),
        atom_count=molecule.GetNumAtoms(),
        mol=molecule,
    )

    class _Session:
        info: dict[str, object] = {}

    session = _Session()
    first = _canonical_mapped_topology_identity(
        cast(Session, session),
        topology,
        (1, 2, 3, 4, 5, 6),
    )
    second = _canonical_mapped_topology_identity(
        cast(Session, session),
        topology,
        (1, 4, 6, 2, 5, 3),
    )

    assert first == second


def test_canonical_mapped_topology_keeps_distinct_assignments_distinct() -> None:
    """Canonicalization must only collapse assignments related by an automorphism."""

    molecule = Chem.MolFromSmiles("CCC")
    assert molecule is not None
    topology = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000006"),
        atom_count=3,
        mol=molecule,
    )

    class _Session:
        info: dict[str, object] = {}

    session = _Session()
    first = _canonical_mapped_topology_identity(
        cast(Session, session),
        topology,
        (1, 2, 3),
    )
    second = _canonical_mapped_topology_identity(
        cast(Session, session),
        topology,
        (1, 3, 2),
    )

    assert first != second


@pytest.mark.parametrize(
    ("source_smiles", "expected_stereo"),
    [
        ("C/C=C/C", Chem.BondStereo.STEREOE),
        ("C/C=C\\C", Chem.BondStereo.STEREOZ),
    ],
)
def test_mapped_smiles_restores_ez_after_database_round_trip(
    source_smiles: str,
    expected_stereo: Chem.BondStereo,
) -> None:
    """Mapped SMILES retain assigned E/Z when RDKit dropped BondDir flags."""

    molecule = Chem.AddHs(Chem.MolFromSmiles(source_smiles))
    for bond in molecule.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)
    topology = SimpleNamespace(atom_count=molecule.GetNumAtoms(), mol=molecule)

    mapped_smiles = mapped_smiles_for_topology(
        topology,
        list(range(1, molecule.GetNumAtoms() + 1)),
    )
    reparsed = Chem.MolFromSmiles(mapped_smiles)
    assert reparsed is not None
    double_bond = next(
        bond for bond in reparsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )
    assert double_bond.GetStereo() == expected_stereo
    assert "/" in mapped_smiles or "\\" in mapped_smiles


def test_transition_state_mapping_smiles_can_omit_unrepresentable_metal_ez() -> None:
    """TS map identity remains serializable when E/Z uses a dative metal bond."""

    molecule = Chem.RWMol()
    for atomic_number, formal_charge in ((7, 0), (6, 0), (45, 3), (8, -1), (6, 0)):
        atom = Chem.Atom(atomic_number)
        atom.SetFormalCharge(formal_charge)
        molecule.AddAtom(atom)
    molecule.AddBond(0, 1, Chem.BondType.DOUBLE)
    molecule.AddBond(0, 2, Chem.BondType.DATIVE)
    molecule.AddBond(1, 3, Chem.BondType.SINGLE)
    molecule.AddBond(1, 4, Chem.BondType.SINGLE)
    topology_mol = molecule.GetMol()
    double_bond = topology_mol.GetBondBetweenAtoms(0, 1)
    assert double_bond is not None
    double_bond.SetStereoAtoms(2, 3)
    double_bond.SetStereo(Chem.BondStereo.STEREOE)
    topology = SimpleNamespace(atom_count=topology_mol.GetNumAtoms(), mol=topology_mol)

    with pytest.raises(ValueError, match="no SMILES traversal preserves"):
        mapped_smiles_for_topology(topology, range(1, topology.atom_count + 1))

    mapped_smiles = mapped_smiles_for_topology(
        topology,
        range(1, topology.atom_count + 1),
        include_stereochemistry=False,
    )
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    parsed = Chem.MolFromSmiles(mapped_smiles, parser)
    assert parsed is not None
    assert {atom.GetAtomMapNum() for atom in parsed.GetAtoms()} == {1, 2, 3, 4, 5}
    assert not any(
        bond.GetStereo() in {Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ}
        for bond in parsed.GetBonds()
    )
    assert "/" not in mapped_smiles and "\\" not in mapped_smiles


def test_mapped_smiles_keeps_distinct_symmetric_diene_configurations() -> None:
    """Adding reaction maps must not collapse strict E/Z topologies."""

    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    configurations = (
        "[H][N]([H])/[N]=[C]([C](=[N]\\[N]([H])[H])\\[C]([H])([H])[H])/[C]([H])([H])[H]",
        "[H][N]([H])/[N]=[C]([C](=[N]/[N]([H])[H])/[C]([H])([H])[H])\\[C]([H])([H])[H]",
        "[H][N]([H])/[N]=[C]([C](=[N]/[N]([H])[H])\\[C]([H])([H])[H])/[C]([H])([H])[H]",
    )
    mapped = []
    for smiles in configurations:
        molecule = Chem.MolFromSmiles(smiles, parser)
        assert molecule is not None
        topology = SimpleNamespace(
            atom_count=molecule.GetNumAtoms(),
            mol=molecule,
            canonical_isomeric_smiles=smiles,
        )
        mapped_smiles = mapped_smiles_for_topology(
            topology,
            list(range(1, molecule.GetNumAtoms() + 1)),
        )
        reparsed = Chem.MolFromSmiles(mapped_smiles, parser)
        assert reparsed is not None
        mapped.append(mapped_smiles)
        assert (
            sum(
                bond.GetStereo() in {Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ}
                for bond in reparsed.GetBonds()
                if bond.GetBondType() == Chem.BondType.DOUBLE
            )
            == 2
        )

    assert len(set(mapped)) == 3


def test_mapped_smiles_uses_persisted_projection_for_complex_ez_after_round_trip() -> None:
    source = Chem.AddHs(Chem.MolFromSmiles("F/C=C(/[C@H](Cl)Br)I"))
    canonical_projection = Chem.MolToSmiles(
        source,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    stored = Chem.Mol(source.ToBinary())
    for bond in stored.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)
    topology = SimpleNamespace(
        atom_count=stored.GetNumAtoms(),
        mol=stored,
        canonical_isomeric_smiles=canonical_projection,
    )

    mapped_smiles = mapped_smiles_for_topology(
        topology,
        list(range(1, stored.GetNumAtoms() + 1)),
    )
    reparsed = Chem.MolFromSmiles(mapped_smiles)
    assert reparsed is not None
    double_bond = next(
        bond for bond in reparsed.GetBonds() if bond.GetBondType() == Chem.BondType.DOUBLE
    )

    assert double_bond.GetStereo() == Chem.BondStereo.STEREOZ
    assert "/" in mapped_smiles or "\\" in mapped_smiles


@pytest.mark.parametrize(
    ("source_smiles", "expected_chiral_tag"),
    [
        ("C[C@H](F)Cl", Chem.ChiralType.CHI_TETRAHEDRAL_CCW),
        ("C[C@@H](F)Cl", Chem.ChiralType.CHI_TETRAHEDRAL_CW),
        ("Cl[Pt@SP1](Cl)([NH3])[NH3]", Chem.ChiralType.CHI_SQUAREPLANAR),
        ("F[P@TB1](Cl)(Br)(I)N", Chem.ChiralType.CHI_TRIGONALBIPYRAMIDAL),
        ("N[Co@OH1](N)(N)(N)(N)N", Chem.ChiralType.CHI_OCTAHEDRAL),
    ],
)
def test_mapped_smiles_preserves_atom_stereo_after_database_round_trip(
    source_smiles: str,
    expected_chiral_tag: Chem.ChiralType,
) -> None:
    """Mapped SMILES retain tetrahedral and supported non-tetrahedral tags."""

    molecule = Chem.MolFromSmiles(source_smiles)
    assert molecule is not None
    # Simulate the PostgreSQL RDKit object boundary.  The test deliberately
    # does not call AssignStereochemistry after loading the molecule.
    molecule = Chem.Mol(molecule.ToBinary())
    topology = SimpleNamespace(atom_count=molecule.GetNumAtoms(), mol=molecule)

    mapped_smiles = mapped_smiles_for_topology(
        topology,
        list(range(1, molecule.GetNumAtoms() + 1)),
    )
    reparsed = Chem.MolFromSmiles(mapped_smiles)
    assert reparsed is not None
    chiral_tags = [
        atom.GetChiralTag()
        for atom in reparsed.GetAtoms()
        if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
    ]

    assert chiral_tags == [expected_chiral_tag]
    assert "@" in mapped_smiles


def test_mapping_assignment_preserves_molop_source_atom_order() -> None:
    """TS endpoint maps remain tied to the calculation-frame atom sequence."""

    source = Chem.RenumberAtoms(
        Chem.AddHs(Chem.MolFromSmiles("c1ccccc1")),
        [7, 4, 10, 1, 8, 2, 11, 0, 9, 3, 6, 5],
    )
    mapped_source = Chem.Mol(source)
    for atom_index, atom in enumerate(mapped_source.GetAtoms(), start=1):
        atom.SetAtomMapNum(atom_index)
    source_smiles = Chem.MolToSmiles(
        mapped_source,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    reaction_smiles = _canonical_mapped_reaction_smiles(
        _mapped_reaction_from_smiles(f"{source_smiles}>>{source_smiles}")
    )
    template = _mapped_reaction_from_smiles(reaction_smiles).GetReactants()[0]
    normalized, _ = normalize_topology_with_mapping(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/test",
        reconstruction_version="test",
        reconstruction_metadata={"topology_source_trusted": True},
    )
    topology = SimpleNamespace(
        atom_count=normalized.topology.atom_count,
        identity_schema_version=normalized.topology.identity_schema_version,
        graph_hash=normalized.topology.graph_hash,
        mol=normalized.topology.mol,
    )

    atom_maps, mapped_smiles = _mapping_assignment_for_topology(
        template,
        topology,
        source_atom_map_numbers=list(range(1, topology.atom_count + 1)),
    )

    assert atom_maps == list(range(1, topology.atom_count + 1))
    expected = Chem.Mol(topology.mol)
    for atom_index, atom in enumerate(expected.GetAtoms(), start=1):
        atom.SetAtomMapNum(atom_index)
    assert mapped_smiles == Chem.MolToSmiles(
        expected,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )


@pytest.mark.parametrize("element", ["As", "Se", "Te"])
def test_mapped_reaction_preserves_nonmetal_heavy_atom_stereo(element: str) -> None:
    at = f"[C:1][{element}@:2]([C:3])([C:4])[C:5]>>[C:1][{element}@:2]([C:3])([C:4])[C:5]"
    aat = at.replace(f"[{element}@:2]", f"[{element}@@:2]")

    canonical_at = _canonical_mapped_reaction_smiles(_mapped_reaction_from_smiles(at))
    canonical_aat = _canonical_mapped_reaction_smiles(_mapped_reaction_from_smiles(aat))

    assert canonical_at != canonical_aat
