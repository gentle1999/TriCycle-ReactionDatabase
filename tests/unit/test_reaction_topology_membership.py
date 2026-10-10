from types import SimpleNamespace
from uuid import UUID

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reaction_topology_membership
from tricycle_reaction_db.application.services._persistence import (
    LEGACY_BULK_IMPORT_SESSION_INFO_KEY,
    SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY,
)
from tricycle_reaction_db.application.services.reaction_topology_membership import (
    _compatible_topology_candidate,
)

PROJECT_ID = UUID("00000000-0000-7000-8000-000000000001")
FORMULA_ID = UUID("00000000-0000-7000-8000-000000000002")


def _topology(stereo_agnostic_graph_hash: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        project_id=PROJECT_ID,
        formula_id=FORMULA_ID,
        atom_count=12,
        formal_charge=0,
        fragment_count=1,
        stereo_agnostic_graph_hash=stereo_agnostic_graph_hash,
    )


def test_membership_candidate_prefilter_uses_stereo_agnostic_hash() -> None:
    concrete = _topology("same-graph")

    assert _compatible_topology_candidate(_topology("same-graph"), concrete)
    assert not _compatible_topology_candidate(_topology("different-graph"), concrete)
    # NULL is retained as a compatibility fallback for rows that predate the
    # hash backfill migration or synthetic ORM fixtures.
    assert _compatible_topology_candidate(_topology(None), concrete)


def test_membership_candidate_prefilter_rejects_different_fragment_count() -> None:
    concrete = _topology("same-graph")
    candidate = _topology("same-graph")
    candidate.fragment_count = 2

    assert not _compatible_topology_candidate(candidate, concrete)


@pytest.mark.parametrize(
    "authority_key",
    [SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY, LEGACY_BULK_IMPORT_SESSION_INFO_KEY],
)
@pytest.mark.parametrize(
    ("general_smiles", "specific_smiles", "expected_method"),
    [
        ("F[C@H](Cl)Br", "F[C@H](Cl)Br", "canonical_atom_order"),
        ("FC(Cl)Br", "F[C@H](Cl)Br", "canonical_abstraction"),
        ("F[C@H](Cl)Br", "F[C@@H](Cl)Br", None),
        ("[13CH3]CO", "CCO", None),
        ("CCO", "COC", None),
    ],
)
def test_source_memberships_use_verified_correspondence_without_graph_search(
    monkeypatch: pytest.MonkeyPatch,
    authority_key: str,
    general_smiles: str,
    specific_smiles: str,
    expected_method: str | None,
) -> None:
    general = Chem.AddHs(Chem.MolFromSmiles(general_smiles))
    specific = Chem.AddHs(Chem.MolFromSmiles(specific_smiles))
    for molecule in (general, specific):
        for atom in molecule.GetAtoms():
            atom.SetNoImplicit(True)
    specific = Chem.RenumberAtoms(specific, list(reversed(range(specific.GetNumAtoms()))))
    original = (general.ToBinary(), specific.ToBinary())
    monkeypatch.setattr(
        reaction_topology_membership, "topology_abstraction_mapping_witness", lambda *_args: None
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("source imports must never search RDKit graph matches")

    monkeypatch.setattr(reaction_topology_membership, "find_topology_matches", forbidden)
    matches, enumerated, method = reaction_topology_membership._topology_mapping_evidence(
        SimpleNamespace(info={authority_key: True}),
        SimpleNamespace(mol=specific),
        SimpleNamespace(mol=general),
    )
    assert not enumerated
    assert (general.ToBinary(), specific.ToBinary()) == original
    if expected_method is None:
        assert matches == ()
    else:
        assert method == expected_method
        assert len(matches) == 1
        mapping = matches[0]
        assert sorted(mapping) == list(range(general.GetNumAtoms()))
        assert mapping != tuple(range(general.GetNumAtoms()))
        for general_index, specific_index in enumerate(mapping):
            a = general.GetAtomWithIdx(general_index)
            b = specific.GetAtomWithIdx(specific_index)
            assert (a.GetAtomicNum(), a.GetIsotope()) == (b.GetAtomicNum(), b.GetIsotope())


def test_source_membership_prefers_existing_dag_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    witness = (2, 0, 1)
    monkeypatch.setattr(
        reaction_topology_membership,
        "topology_abstraction_mapping_witness",
        lambda *_args: witness,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verified DAG evidence must be reused")

    monkeypatch.setattr(reaction_topology_membership, "find_topology_matches", forbidden)
    monkeypatch.setattr(reaction_topology_membership, "canonical_atom_index_mapping", forbidden)
    assert reaction_topology_membership._topology_mapping_evidence(
        SimpleNamespace(info={SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY: True}),
        SimpleNamespace(),
        SimpleNamespace(),
    ) == ((witness,), False, "stereo_abstraction_dag")
