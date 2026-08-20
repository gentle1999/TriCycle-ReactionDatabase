import asyncio
from uuid import uuid4

import pytest

from tricycle_reaction_db.application.dtos import (
    MappedReactionDetail,
    MappedReactionEdgeView,
    MappedReactionNodeView,
    NodeAdditivePropertiesView,
)
from tricycle_reaction_db.application.services import ReactionEnergyQueryService
from tricycle_reaction_db.application.services.queries import MappedReactionQueryService


def _node(
    *,
    node_key: str,
    node_index: int,
    role: str,
    energy: float,
) -> MappedReactionNodeView:
    return MappedReactionNodeView(
        id=uuid4(),
        node_key=node_key,
        node_index=node_index,
        role=role,
        geometries=[],
        additive_properties=NodeAdditivePropertiesView(
            component_count=1,
            policy_version="test-v1",
            source_levels_compatible=True,
            electronic_energy_hartree=energy,
            gibbs_free_energy_hartree=energy,
        ),
    )


def test_reaction_energy_profile_calculates_relative_energies_and_barriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped_reaction_id = uuid4()
    logical_reaction_id = uuid4()
    source = _node(node_key="reactants", node_index=0, role="reactant", energy=-100.0)
    product = _node(node_key="products", node_index=1, role="product", energy=-99.99)
    transition_state = _node(
        node_key="transition-state",
        node_index=2,
        role="transition_state",
        energy=-99.98,
    )
    edge = MappedReactionEdgeView(
        id=uuid4(),
        edge_key="elementary-step",
        source_node_id=source.id,
        target_node_id=product.id,
        transition_state_node_id=transition_state.id,
        edge_kind="elementary_step",
    )
    reaction = MappedReactionDetail(
        id=mapped_reaction_id,
        logical_reaction_id=logical_reaction_id,
        mapped_reaction_key="test-path",
        mapped_reaction_kind="curated",
        mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
        mapping_hash="a" * 64,
        reaction_structural_bfp_schema_version="reaction-structural-bfp-r5-v1",
        reaction_key="test-reaction",
        participants=[],
        nodes=[source, product, transition_state],
        edges=[edge],
    )

    async def get_mapped_reaction(
        *,
        mapped_reaction_id: object,
    ) -> MappedReactionDetail:
        assert mapped_reaction_id == reaction.id
        return reaction

    monkeypatch.setattr(
        MappedReactionQueryService,
        "get_mapped_reaction",
        staticmethod(get_mapped_reaction),
    )
    profile = asyncio.run(
        ReactionEnergyQueryService.get_reaction_energy_profile(
            mapped_reaction_id=mapped_reaction_id,
            energy_kind="gibbs_free_energy_hartree",
        )
    )

    assert profile is not None
    assert profile.reference_node_id == source.id
    assert [point.relative_energy_kcal_mol for point in profile.points] == pytest.approx(
        [0.0, 6.275095, 12.550189]
    )
    assert profile.edges[0].reaction_energy_kcal_mol == pytest.approx(6.275095)
    assert profile.edges[0].forward_barrier_kcal_mol == pytest.approx(12.550189)
    assert profile.edges[0].reverse_barrier_kcal_mol == pytest.approx(6.275095)


def test_reaction_energy_profile_rejects_unknown_energy_kind() -> None:
    with pytest.raises(ValueError, match="unsupported energy_kind"):
        asyncio.run(
            ReactionEnergyQueryService.get_reaction_energy_profile(
                mapped_reaction_id=uuid4(),
                energy_kind="not-an-energy",
            )
        )
