from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlmodel import Session

from tricycle_reaction_db.application.services.reactions import (
    reindex_mapped_reaction_endpoint_geometry_components,
)
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionNodeRole,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.flush_count = 0
        self.lock_statement = ""

    def execute(self, statement):
        self.lock_statement = str(statement)

    def exec(self, _statement):
        return _Rows(self.rows)

    def add(self, _entity):
        pass

    def flush(self):
        self.flush_count += 1


def test_reindexes_swapped_endpoint_component_slots_in_two_phases():
    reaction_id, node_id = uuid4(), uuid4()
    node = SimpleNamespace(
        id=node_id,
        mapped_reaction_id=reaction_id,
        role=MappedReactionNodeRole.REACTANT,
    )
    participant_zero = SimpleNamespace(
        mapped_reaction_id=reaction_id,
        side=LogicalReactionParticipantSide.REACTANT,
        template_index=0,
    )
    participant_one = SimpleNamespace(
        mapped_reaction_id=reaction_id,
        side=LogicalReactionParticipantSide.REACTANT,
        template_index=1,
    )
    binding_zero = SimpleNamespace(
        id=uuid4(),
        component_key="reactant:1",
        component_index=1,
        coordinate_index=0,
    )
    binding_one = SimpleNamespace(
        id=uuid4(),
        component_key="reactant:0",
        component_index=0,
        coordinate_index=0,
    )
    session = _Session(
        [
            (binding_zero, node, participant_zero),
            (binding_one, node, participant_one),
        ]
    )

    typed_session = cast(Session, session)
    changed = reindex_mapped_reaction_endpoint_geometry_components(typed_session, reaction_id)

    assert changed == 2
    assert (binding_zero.component_key, binding_zero.component_index) == ("reactant:0", 0)
    assert (binding_one.component_key, binding_one.component_index) == ("reactant:1", 1)
    assert binding_zero.coordinate_index == binding_one.coordinate_index == 0
    assert session.flush_count == 2
    assert "LOCK TABLE mapped_reaction_node_geometry" in session.lock_statement
    assert reindex_mapped_reaction_endpoint_geometry_components(typed_session, reaction_id) == 0


def test_reindex_rejects_a_target_slot_collision_before_mutating_rows():
    reaction_id, node_id = uuid4(), uuid4()
    node = SimpleNamespace(
        id=node_id,
        mapped_reaction_id=reaction_id,
        role=MappedReactionNodeRole.REACTANT,
    )
    participants = [
        SimpleNamespace(
            mapped_reaction_id=reaction_id,
            side=LogicalReactionParticipantSide.REACTANT,
            template_index=0,
        )
        for _ in range(2)
    ]
    bindings = [
        SimpleNamespace(
            id=uuid4(),
            component_key=f"legacy:{index}",
            component_index=index,
            coordinate_index=0,
        )
        for index in range(2)
    ]
    session = _Session([(bindings[0], node, participants[0]), (bindings[1], node, participants[1])])

    with pytest.raises(ValueError, match="would collide"):
        reindex_mapped_reaction_endpoint_geometry_components(cast(Session, session), reaction_id)

    assert session.flush_count == 0
    assert [binding.component_key for binding in bindings] == ["legacy:0", "legacy:1"]
