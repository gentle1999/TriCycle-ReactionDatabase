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
