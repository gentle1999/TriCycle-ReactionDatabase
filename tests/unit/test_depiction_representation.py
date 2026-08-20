from rdkit import Chem

from tricycle_reaction_db.api.routes.depictions import _parse_mol_block


def test_parse_mol_block_accepts_chemdoodle_header_layout() -> None:
    molecule = Chem.MolFromSmiles("CO")
    assert molecule is not None
    rdkit_block = Chem.MolToMolBlock(molecule)
    chemdoodle_block = rdkit_block.lstrip("\n")

    parsed = _parse_mol_block(chemdoodle_block)

    assert parsed is not None
    assert parsed.GetNumAtoms() == 2
