from typing import Any

from fastapi.routing import APIRoute
from graphql import build_schema
from nexusx import build_compose_schema  # type: ignore[import-untyped]
from starlette.routing import Mount

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.nexusx import config, playground_config


def _request_properties(openapi: dict[str, Any], path: str) -> set[str]:
    paths = openapi["paths"]
    assert isinstance(paths, dict)
    operation = paths[path]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    reference = schema["$ref"]
    name = reference.rsplit("/", 1)[-1]
    components = openapi["components"]
    return set(components["schemas"][name]["properties"])


def _graphql_field(sdl: str, field_name: str) -> str:
    start = sdl.index(f"  {field_name}(")
    return sdl[start : sdl.index("\n", start)]


def test_nexusx_exposes_only_the_explicit_reaction_mutation() -> None:
    schema = build_compose_schema(config)
    sdl = schema.render_sdl()
    build_schema(sdl)

    assert schema.has_mutation is True
    assert "type Mutation" in sdl
    assert [service.__name__ for service in config.services] == [
        "SystemService",
        "ArtifactQueryService",
        "ArtifactIngestionQueryService",
        "MolecularFormulaQueryService",
        "MolecularFormulaDetailQueryService",
        "MolecularTopologyQueryService",
        "MolecularTopologyDetailQueryService",
        "MolecularTopologyDerivationQueryService",
        "GeometryQueryService",
        "CalculationProtocolQueryService",
        "CalculationSegmentQueryService",
        "ParseRevisionQueryService",
        "TransitionStateInferenceQueryService",
        "ScientificArrayQueryService",
        "StorageGarbageCollectionQueryService",
        "ReactionEnergyQueryService",
        "LogicalReactionQueryService",
        "MappedReactionQueryService",
        "CalculationQueryService",
        "CalculationResultQueryService",
        "WorkflowManifestQueryService",
        "ReactionCommandService",
    ]
    # Project scoping is additive: existing GraphQL callers can continue to omit
    # the optional argument while route-driven clients pin a detail to one project.
    logical_reaction_field = _graphql_field(sdl, "get_logical_reaction")
    assert logical_reaction_field.startswith("  get_logical_reaction(logical_reaction_id: UUID!")
    assert "project_id: UUID = null" in logical_reaction_field
    mapped_reaction_detail_field = _graphql_field(sdl, "get_mapped_reaction")
    assert mapped_reaction_detail_field.startswith(
        "  get_mapped_reaction(mapped_reaction_id: UUID!"
    )
    assert "project_id: UUID = null" in mapped_reaction_detail_field
    assert "create_reaction(" in sdl
    assert "reaction: String!" in sdl
    assert "CreateReactionCommand" not in sdl
    calculation_detail_field = _graphql_field(sdl, "get_calculation_frame")
    assert calculation_detail_field.startswith("  get_calculation_frame(frame_id: UUID!")
    assert "project_id: UUID = null" in calculation_detail_field
    assert "search_formulas(minimum_counts: [Int]!" in sdl
    topology_search_field = _graphql_field(sdl, "search_topologies")
    assert topology_search_field.startswith(
        "  search_topologies(project_id: UUID = null, topology_id: UUID = null"
    )
    assert "similarity_metric: SimilarityMetric! = tanimoto" in sdl
    assert "similarity_score: Float" in sdl
    assert "ScientificArraySummary" in sdl
    assert "data:" not in sdl

    assert {
        "logical_reaction_id: UUID = null",
        "calculation_frame_id: UUID = null",
        "minimum_imaginary_frequency_cm1: Float = null",
    } <= set(_graphql_field(sdl, "list_transition_state_inferences").split(", "))
    calculation_field = _graphql_field(sdl, "list_calculation_frames")
    assert "segment_index: Int = null" in calculation_field
    assert "minimum_negative_frequency_count: Int = null" in calculation_field
    assert "maximum_lowest_frequency_cm1: Float = null" in calculation_field
    assert "shape: [Int!] = null" in _graphql_field(sdl, "list_scientific_arrays")
    geometry_field = _graphql_field(sdl, "list_geometries")
    assert "topology_derivation_id: UUID = null" in geometry_field
    assert "reaction_node_role: String = null" in geometry_field
    assert "imaginary_frequency_status: String = null" in geometry_field
    mapped_reaction_field = _graphql_field(sdl, "list_mapped_reactions")
    assert "project_id: UUID = null" in mapped_reaction_field
    assert "minimum_transition_state_geometry_count: Int = null" in mapped_reaction_field
    assert "created_after: DateTime = null" in mapped_reaction_field


def test_direct_playground_exposes_only_the_small_read_only_catalog() -> None:
    service_names = {service.__name__ for service in playground_config.services}
    assert service_names == {"SystemService", "GraphQLCatalogService"}


def test_fastapi_mounts_all_allowlisted_nexusx_transports() -> None:
    application = create_app()
    expected_paths = {
        "/api/system_service/info",
        "/api/artifact_query_service/list_artifacts",
        "/api/artifact_query_service/get_artifact",
        "/api/artifact_ingestion_query_service/list_artifact_ingestions",
        "/api/molecular_formula_query_service/search_formulas",
        "/api/molecular_formula_detail_query_service/get_formula",
        "/api/molecular_topology_query_service/search_topologies",
        "/api/molecular_topology_detail_query_service/get_topology",
        "/api/geometry_query_service/list_geometries",
        "/api/calculation_protocol_query_service/list_calculation_protocols",
        "/api/calculation_segment_query_service/list_calculation_segments",
        "/api/parse_revision_query_service/list_parse_revisions",
        "/api/transition_state_inference_query_service/list_transition_state_inferences",
        "/api/scientific_array_query_service/list_scientific_arrays",
        "/api/reaction_energy_query_service/get_reaction_energy_profile",
        "/api/reaction_energy_query_service/get_mapped_reaction_thermodynamics",
        "/api/logical_reaction_query_service/list_logical_reactions",
        "/api/logical_reaction_query_service/get_logical_reaction",
        "/api/mapped_reaction_query_service/get_mapped_reaction",
        "/api/mapped_reaction_query_service/list_mapped_reactions",
        "/api/reaction_command_service/create_reaction",
        "/api/calculation_query_service/list_calculation_frames",
        "/api/calculation_query_service/get_calculation_frame",
        "/api/calculation_result_query_service/list_calculation_results",
        "/api/calculation_result_query_service/get_calculation_results",
        "/api/workflow_manifest_query_service/list_workflow_manifests",
        "/api/workflow_manifest_query_service/list_manifest_artifact_bindings",
        "/api/storage_garbage_collection_query_service/list_storage_gc_states",
        "/api/storage_garbage_collection_query_service/list_storage_gc_runs",
        "/api/molecular_topology_derivation_query_service/list_topology_derivations",
        "/graphql",
        "/graphql/schema",
    }
    openapi = application.openapi()

    assert {
        "logical_reaction_id",
        "calculation_frame_id",
        "minimum_imaginary_frequency_cm1",
        "maximum_imaginary_frequency_cm1",
    } <= _request_properties(
        openapi,
        "/api/transition_state_inference_query_service/list_transition_state_inferences",
    )
    assert {
        "topology_smiles",
        "topology_mol_block",
        "topology_smarts",
    } <= _request_properties(
        openapi,
        "/api/geometry_query_service/list_geometries",
    )
    assert {
        "reaction_smarts",
        "reactant_mol_block",
        "product_mol_block",
        "filter_expression",
    } <= _request_properties(
        openapi,
        "/api/logical_reaction_query_service/list_logical_reactions",
    )
    assert {
        "segment_index",
        "frame_index",
        "file_frame_index",
        "charge",
        "multiplicity",
        "minimum_frequency_count",
        "maximum_lowest_frequency_cm1",
    } <= _request_properties(openapi, "/api/calculation_query_service/list_calculation_frames")
    assert {"qm_software_version", "method_family", "solvation_model"} <= _request_properties(
        openapi,
        "/api/calculation_protocol_query_service/list_calculation_protocols",
    )
    assert {"dtype", "shape", "payload_sha256"} <= _request_properties(
        openapi,
        "/api/scientific_array_query_service/list_scientific_arrays",
    )
    assert {
        "topology_derivation_id",
        "reaction_node_role",
        "imaginary_frequency_status",
        "minimum_atom_count",
        "maximum_atom_count",
        "filter_expression",
    } <= _request_properties(openapi, "/api/geometry_query_service/list_geometries")
    assert {
        "label",
        "node_role",
        "minimum_transition_state_geometry_count",
        "maximum_geometry_count",
        "created_after",
        "created_before",
    } <= _request_properties(
        openapi,
        "/api/mapped_reaction_query_service/list_mapped_reactions",
    )

    assert expected_paths <= set(openapi["paths"])
    request_schema = openapi["components"]["schemas"]["ReactionCommandServiceCreateReactionRequest"]
    assert request_schema["required"] == ["reaction"]
    assert "reaction" in request_schema["properties"]
    assert "command" not in request_schema["properties"]
    for path in expected_paths:
        if path.startswith("/api/"):
            assert set(openapi["paths"][path]) == {"post"}

    api_routes = [route for route in application.routes if isinstance(route, APIRoute)]
    assert any(route.path == "/graphql" and "GET" in route.methods for route in api_routes)
    assert any(isinstance(route, Mount) and route.path == "/mcp" for route in application.routes)
