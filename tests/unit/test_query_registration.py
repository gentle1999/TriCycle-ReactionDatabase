import asyncio

import pytest

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.nexusx import schema
from tricycle_reaction_db.application.dtos import MolecularTopologySearchQuery
from tricycle_reaction_db.application.services import CalculationResultQueryService


def test_nexusx_registers_complete_read_query_surface() -> None:
    paths = set(create_app().openapi()["paths"])
    assert {
        "/api/geometry_query_service/list_geometries",
        "/api/geometry_query_service/get_geometry",
        "/api/calculation_protocol_query_service/list_calculation_protocols",
        "/api/artifact_ingestion_query_service/list_artifact_ingestions",
        "/api/parse_revision_query_service/list_parse_revisions",
        "/api/calculation_segment_query_service/list_calculation_segments",
        "/api/transition_state_inference_query_service/list_transition_state_inferences",
        "/api/scientific_array_query_service/list_scientific_arrays",
        "/api/reaction_energy_query_service/get_reaction_energy_profile",
        "/api/reaction_energy_query_service/get_mapped_reaction_thermodynamics",
        "/api/mapped_reaction_query_service/list_mapped_reactions",
        "/api/calculation_query_service/list_calculation_frames",
        "/api/calculation_result_query_service/list_calculation_results",
        "/api/calculation_result_query_service/get_calculation_results",
        "/api/workflow_manifest_query_service/list_workflow_manifests",
        "/api/workflow_manifest_query_service/get_workflow_manifest",
        "/api/workflow_manifest_query_service/list_manifest_artifact_bindings",
        "/api/storage_garbage_collection_query_service/list_storage_gc_states",
        "/api/storage_garbage_collection_query_service/list_storage_gc_runs",
        "/api/molecular_topology_derivation_query_service/list_topology_derivations",
        "/api/molecular_topology_derivation_query_service/get_topology_derivation",
        "/api/depictions/geometry/{geometry_id}.sdf",
        "/api/depictions/geometry/{geometry_id}.xyz",
        "/api/depictions/geometry/{geometry_id}.svg",
        "/api/depictions/topology/{topology_id}.svg",
        "/api/depictions/topology/{topology_id}.mol",
        "/api/mapped-reactions/{mapped_reaction_id}/thermodynamics",
    } <= paths

    sdl = schema.render_sdl()
    assert "get_mapped_reaction_thermodynamics" in sdl
    assert "geometry_id: UUID = null" in sdl
    assert "protocol_id: UUID = null" in sdl
    assert "reaction_smarts: String = null" in sdl
    assert "similarity_reaction_smiles: String = null" in sdl
    assert "result_kind: String = null" in sdl
    assert "molecular_orbitals: MolecularOrbitalResultView" in sdl
    assert "bound_artifact_file_id: UUID = null" in sdl
    assert "started_after: DateTime = null" in sdl
    assert "provenance_hash: String = null" in sdl


def test_topology_descriptor_ranges_are_validated() -> None:
    query = MolecularTopologySearchQuery(
        minimum_molecular_weight=100.0,
        maximum_molecular_weight=200.0,
        minimum_ring_count=1,
    )
    assert query.minimum_molecular_weight == 100.0

    try:
        MolecularTopologySearchQuery(
            minimum_tpsa=20.0,
            maximum_tpsa=10.0,
        )
    except ValueError as error:
        assert "minimum_tpsa cannot exceed maximum_tpsa" in str(error)
    else:
        raise AssertionError("reversed descriptor bounds must be rejected")


def test_advanced_result_kind_is_validated_before_query_execution() -> None:
    with pytest.raises(ValueError, match="unsupported result_kind"):
        asyncio.run(
            CalculationResultQueryService.list_calculation_results(
                result_kind="unknown",
            )
        )
