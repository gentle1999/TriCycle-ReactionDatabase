from types import SimpleNamespace
from uuid import UUID

import pytest

from tricycle_reaction_db.application.services import (
    molecular_geometry,
    molop_artifact_ingestion,
    reaction_mapping_resolution,
    topology_abstraction,
)
from tricycle_reaction_db.application.services._persistence import (
    SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
)


class _Session:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.autoflush = True
        self.new: list[object] = []


def test_source_authoritative_reconciliation_skips_mapping_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = UUID("00000000-0000-7000-8000-000000000001")
    session = _Session()
    context = GeometryPersistenceContext(
        project_id=project_id,
        source_atom_order_authoritative=True,
    )
    context.logical_reactions_to_resolve_mappings[UUID(int=2)] = SimpleNamespace(
        project_id=project_id,
    )
    context.topologies_to_resolve_reactions.add(UUID(int=3))

    def fail_if_expansion_runs(*_args, **_kwargs):
        raise AssertionError("source-order mappings must not enter graph-based expansion")

    monkeypatch.setattr(
        reaction_mapping_resolution,
        "ensure_mapped_reactions_for_logical_reaction",
        fail_if_expansion_runs,
    )
    monkeypatch.setattr(
        reaction_mapping_resolution,
        "ensure_mapped_reactions_for_concrete_topology",
        fail_if_expansion_runs,
    )
    monkeypatch.setattr(molop_artifact_ingestion, "_attach_pending_entities", lambda _session: None)
    monkeypatch.setattr(molop_artifact_ingestion, "_flush_if_needed", lambda _session: None)

    def assert_source_authority(session, *_args, **_kwargs):
        assert session.info.get(SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY)
        return set()

    monkeypatch.setattr(
        molop_artifact_ingestion,
        "reconcilable_geometry_ids",
        assert_source_authority,
    )
    monkeypatch.setattr(
        molop_artifact_ingestion,
        "preload_reconciliation_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        molop_artifact_ingestion,
        "mark_mapped_reactions_thermodynamics_dirty",
        lambda *_args, **_kwargs: None,
    )

    assert molop_artifact_ingestion.reconcile_molop_geometry_context(session, context) == set()
    assert context.logical_reactions_to_resolve_mappings == {}
    assert context.topologies_to_resolve_reactions == set()
    assert SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY not in session.info


def test_source_authoritative_topology_skips_abstraction_upstream_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = SimpleNamespace()
    context = GeometryPersistenceContext(
        project_id=UUID("00000000-0000-7000-8000-000000000001"),
        source_atom_order_authoritative=True,
    )

    def fail_if_matching_runs(*_args, **_kwargs):
        raise AssertionError("source-order imports must skip topology graph matching")

    monkeypatch.setattr(
        topology_abstraction,
        "ensure_topology_upstreams",
        fail_if_matching_runs,
    )

    assert molecular_geometry._register_topology_upstreams(
        _Session(), topology, context=context
    ) == (topology,)
