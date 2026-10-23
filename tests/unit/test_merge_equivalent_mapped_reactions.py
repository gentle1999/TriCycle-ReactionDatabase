"""A shared coordinate may collapse only under a complete reaction automorphism."""

import importlib.util
import sys
from pathlib import Path

import pytest
from rdkit import Chem

from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/merge_equivalent_mapped_reactions.py"
sys.path.insert(0, str(SCRIPT.parent))
try:
    spec = importlib.util.spec_from_file_location("merge_reactions_script", SCRIPT)
    assert spec and spec.loader
    merge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge)
finally:
    sys.path.pop(0)


def components(product):
    return {
        Side.REACTANT: [(Chem.MolFromSmiles("CCC"), [1, 2, 3])],
        Side.PRODUCT: [(Chem.MolFromSmiles(product), [1, 2, 3])],
    }


def test_shared_geometry_accepts_simultaneous_endpoint_symmetry():
    assert merge.equivalent_geometry_labels([1, 2, 3], [3, 2, 1], components("C=C=C"))


def test_shared_geometry_rejects_symmetry_broken_by_reaction():
    # Both terminal carbons are equivalent in propane, but only one forms the
    # new double bond. Endpoint-by-endpoint class matching loses that evidence.
    assert not merge.equivalent_geometry_labels([1, 2, 3], [3, 2, 1], components("C=CC"))


def test_shared_geometry_rejects_nonautomorphic_carbon_permutation():
    assert not merge.equivalent_geometry_labels([1, 2, 3], [2, 1, 3], components("C=C=C"))


@pytest.mark.parametrize("second", [[1, 1, 3], [1, 2], [1, 2, 4]])
def test_shared_geometry_rejects_nonbijective_labels(second):
    assert not merge.equivalent_geometry_labels([1, 2, 3], second, components("C=C=C"))


def test_inheritance_projection_preserves_the_original_stereo_graph_and_maps():
    molecule = Chem.MolFromSmiles("F[C@H](Cl)Br")
    original = Chem.MolToSmiles(molecule)
    maps = [1, 2, 3, 4]
    source = {Side.REACTANT: [(molecule, maps)], Side.PRODUCT: [(molecule, maps)]}
    projected = merge.without_stereo(source)
    assert Chem.MolToSmiles(molecule) == original
    assert "@" not in Chem.MolToSmiles(projected[Side.REACTANT][0][0])
    projected[Side.REACTANT][0][1][0] = 99
    assert maps == [1, 2, 3, 4]
