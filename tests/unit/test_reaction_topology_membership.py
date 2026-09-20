from types import SimpleNamespace
from uuid import UUID

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
