from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services.reaction_commands import (
    _align_participant_indices,
    _ResolvedComponent,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide


def component(index, identity, maps):
    return _ResolvedComponent(
        side=LogicalReactionParticipantSide.REACTANT,
        template_index=index,
        formula=None,
        topology=SimpleNamespace(id=UUID(int=identity), graph_hash=str(identity)),
        topology_atom_map_numbers=maps,
    )


def test_existing_participant_order_is_reused_without_changing_atom_maps():
    components = [component(0, 2, [3, 4]), component(1, 1, [1, 2])]
    existing = [
        SimpleNamespace(
            side=LogicalReactionParticipantSide.REACTANT,
            topology_id=UUID(int=identity),
            participant_index=index,
        )
        for index, identity in enumerate([1, 2])
    ]
    concrete, logical = _align_participant_indices(components, components, existing)
    assert [item.template_index for item in concrete] == [1, 0]
    assert [item.topology_atom_map_numbers for item in concrete] == [[3, 4], [1, 2]]
    assert concrete == logical


def test_repeated_participants_get_distinct_slots():
    components = [component(0, 1, [2]), component(1, 1, [1])]
    concrete, _ = _align_participant_indices(components, components, [])
    assert [item.template_index for item in concrete] == [1, 0]


def test_incompatible_existing_topology_is_still_rejected():
    components = [component(0, 1, [1])]
    existing = [
        SimpleNamespace(
            side=LogicalReactionParticipantSide.REACTANT,
            topology_id=UUID(int=2),
            participant_index=0,
        )
    ]
    with pytest.raises(ValueError, match="incompatible"):
        _align_participant_indices(components, components, existing)
