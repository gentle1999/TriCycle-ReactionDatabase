from types import SimpleNamespace
from uuid import UUID

import pytest
from rdkit import Chem

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


def test_source_authoritative_reconciliation_expands_without_discarding_source_authority(
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

    context.molecular_topologies_by_id[UUID(int=3)] = SimpleNamespace(project_id=project_id)
    expanded: list[object] = []

    def expand_preserving_authority(active_session, entity, **_kwargs):
        assert active_session.info[SOURCE_ATOM_ORDER_AUTHORITATIVE_SESSION_INFO_KEY]
        assert active_session.info["tricycle_fast_insert"] is False
        assert active_session.autoflush
        assert entity.project_id == project_id
        expanded.append(entity)
        return ()

    monkeypatch.setattr(
        reaction_mapping_resolution,
        "ensure_mapped_reactions_for_logical_reaction",
        expand_preserving_authority,
    )
    monkeypatch.setattr(
        reaction_mapping_resolution,
        "ensure_mapped_reactions_for_concrete_topology",
        expand_preserving_authority,
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
    assert len(expanded) == 2
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


def test_expansion_deduplicates_symmetric_ez_traversals_using_verified_stereo() -> None:
    project_id = UUID(int=1)
    topologies = tuple(
        SimpleNamespace(
            id=UUID(int=index),
            project_id=project_id,
            stereo_agnostic_graph_hash="same-connectivity",
            mol=Chem.AddHs(Chem.MolFromSmiles(smiles)),
        )
        for index, smiles in enumerate(
            (
                "[H][O]/[N]=[C]([Cl])/[C]([Cl])=[N]/[O][H]",
                "[H][O]/[N]=[C]([Cl])\\[C]([Cl])=[N]/[O][H]",
                "[H][O]/[N]=[C]([Cl])\\[C]([Cl])=[N]\\[O][H]",
                "[H][O]/[N]=[C]([Cl])/[C]([Cl])=[N]\\[O][H]",
            ),
            start=2,
        )
    )
    # Prefer the Geometry-backed traversal even when its UUID sorts later.
    observed_ids = [topologies[0].id, topologies[1].id, topologies[3].id]
    session = SimpleNamespace(exec=lambda _statement: SimpleNamespace(all=lambda: observed_ids))
    source_molecules = [row.mol.ToBinary() for row in topologies]
    representatives = reaction_mapping_resolution._strict_topology_representatives(
        session, topologies
    )
    assert {row.id for row in representatives} == set(observed_ids)
    assert [row.mol.ToBinary() for row in topologies] == source_molecules
