import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services.queries import reaction_smarts_from_mol_blocks


def _mol_block(smiles: str) -> str:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    return Chem.MolToMolBlock(molecule)


def test_reaction_structure_query_is_canonicalized_from_mol_blocks() -> None:
    assert reaction_smarts_from_mol_blocks(_mol_block("C=C"), _mol_block("CC")) == "C=C>>CC"


@pytest.mark.parametrize(
    ("reactant_mol_block", "product_mol_block", "expected"),
    (
        (_mol_block("C=C"), None, "C=C>>"),
        (None, _mol_block("CC"), ">>CC"),
    ),
)
def test_reaction_structure_query_supports_single_sides(
    reactant_mol_block: str | None,
    product_mol_block: str | None,
    expected: str,
) -> None:
    assert reaction_smarts_from_mol_blocks(reactant_mol_block, product_mol_block) == expected


def test_reaction_structure_query_rejects_invalid_mol_block() -> None:
    with pytest.raises(ValueError, match="product_mol_block must contain a valid molecule"):
        reaction_smarts_from_mol_blocks(_mol_block("C=C"), "not a mol block")
