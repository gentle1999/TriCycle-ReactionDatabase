"""Regressions for coordination stereo and trusted TS participant validation."""

from types import SimpleNamespace

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    canonical_reaction_identity,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    _geometry_maps_from_creation_witness,
    _mapped_reaction_matches_participant_projection,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.ingestion.normalization import normalize_topology_with_mapping


def trusted_smiles(smiles):
    params = Chem.SmilesParserParams()
    params.removeHs = False
    params.sanitize = False
    molecule = Chem.MolFromSmiles(smiles, params)
    molecule.UpdatePropertyCache(strict=False)
    Chem.SetBondStereoFromDirections(molecule)
    return molecule


@pytest.mark.parametrize(
    "smiles",
    [
        "F[Pt@SP1](Cl)(Br)I",
        "F[As@TB4](Cl)(Br)(I)N",
        "F[Co@OH16](Cl)(Br)(I)(N)O",
    ],
)
@pytest.mark.parametrize("trusted", [False, True])
def test_non_tetrahedral_arrangement_survives_topology_normalization(smiles, trusted):
    source = Chem.AddHs(Chem.MolFromSmiles(smiles))
    count = source.GetNumAtoms()
    normalized, order = normalize_topology_with_mapping(
        source,
        add_hydrogens=False,
        reconstruction_method="molgr/test" if trusted else "rdkit/test",
        reconstruction_version="test",
    )
    maps = [0] * count
    for source_index, topology_index in enumerate(order):
        maps[topology_index] = source_index + 1
    source_components = {side: [(source, list(range(1, count + 1)))] for side in Side}
    target_components = {side: [(normalized.topology.mol, maps)] for side in Side}
    assert _geometry_maps_from_creation_witness(
        source_components, target_components, list(range(count)), {i: i for i in maps}
    ) == list(range(1, count + 1))


@pytest.mark.parametrize(
    "left,right",
    [
        ("F[Pt@SP1](Cl)(Br)I", "F[Pt@SP2](Cl)(Br)I"),
        ("F[As@TB4](Cl)(Br)(I)N", "F[As@TB5](Cl)(Br)(I)N"),
        ("F[Co@OH16](Cl)(Br)(I)(N)O", "F[Co@OH17](Cl)(Br)(I)(N)O"),
    ],
)
def test_coordination_stereoisomers_keep_distinct_reaction_identities(left, right):
    identities = []
    for smiles in (left, right):
        mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
        normalized, order = normalize_topology_with_mapping(
            mol,
            add_hydrogens=False,
            reconstruction_method="molgr/test",
            reconstruction_version="test",
        )
        maps = [0] * mol.GetNumAtoms()
        for i, j in enumerate(order):
            maps[j] = i + 1
        identities.append(
            canonical_reaction_identity(
                {side: [(normalized.topology.mol, maps)] for side in Side}
            ).smiles
        )
    assert identities[0] != identities[1]


# Exact projections of failed source files 101341.log and 103051.log.
@pytest.mark.parametrize(
    "reaction",
    [
        "[c:1]1[c:2]2[c:3][c:4]1=2>>[c:1]12[c:2][c:3]=1[c:4]2",
        "[H:1][O:2][C:3]1=[O:4]->[Al@@+3:5]2(<-[O-:6][C:7](=[O:8])[C:9]([H:10])([H:11])[O-:12]->2)<-[O-:13][C:14]1([H:15])[H:16]>>[H:1][O:2][C@@:3]1([O-:4]->[Al@@+3:5]2(<-[O-:6][C:7](=[O:8])[C:9]([H:10])([H:11])[O-:12]->2)<-[O:13]=[C:14]1[H:15])[H:16]",
    ],
)
def test_trusted_participants_are_not_resanitized(reaction):
    participants = [
        SimpleNamespace(side=side, mapped_smiles=smiles)
        for side, smiles in zip((Side.REACTANT, Side.PRODUCT), reaction.split(">>"), strict=True)
    ]
    assert _mapped_reaction_matches_participant_projection(reaction, participants)
    # Rewriting an equivalent traversal remains valid.
    for participant in participants:
        mol = trusted_smiles(participant.mapped_smiles)
        participant.mapped_smiles = Chem.MolToSmiles(
            mol, canonical=False, rootedAtAtom=mol.GetNumAtoms() - 1
        )
    assert _mapped_reaction_matches_participant_projection(reaction, participants)


@pytest.mark.parametrize(
    "different",
    [
        "[H:1][C:2]([H:3])([H:4])[F:6]",  # missing / changed mapped atom
        "[H:1][C:2]([H:3])([H:4])[Cl:5]",  # wrong element
        "[H:1][C:2]([H:3])([H:4])[18F:5]",  # wrong isotope
    ],
)
def test_participant_validation_still_rejects_changed_inventory(different):
    original = "[H:1][C:2]([H:3])([H:4])[F:5]"
    participants = [SimpleNamespace(side=side, mapped_smiles=different) for side in Side]
    assert not _mapped_reaction_matches_participant_projection(
        original + ">>" + original, participants
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_coordination_sidecar_restores_arrangement_in_persisted_atom_order(reverse):
    from tricycle_reaction_db.domain.mol_properties import (
        mol_atom_properties,
        restore_mol_atom_properties,
    )
    from tricycle_reaction_db.ingestion.normalization import serialize_molecule_smiles

    source = Chem.AddHs(Chem.MolFromSmiles("F[Co@OH16](Cl)(Br)(I)(N)O"))
    if reverse:
        source = Chem.RenumberAtoms(source, list(reversed(range(source.GetNumAtoms()))))
    properties = mol_atom_properties(source)
    assert any(key.startswith("chiral_permutation:") for key in properties)
    restored = Chem.Mol(source.ToBinary(Chem.PropertyPickleOptions.NoProps))
    for atom in restored.GetAtoms():
        if atom.HasProp("_chiralPermutation"):
            atom.ClearProp("_chiralPermutation")
    restore_mol_atom_properties(restored, properties)
    assert serialize_molecule_smiles(restored) == serialize_molecule_smiles(source)
