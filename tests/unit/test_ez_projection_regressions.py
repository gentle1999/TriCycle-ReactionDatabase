"""Real partial-parse regressions: frozen chemistry survives E/Z projection."""

import json
from pathlib import Path

import pytest
from rdkit import Chem

from tricycle_reaction_db.ingestion import normalization as normalization

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ez_projection"


def source_graph(filename):
    data = json.loads((FIXTURES / f"{filename}.json").read_text())
    builder = Chem.RWMol()
    for number, isotope, charge, radicals, hydrogens, no_implicit, aromatic, chiral in data[
        "atoms"
    ]:
        atom = Chem.Atom(number)
        atom.SetIsotope(isotope)
        atom.SetFormalCharge(charge)
        atom.SetNumRadicalElectrons(radicals)
        atom.SetNumExplicitHs(hydrogens)
        atom.SetNoImplicit(no_implicit)
        atom.SetIsAromatic(aromatic)
        atom.SetChiralTag(Chem.ChiralType.values[chiral])
        builder.AddAtom(atom)
    for begin, end, kind, aromatic, _stereo, _controls in data["bonds"]:
        builder.AddBond(begin, end, Chem.BondType.names[kind])
        builder.GetBondBetweenAtoms(begin, end).SetIsAromatic(aromatic)
    mol = builder.GetMol()
    for begin, end, _kind, _aromatic, stereo, controls in data["bonds"]:
        bond = mol.GetBondBetweenAtoms(begin, end)
        if controls:
            bond.SetStereoAtoms(*controls)
        bond.SetStereo(Chem.BondStereo.values[stereo])
    mol.UpdatePropertyCache(strict=False)
    return mol


@pytest.mark.parametrize("filename", ["109494", "112574", "121567", "131267", "133279"])
@pytest.mark.parametrize("mapped", [False, True])
def test_real_ez_projection_preserves_frozen_source_and_is_stable(filename, mapped):
    source = source_graph(filename)
    for atom in source.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    before = source.ToBinary(Chem.PropertyPickleOptions.AllProps)
    projected = normalization.ensure_serializable_double_bond_stereochemistry(
        source, preserve_atom_maps=mapped
    )
    assert source.ToBinary(Chem.PropertyPickleOptions.AllProps) == before
    assert normalization._stereo_signatures_match(
        normalization._e_z_stereo_signature(projected, preserve_atom_maps=True),
        normalization._e_z_stereo_signature(source, preserve_atom_maps=True),
    )
    assert [a.GetNumRadicalElectrons() for a in projected.GetAtoms()] == [
        a.GetNumRadicalElectrons() for a in source.GetAtoms()
    ]
    expected = normalization.serialize_molecule_smiles(source, preserve_atom_maps=mapped)
    assert normalization.serialize_molecule_smiles(projected, preserve_atom_maps=mapped) == expected
    # Atom traversal and the last parsed control pair must not choose an identity.
    for order in (
        list(reversed(range(source.GetNumAtoms()))),
        list(range(1, source.GetNumAtoms())) + [0],
    ):
        reordered = Chem.RenumberAtoms(source, order)
        assert (
            normalization.serialize_molecule_smiles(reordered, preserve_atom_maps=mapped)
            == expected
        )


def test_validated_round_trip_keeps_radical_and_molgr_atom_evidence():
    source = source_graph("112574")
    for atom in source.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
        atom.SetIntProp("_MolGR_test_electronic_evidence", atom.GetIdx() + 100)
    _smiles, round_trip = normalization._serialize_molecule_smiles_once(
        source, preserve_atom_maps=True
    )
    by_map = {a.GetAtomMapNum(): a for a in source.GetAtoms()}
    for atom in round_trip.GetAtoms():
        original = by_map[atom.GetAtomMapNum()]
        assert atom.GetNumRadicalElectrons() == original.GetNumRadicalElectrons()
        assert atom.GetIntProp("_MolGR_test_electronic_evidence") == original.GetIntProp(
            "_MolGR_test_electronic_evidence"
        )


def test_redundant_tag_handling_does_not_accept_new_stereogenic_assignment():
    source = Chem.MolFromSmiles("[F:1][CH:2]=[CH:3][F:4]")
    with pytest.raises(ValueError, match="E/Z control-atom relationship"):
        normalization._validate_smiles_round_trip(
            source,
            "[F:1]/[CH:2]=[CH:3]/[F:4]",
            None,
            preserve_atom_maps=True,
            retain_atom_maps=True,
            isomeric_smiles=True,
        )


@pytest.mark.parametrize("changed", ["[F:1]/[CH:2]=[CH:3]\\[F:4]", "[F:1][CH:2]=[CH:3][F:4]"])
def test_round_trip_still_rejects_flipped_or_missing_source_ez(changed):
    source = Chem.MolFromSmiles("[F:1]/[CH:2]=[CH:3]/[F:4]")
    with pytest.raises(ValueError, match="E/Z control-atom relationship"):
        normalization._validate_smiles_round_trip(
            source,
            changed,
            None,
            preserve_atom_maps=True,
            retain_atom_maps=True,
            isomeric_smiles=True,
        )


def test_real_ts_participants_ignore_only_redundant_coordination_ring_ez():
    from types import SimpleNamespace

    from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
        _mapped_reaction_matches_participant_projection,
    )
    from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

    data = json.loads((FIXTURES / "119819-projections.json").read_text())
    participants = [
        SimpleNamespace(side=Side(row["side"]), mapped_smiles=row["smiles"])
        for row in data["participants"]
    ]
    assert _mapped_reaction_matches_participant_projection(data["reaction"], participants)
    product = next(row for row in participants if row.side == Side.PRODUCT)
    # Flip the independently assigned 7=9 bond while leaving redundant ring
    # tags and the complete atom-map inventory untouched.
    product.mapped_smiles = product.mapped_smiles.replace("(/[H:10])", "(\\[H:10])")
    assert not _mapped_reaction_matches_participant_projection(data["reaction"], participants)
