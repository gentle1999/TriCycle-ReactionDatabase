from rdkit import Chem

from tricycle_reaction_db.ingestion import normalization as n


def test_conjugated_coordination_keeps_both_double_bond_controls():
    params = Chem.SmilesParserParams()
    params.removeHs = False
    params.sanitize = False
    mol = Chem.MolFromSmiles(
        "[Pd+2:1]<-[C-:2](=[C:3]([B-:4])[C:5])[C:6](=[C:7]([H:8])[C:9])[H:10]",
        params,
    )
    mol.UpdatePropertyCache(strict=False)
    indices = {atom.GetAtomMapNum(): atom.GetIdx() for atom in mol.GetAtoms()}
    for a, b, c, d in [(2, 3, 6, 5), (6, 7, 2, 9)]:
        bond = mol.GetBondBetweenAtoms(indices[a], indices[b])
        bond.SetStereoAtoms(indices[c], indices[d])
        bond.SetStereo(Chem.BondStereo.STEREOE)
    smiles = n._serialize_molecule_smiles_once(mol, preserve_atom_maps=True)
    signature = n._validate_smiles_round_trip(
        mol,
        smiles,
        None,
        preserve_atom_maps=True,
        retain_atom_maps=True,
        isomeric_smiles=True,
    )
    assert len(signature) == 2
    assert "<-" in smiles


def test_direction_repair_does_not_guess_unreported_atom_order():
    mol = Chem.MolFromSmiles("F/C=C/F")
    assert n._restore_tree_bond_directions(mol, "FC=CF", None) == "FC=CF"


def test_direction_repair_leaves_unsupported_atom_syntax_to_writer():
    mol = Chem.MolFromSmiles("F/C=C/F")
    assert n._restore_tree_bond_directions(mol, "FC=CF", [0, 1, 2, 3]) == "FC=CF"


def test_explicit_kekule_orders_do_not_keep_stale_aromatic_bond_flags():
    mol = Chem.MolFromSmiles("c1ccccc1")
    Chem.Kekulize(mol, clearAromaticFlags=False)
    orders = [bond.GetBondType() for bond in mol.GetBonds()]
    normalized = n.normalize_molgr_stereochemistry(mol)
    assert [bond.GetBondType() for bond in normalized.GetBonds()] == orders
    assert not any(bond.GetIsAromatic() for bond in normalized.GetBonds())
    assert all(atom.GetIsAromatic() for atom in normalized.GetAtoms())
    assert n.serialize_molecule_smiles(normalized)
