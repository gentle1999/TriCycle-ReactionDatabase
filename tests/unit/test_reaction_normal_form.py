"""Persist the selected form without requiring RDKit fixed-point convergence."""

from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rdkit import rdBase

from tricycle_reaction_db.application.dtos.reactions import MappedReactionRecord
from tricycle_reaction_db.application.services import reactions
from tricycle_reaction_db.application.services.artifact_uploads import _reaction_index_snapshot
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    REACTION_INDEX_POLICY,
    CanonicalReactionIdentity,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.domain.enums import MappedReactionKind


@pytest.mark.parametrize("reuse", [False, True])
def test_persistence_remembers_selected_form_without_recanonicalizing(monkeypatch, reuse):
    smiles = "[He:1]>>[He:1]"
    identity = CanonicalReactionIdentity(smiles, {1: 1})
    record = MappedReactionRecord(
        mapped_reaction_key="selected-form",
        mapped_reaction_kind=MappedReactionKind.CURATED,
        mapped_reaction_smiles=smiles,
        mapping_hash=sha256(smiles.encode()).hexdigest(),
    )
    project = uuid4()
    topology = SimpleNamespace(id=uuid4(), project_id=project)
    logical = SimpleNamespace(
        id=uuid4(),
        project_id=project,
        participants=[
            SimpleNamespace(side=side, participant_index=0, topology_id=topology.id)
            for side in (Side.REACTANT, Side.PRODUCT)
        ],
    )
    existing = SimpleNamespace(
        id=uuid4(), mapped_reaction_smiles=smiles, normalization_metadata=None
    )
    added = []
    session = SimpleNamespace(
        exec=lambda _: SimpleNamespace(first=lambda: existing if reuse else None),
        add=added.append,
    )

    def unexpected_recanonicalization(*args, **kwargs):
        pytest.fail("selected output must not be standardized again")

    monkeypatch.setattr(reactions, "canonical_reaction_identity", unexpected_recanonicalization)
    monkeypatch.setattr(reactions, "_acquire_identity_locks", lambda *args: None)
    monkeypatch.setattr(reactions, "_resolve_topology_value", lambda *args: topology)
    monkeypatch.setattr(reactions, "source_atom_mapping_is_authoritative", lambda _: True)
    monkeypatch.setattr(reactions, "persist_mapped_reaction_participant", lambda *a, **k: None)
    monkeypatch.setattr(
        reactions, "_new_entity", lambda *a, **kwargs: SimpleNamespace(id=uuid4(), **kwargs)
    )
    monkeypatch.setattr(reactions, "_flush_new_entity", lambda *a, **k: None)
    monkeypatch.setattr(reactions, "_mark_new_mapped_reaction_in_cache", lambda *a: None)
    result = reactions.persist_mapped_reaction(
        session,
        logical,
        record,
        canonical_identity=identity,
        source_atom_maps_by_template={(side, 0): [1] for side in (Side.REACTANT, Side.PRODUCT)},
        topology_ids_by_template={(side, 0): topology.id for side in (Side.REACTANT, Side.PRODUCT)},
        precomputed_mapped_smiles_by_template={
            (side, 0): "[He:1]" for side in (Side.REACTANT, Side.PRODUCT)
        },
        source_mapped_reaction_smiles=smiles,
    )
    assert result.mapped_reaction_smiles == smiles
    assert result.normalization_metadata == {
        "policy": REACTION_INDEX_POLICY,
        "rdkit_version": rdBase.rdkitVersion,
        "selected_mapped_reaction_smiles": smiles,
    }
    if reuse:
        assert result is existing
        assert added == [existing]


def test_source_snapshot_uses_actual_bound_form_and_copies_maps():
    stored = SimpleNamespace(mapped_reaction_smiles="[He:2].[Ne:1]>>[He:2].[Ne:1]")
    maps = [2, 1]
    snapshot = _reaction_index_snapshot(
        SimpleNamespace(get=lambda *args: stored),
        mapped_reaction_id=uuid4(),
        source_atom_maps=maps,
    )
    maps.reverse()
    assert snapshot["canonical_mapped_reaction_smiles"] == stored.mapped_reaction_smiles
    assert snapshot["source_atom_map_numbers"] == [2, 1]
    assert snapshot["reaction_index_policy"] == REACTION_INDEX_POLICY


@pytest.mark.parametrize("maps", [[], [1, 1], [0, 1], [1, 3]])
def test_source_snapshot_rejects_incomplete_permutation(maps):
    with pytest.raises(ValueError, match="complete reaction atom permutation"):
        _reaction_index_snapshot(
            SimpleNamespace(get=lambda *args: SimpleNamespace(mapped_reaction_smiles="unused")),
            mapped_reaction_id=uuid4(),
            source_atom_maps=maps,
        )


@pytest.mark.parametrize("queue", ["orm", "bulk"])
def test_source_snapshot_accepts_unflushed_selected_reaction(queue):
    from tricycle_reaction_db.db.models import MappedReaction

    reaction = MappedReaction(
        id=uuid4(),
        logical_reaction_id=uuid4(),
        project_id=uuid4(),
        mapped_reaction_key="pending",
        mapped_reaction_kind=MappedReactionKind.OTHER,
        mapped_reaction_smiles="[He:1]>>[He:1]",
        mapping_hash="a" * 64,
    )
    session = SimpleNamespace(
        get=lambda *args: None,
        new=[reaction] if queue == "orm" else [],
        info={"_fast_pending_entities": [reaction]} if queue == "bulk" else {},
    )
    snapshot = _reaction_index_snapshot(
        session, mapped_reaction_id=reaction.id, source_atom_maps=[1]
    )
    assert snapshot["canonical_mapped_reaction_smiles"] == reaction.mapped_reaction_smiles


def test_source_snapshot_does_not_take_another_pending_reaction():
    with pytest.raises(RuntimeError, match="missing mapped reaction"):
        _reaction_index_snapshot(
            SimpleNamespace(get=lambda *args: None, new=[], info={}),
            mapped_reaction_id=uuid4(),
            source_atom_maps=[1],
        )
