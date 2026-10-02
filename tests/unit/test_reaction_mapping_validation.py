"""Malformed labels must fail before any topology persistence or translation."""

from uuid import uuid4

import pytest

from tricycle_reaction_db.application.dtos import CreateReactionCommand
from tricycle_reaction_db.application.services import reaction_commands as module
from tricycle_reaction_db.application.services.molecular_geometry import GeometryPersistenceContext


@pytest.mark.parametrize(
    ("reaction", "message"),
    [
        ("[H:1][H]>>[H:1][H]", "either absent or complete"),
        ("[He:1].[Ne]>>[He:1].[Ne]", "either absent or complete"),
        ("[He:1]>>[He]", "either absent or complete"),
        ("[H:1][H:1]>>[H:1][H:1]", "RDKit rejected"),
        ("[He:1]>>[He:2]", "atom-map sets must match"),
    ],
)
def test_invalid_mapping_rejected_before_resolving_topologies(monkeypatch, reaction, message):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid mapping reached topology resolution")

    monkeypatch.setattr(module, "_resolve_components", unexpected)
    with pytest.raises(ValueError, match=message):
        module._create_reaction(
            None,
            CreateReactionCommand(reaction=reaction),
            topology_context=GeometryPersistenceContext(project_id=uuid4()),
        )


@pytest.mark.parametrize("sides", [[[0, 0], [0, 0]], [[1, 2], [2, 1]]])
def test_unmapped_and_complete_inputs_are_valid(sides):
    assert module._validate_atom_mapping_sides(sides) is bool(sides[0][0])


def test_duplicate_maps_rejected_by_internal_validator():
    with pytest.raises(ValueError, match="unique on each side"):
        module._validate_atom_mapping_sides([[1, 1], [1, 1]])
