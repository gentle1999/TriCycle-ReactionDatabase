import pytest
from pydantic import ValidationError
from rdkit import Chem

from tricycle_reaction_db.application.dtos import MolecularTopologySearchQuery


def test_topology_search_query_accepts_formula_prefilter_and_smarts() -> None:
    query = MolecularTopologySearchQuery(
        formula_hill_formula="C2H6O",
        smarts="CO",
        minimum_substructure_matches=1,
    )

    assert query.formula_hill_formula == "C2H6O"
    assert query.smarts == "CO"


def test_topology_search_query_accepts_exact_topology_id() -> None:
    query = MolecularTopologySearchQuery(topology_id="00000000-0000-0000-0000-000000000001")

    assert str(query.topology_id) == "00000000-0000-0000-0000-000000000001"


def test_topology_search_query_normalizes_exact_smiles_to_display_identity() -> None:
    query = MolecularTopologySearchQuery(exact_smiles="OCC")

    assert query.exact_smiles == "CCO"


def test_topology_search_query_accepts_sketcher_mol_block() -> None:
    molecule = Chem.MolFromSmiles("C=C")
    assert molecule is not None
    mol_block = Chem.MolToMolBlock(molecule)

    query = MolecularTopologySearchQuery(mol_block=mol_block)

    assert query.mol_block == mol_block


def test_topology_search_query_accepts_versioned_similarity_options() -> None:
    query = MolecularTopologySearchQuery(
        similarity_smiles="OCC",
        similarity_metric="dice",
        minimum_similarity=0.8,
    )

    assert query.similarity_smiles == "CCO"
    assert query.similarity_metric == "dice"
    assert query.minimum_similarity == 0.8


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "at least one predicate"),
        (
            {"formula_id": "00000000-0000-0000-0000-000000000001", "formula_hill_formula": "CH4"},
            "conflict",
        ),
        ({"exact_smiles": "not a SMILES"}, "valid SMILES"),
        ({"mol_block": "not a mol block"}, "valid MDL mol block"),
        (
            {"exact_smiles": "C=C", "mol_block": Chem.MolToMolBlock(Chem.MolFromSmiles("C=C"))},
            "conflict",
        ),
        ({"similarity_smiles": "not a SMILES"}, "valid SMILES"),
        ({"minimum_similarity": 0.8}, "requires similarity_smiles"),
        (
            {"similarity_metric": "dice", "formula_hill_formula": "CH4"},
            "requires similarity_smiles",
        ),
        ({"smarts": "[not a SMARTS"}, "valid SMARTS"),
        ({"smarts": "CO", "minimum_substructure_matches": 0}, "greater than or equal"),
        ({"match_chirality": True, "formula_hill_formula": "CH4"}, "match_chirality requires"),
    ],
)
def test_topology_search_query_rejects_invalid_or_unbounded_requests(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        MolecularTopologySearchQuery.model_validate(payload)
