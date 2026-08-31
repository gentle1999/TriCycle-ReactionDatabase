from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from rdkit import Chem

from tricycle_reaction_db.application.dtos import (
    CreateReactionCommand,
    LogicalReactionParticipantRecord,
    ManifestArtifactBindingRecord,
    MappedReactionNodeGeometryMappingRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services.reactions import (
    _canonical_mapped_reaction_smiles,
    _mapped_reaction_from_smiles,
    _mapping_assignment_for_topology,
    mapped_smiles_for_topology,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
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
