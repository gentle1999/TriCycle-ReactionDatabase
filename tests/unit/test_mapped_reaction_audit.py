"""The read-only audit must use the same whole-reaction identity as ingestion."""

import importlib.util
import sys
from pathlib import Path

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    parse_mapped_reaction_smiles,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_mapped_reaction_atom_mappings.py"
spec = importlib.util.spec_from_file_location("mapped_reaction_audit_script", SCRIPT)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)
_reaction_evidence = audit._reaction_evidence
_reaction_translation_exists = audit._reaction_translation_exists


def evidence(smiles, *, stereo=True, reverse=False):
    reaction = parse_mapped_reaction_smiles(smiles)
    components = {}
    for side, templates in (
        (Side.REACTANT, reaction.GetReactants()),
        (Side.PRODUCT, reaction.GetProducts()),
    ):
        components[side] = []
        for molecule in templates:
            if reverse:
                molecule = Chem.RenumberAtoms(
                    molecule, list(reversed(range(molecule.GetNumAtoms())))
                )
            components[side].append((molecule, [a.GetAtomMapNum() for a in molecule.GetAtoms()]))
    return _reaction_evidence(components, include_stereochemistry=stereo)


def test_audit_accepts_atom_reordering_and_map_renumbering():
    source = evidence("[O:1]=[C:2]=[C:3]=[S:4]>>[O:1]=[C:2]=[C:3]=[S:4]")
    target = evidence("[O:11]=[C:12]=[C:13]=[S:14]>>[O:11]=[C:12]=[C:13]=[S:14]", reverse=True)
    assert source and target
    assert _reaction_translation_exists(source, target)
    assert all(
        source.source_map_to_canonical[n] == target.source_map_to_canonical[n + 10]
        for n in range(1, 5)
    )


def test_audit_rejects_different_correspondence_with_identical_endpoint_graphs():
    source = evidence("[O:1]=[C:2]=[C:3]=[S:4]>>[O:1]=[C:2]=[C:3]=[S:4]")
    target = evidence("[O:1]=[C:2]=[C:3]=[S:4]>>[O:1]=[C:3]=[C:2]=[S:4]")
    assert source and target
    assert not _reaction_translation_exists(source, target)


def test_audit_distinguishes_stereoisomers_unless_projection_explicitly_requested():
    left = "[F:1][C@:2]([Cl:3])([Br:4])[I:5]"
    right = left.replace("C@:", "C@@:")
    source, target = evidence(f"{left}>>{left}"), evidence(f"{right}>>{right}")
    assert source and target
    assert not _reaction_translation_exists(source, target)
    source, target = (
        evidence(f"{left}>>{left}", stereo=False),
        evidence(f"{right}>>{right}", stereo=False),
    )
    assert source and target
    assert _reaction_translation_exists(source, target)


@pytest.mark.parametrize("smiles", ["[He:1]>>[Ne:1]", "[He:1]>>[He:2]", "[H:1][H:1]>>[H:1][H:1]"])
def test_audit_rejects_nonconserving_or_nonbijective_labels(smiles):
    assert evidence(smiles) is None


def direct_proof(source, target, maps, *, snapshot=None):
    from types import SimpleNamespace

    from tricycle_reaction_db.domain.enums import FrameRole

    count = source.GetNumAtoms()
    return audit._mapping_matches_source_frame(
        frame=SimpleNamespace(
            frame_role=FrameRole.SINGLE_POINT,
            observed_to_geometry=list(range(count)),
            source_atom_maps=snapshot,
        ),
        geometry=SimpleNamespace(mol=source),
        geometry_maps=maps,
        source_components={side: [(source, list(range(1, count + 1)))] for side in Side},
        target_components={side: [(target, list(range(1, count + 1)))] for side in Side},
    )


def test_audit_verifies_selected_symmetric_maps_without_renumbering(monkeypatch):
    molecule = Chem.AddHs(Chem.MolFromSmiles("C"))
    maps = [1, 3, 2, 4, 5]  # Valid selected exchange of two indistinguishable H atoms.

    def unexpected(*args, **kwargs):
        raise AssertionError("must verify the stored correspondence, not select a new one")

    monkeypatch.setattr(audit, "canonical_reaction_identity", unexpected)
    assert direct_proof(molecule, molecule, maps, snapshot=maps)[0]


def test_audit_rejects_map_vector_that_disagrees_with_source_snapshot():
    molecule = Chem.AddHs(Chem.MolFromSmiles("C"))
    okay, reason = direct_proof(molecule, molecule, [1, 3, 2, 4, 5], snapshot=[1, 2, 3, 4, 5])
    assert not okay
    assert reason == "stored-geometry-map-disagrees-with-source-snapshot"


def test_audit_rejects_ring_permutation_despite_equal_local_symmetry_classes():
    molecule = Chem.AddHs(Chem.MolFromSmiles("C1CCCCC1"))
    maps = list(range(1, molecule.GetNumAtoms() + 1))
    maps[0], maps[1] = maps[1], maps[0]
    okay, reason = direct_proof(molecule, molecule, maps, snapshot=maps)
    assert not okay
    assert reason == "stored-map-does-not-preserve-labelled-source-endpoints"


def test_audit_selected_form_still_rejects_opposite_stereo():
    source = Chem.AddHs(Chem.MolFromSmiles("F[C@H](Cl)Br"))
    target = Chem.AddHs(Chem.MolFromSmiles("F[C@@H](Cl)Br"))
    maps = list(range(1, source.GetNumAtoms() + 1))
    assert not direct_proof(source, target, maps, snapshot=maps)[0]
