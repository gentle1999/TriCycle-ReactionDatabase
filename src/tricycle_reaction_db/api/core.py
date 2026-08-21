"""NexusX Core API routes built from explicit DTO subsets and resolvers."""

import re
from typing import Annotated, Any, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from nexusx import DefineSubset, ErDiagram, ErManager  # type: ignore[import-untyped]

from tricycle_reaction_db.api.authentication import get_optional_principal
from tricycle_reaction_db.application.dtos import (
    ArtifactPage,
    ArtifactPreview,
    ArtifactSummary,
    CalculationFrameDetail,
    CalculationFramePage,
    LogicalReactionDetail,
    LogicalReactionPage,
    MappedReactionDetail,
    MappedReactionPage,
    MappedReactionThermodynamics,
    MappedReactionThermodynamicStatistics,
    MolecularFormulaPage,
    MolecularFormulaRangeQuery,
    MolecularTopologySearchPage,
    MolecularTopologySearchQuery,
    ReactionEnergyProfile,
    ScientificArrayPreview,
)
from tricycle_reaction_db.application.services import (
    ArtifactContentService,
    ArtifactForbiddenError,
    ArtifactNotFoundError,
    ArtifactObjectIntegrityError,
    ArtifactPreviewUnsupportedError,
    ArtifactQueryService,
    ArtifactUnavailableError,
    AuthenticatedPrincipal,
    CalculationQueryService,
    LogicalReactionQueryService,
    MappedReactionQueryService,
    MolecularFormulaQueryService,
    MolecularTopologyQueryService,
    ReactionEnergyQueryService,
    ReactionThermodynamicAnalyticsService,
    ScientificArrayContentService,
    ScientificArrayNotFoundError,
    ScientificArrayPayloadTooLargeError,
    iter_artifact_download,
)
from tricycle_reaction_db.db.models import MolecularFormula, MolecularTopology
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    FrameRole,
    MappedReactionKind,
    OptimizationStatus,
    ReactionClass,
    SCFStatus,
    SimilarityMetric,
    StorageStatus,
)

CoreLimit = Annotated[int, Query(ge=1, le=200)]
CoreOffset = Annotated[int, Query(ge=0)]
PreviewBytes = Annotated[int, Query(ge=1024, le=512 * 1024)]
ArrayPayloadBytes = Annotated[int, Query(ge=1, le=256 * 1024 * 1024)]
ArrayPreviewElements = Annotated[int, Query(ge=1, le=4096)]
OptionalPrincipal = Annotated[
    AuthenticatedPrincipal | None,
    Depends(get_optional_principal),
]


class MolecularFormulaCoreDTO(DefineSubset):  # type: ignore[misc]
    __subset__ = (
        MolecularFormula,
        ("id", "hill_formula", "atom_count", "composition_hash"),
    )


class MolecularTopologyCoreDTO(DefineSubset):  # type: ignore[misc]
    __subset__ = (
        MolecularTopology,
        (
            "id",
            "formula_id",
            "canonical_isomeric_smiles",
            "graph_hash",
            "atom_count",
            "heavy_atom_count",
            "formal_charge",
            "radical_electron_count",
            "fragment_count",
            "stereo_status",
            "sanitization_status",
            "sanitization_error",
        ),
    )
    formula: MolecularFormulaCoreDTO | None = None


er_manager = ErManager(
    entities=[MolecularFormula, MolecularTopology],
    session_factory=session_factory,
)
CoreResolver = er_manager.create_resolver()

router = APIRouter(prefix="/api", tags=["Core API"])


@router.get("/topologies", response_model=list[MolecularTopologyCoreDTO])
async def list_topologies(
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> list[MolecularTopologyCoreDTO]:
    """List only topologies rooted in artifacts visible to this request."""

    page = await MolecularTopologyQueryService.list_visible_topologies(
        limit=limit,
        offset=offset,
    )
    topology_dto = cast(Any, MolecularTopologyCoreDTO)
    formula_dto = cast(Any, MolecularFormulaCoreDTO)
    return [
        topology_dto(
            id=item.id,
            formula_id=item.formula_id,
            canonical_isomeric_smiles=item.canonical_isomeric_smiles,
            graph_hash=item.graph_hash,
            atom_count=item.atom_count,
            heavy_atom_count=item.heavy_atom_count,
            formal_charge=item.formal_charge,
            radical_electron_count=item.radical_electron_count,
            fragment_count=item.fragment_count,
            stereo_status=item.stereo_status,
            sanitization_status=item.sanitization_status,
            sanitization_error=item.sanitization_error,
            formula=formula_dto(
                id=item.formula_id,
                hill_formula=item.hill_formula,
                atom_count=item.atom_count,
                composition_hash=item.formula_composition_hash,
            ),
        )
        for item in page.items
    ]


@router.post("/formulas/search", response_model=MolecularFormulaPage)
async def search_molecular_formulas(
    ranges: MolecularFormulaRangeQuery,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> MolecularFormulaPage:
    """Search formula identities by a 118-dimensional inclusive count range."""

    return cast(
        MolecularFormulaPage,
        await MolecularFormulaQueryService.search_formulas(
            minimum_counts=ranges.minimum_counts,
            maximum_counts=ranges.maximum_counts,
            limit=limit,
            offset=offset,
        ),
    )


@router.post("/topologies/search", response_model=MolecularTopologySearchPage)
async def search_molecular_topologies(
    search: MolecularTopologySearchQuery,
    project_id: UUID | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> MolecularTopologySearchPage:
    """Search Formula-prefiltered graphs with exact, SMARTS, and indexed similarity predicates."""

    return cast(
        MolecularTopologySearchPage,
        await MolecularTopologyQueryService.search_topologies(
            project_id=project_id,
            **search.model_dump(),
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/artifacts", response_model=ArtifactPage)
async def list_artifacts(
    artifact_id: UUID | None = None,
    artifact_kind: ArtifactKind | None = None,
    project_id: UUID | None = None,
    content_sha256: str | None = None,
    storage_status: StorageStatus | None = None,
    original_filename_contains: str | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
    cursor: str | None = None,
) -> ArtifactPage:
    return cast(
        ArtifactPage,
        await ArtifactQueryService.list_artifacts(
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            project_id=project_id,
            content_sha256=content_sha256,
            storage_status=storage_status,
            original_filename_contains=original_filename_contains,
            limit=limit,
            offset=offset,
            cursor=cursor,
        ),
    )


@router.get("/artifacts/{artifact_id}", response_model=ArtifactSummary)
async def get_artifact(artifact_id: UUID) -> ArtifactSummary:
    result = cast(
        ArtifactSummary | None,
        await ArtifactQueryService.get_artifact(artifact_id=artifact_id),
    )
    return _require_result(result, "artifact")


@router.get("/artifacts/{artifact_id}/preview", response_model=ArtifactPreview)
async def preview_artifact(
    artifact_id: UUID,
    principal: OptionalPrincipal,
    max_bytes: PreviewBytes = 128 * 1024,
) -> ArtifactPreview:
    try:
        return await ArtifactContentService.preview(
            artifact_id,
            max_bytes=max_bytes,
            user_id=principal.user_id if principal is not None else None,
        )
    except (ArtifactNotFoundError, ArtifactForbiddenError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ArtifactPreviewUnsupportedError as error:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(error),
        ) from error
    except ArtifactUnavailableError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactObjectIntegrityError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error


@router.get("/artifacts/{artifact_id}/download", response_class=StreamingResponse)
async def download_artifact(
    artifact_id: UUID,
    principal: OptionalPrincipal,
) -> StreamingResponse:
    try:
        download = await ArtifactContentService.download(
            artifact_id,
            user_id=principal.user_id if principal is not None else None,
        )
    except (ArtifactNotFoundError, ArtifactForbiddenError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ArtifactUnavailableError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ArtifactObjectIntegrityError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    fallback_name = re.sub(r"[^A-Za-z0-9._-]", "_", download.original_filename) or "artifact"
    disposition = (
        f'attachment; filename="{fallback_name}"; '
        f"filename*=UTF-8''{quote(download.original_filename)}"
    )
    return StreamingResponse(
        iter_artifact_download(download),
        media_type=download.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": disposition,
            "Content-Length": str(download.size_bytes),
            "X-Content-SHA256": download.content_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/logical-reactions", response_model=LogicalReactionPage)
async def list_logical_reactions(
    project_id: UUID | None = None,
    topology_id: UUID | None = None,
    reaction_key: str | None = None,
    reaction_hash: str | None = None,
    reaction_class: ReactionClass | None = None,
    reaction_smarts: str | None = None,
    reactant_mol_block: str | None = None,
    product_mol_block: str | None = None,
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    has_activation_gibbs_free_energy: bool | None = None,
    has_reaction_gibbs_free_energy: bool | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> LogicalReactionPage:
    try:
        return cast(
            LogicalReactionPage,
            await LogicalReactionQueryService.list_logical_reactions(
                project_id=project_id,
                topology_id=topology_id,
                reaction_key=reaction_key,
                reaction_hash=reaction_hash,
                reaction_class=reaction_class,
                reaction_smarts=reaction_smarts,
                reactant_mol_block=reactant_mol_block,
                product_mol_block=product_mol_block,
                minimum_activation_gibbs_free_energy_kcal_mol=(
                    minimum_activation_gibbs_free_energy_kcal_mol
                ),
                maximum_activation_gibbs_free_energy_kcal_mol=(
                    maximum_activation_gibbs_free_energy_kcal_mol
                ),
                minimum_reaction_gibbs_free_energy_kcal_mol=(
                    minimum_reaction_gibbs_free_energy_kcal_mol
                ),
                maximum_reaction_gibbs_free_energy_kcal_mol=(
                    maximum_reaction_gibbs_free_energy_kcal_mol
                ),
                has_activation_gibbs_free_energy=has_activation_gibbs_free_energy,
                has_reaction_gibbs_free_energy=has_reaction_gibbs_free_energy,
                limit=limit,
                offset=offset,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/logical-reactions/{reaction_id}", response_model=LogicalReactionDetail)
async def get_logical_reaction(
    reaction_id: UUID,
    project_id: UUID | None = None,
) -> LogicalReactionDetail:
    result = cast(
        LogicalReactionDetail | None,
        await LogicalReactionQueryService.get_logical_reaction(
            logical_reaction_id=reaction_id,
            project_id=project_id,
        ),
    )
    return _require_result(result, "logical reaction")


@router.get(
    "/mapped-reactions/thermodynamics/statistics",
    response_model=MappedReactionThermodynamicStatistics,
)
async def get_mapped_reaction_thermodynamic_statistics(
    project_id: UUID | None = None,
) -> MappedReactionThermodynamicStatistics:
    return await ReactionThermodynamicAnalyticsService.statistics(project_id=project_id)


@router.get(
    "/mapped-reactions/thermodynamics/export.csv",
    response_class=StreamingResponse,
)
async def export_mapped_reaction_thermodynamics(
    project_id: UUID | None = None,
) -> StreamingResponse:
    stream = await ReactionThermodynamicAnalyticsService.export_csv(project_id=project_id)
    return StreamingResponse(
        stream,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": ('attachment; filename="mapped-reaction-thermodynamics.csv"'),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/mapped-reactions/{mapped_reaction_id}", response_model=MappedReactionDetail)
async def get_mapped_reaction(
    mapped_reaction_id: UUID,
    project_id: UUID | None = None,
) -> MappedReactionDetail:
    result = cast(
        MappedReactionDetail | None,
        await MappedReactionQueryService.get_mapped_reaction(
            mapped_reaction_id=mapped_reaction_id,
            project_id=project_id,
        ),
    )
    return _require_result(result, "mapped reaction")


@router.get(
    "/mapped-reactions/{mapped_reaction_id}/thermodynamics",
    response_model=MappedReactionThermodynamics,
)
async def get_mapped_reaction_thermodynamics(
    mapped_reaction_id: UUID,
    project_id: UUID | None = None,
) -> MappedReactionThermodynamics:
    result = cast(
        MappedReactionThermodynamics | None,
        await ReactionEnergyQueryService.get_mapped_reaction_thermodynamics(
            mapped_reaction_id=mapped_reaction_id,
            project_id=project_id,
        ),
    )
    return _require_result(result, "mapped reaction")


@router.get("/mapped-reactions", response_model=MappedReactionPage)
async def list_mapped_reactions(
    project_id: UUID | None = None,
    logical_reaction_id: UUID | None = None,
    topology_id: UUID | None = None,
    geometry_id: UUID | None = None,
    mapping_hash: str | None = None,
    mapped_reaction_kind: MappedReactionKind | None = None,
    reaction_smarts: str | None = None,
    similarity_reaction_smiles: str | None = None,
    similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
    minimum_similarity: float | None = None,
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> MappedReactionPage:
    return cast(
        MappedReactionPage,
        await MappedReactionQueryService.list_mapped_reactions(
            project_id=project_id,
            logical_reaction_id=logical_reaction_id,
            topology_id=topology_id,
            geometry_id=geometry_id,
            mapping_hash=mapping_hash,
            mapped_reaction_kind=mapped_reaction_kind,
            reaction_smarts=reaction_smarts,
            similarity_reaction_smiles=similarity_reaction_smiles,
            similarity_metric=similarity_metric,
            minimum_similarity=minimum_similarity,
            minimum_activation_gibbs_free_energy_kcal_mol=(
                minimum_activation_gibbs_free_energy_kcal_mol
            ),
            maximum_activation_gibbs_free_energy_kcal_mol=(
                maximum_activation_gibbs_free_energy_kcal_mol
            ),
            minimum_reaction_gibbs_free_energy_kcal_mol=(
                minimum_reaction_gibbs_free_energy_kcal_mol
            ),
            maximum_reaction_gibbs_free_energy_kcal_mol=(
                maximum_reaction_gibbs_free_energy_kcal_mol
            ),
            limit=limit,
            offset=offset,
        ),
    )


@router.get(
    "/mapped-reactions/{mapped_reaction_id}/energy-profile",
    response_model=ReactionEnergyProfile,
)
async def get_reaction_energy_profile(
    mapped_reaction_id: UUID,
    project_id: UUID | None = None,
    energy_kind: str = "gibbs_free_energy_hartree",
    reference_node_id: UUID | None = None,
) -> ReactionEnergyProfile:
    result = cast(
        ReactionEnergyProfile | None,
        await ReactionEnergyQueryService.get_reaction_energy_profile(
            mapped_reaction_id=mapped_reaction_id,
            project_id=project_id,
            energy_kind=energy_kind,
            reference_node_id=reference_node_id,
        ),
    )
    return _require_result(result, "mapped reaction")


@router.get("/calculation-frames", response_model=CalculationFramePage)
async def list_calculation_frames(
    project_id: UUID | None = None,
    artifact_file_id: UUID | None = None,
    geometry_id: UUID | None = None,
    topology_id: UUID | None = None,
    protocol_id: UUID | None = None,
    frame_role: FrameRole | None = None,
    scf_status: SCFStatus | None = None,
    optimization_status: OptimizationStatus | None = None,
    minimum_energy_hartree: float | None = None,
    maximum_energy_hartree: float | None = None,
    has_selected_energy: bool | None = None,
    has_frequencies: bool | None = None,
    limit: CoreLimit = 100,
    offset: CoreOffset = 0,
) -> CalculationFramePage:
    return cast(
        CalculationFramePage,
        await CalculationQueryService.list_calculation_frames(
            project_id=project_id,
            artifact_file_id=artifact_file_id,
            geometry_id=geometry_id,
            topology_id=topology_id,
            protocol_id=protocol_id,
            frame_role=frame_role,
            scf_status=scf_status,
            optimization_status=optimization_status,
            minimum_energy_hartree=minimum_energy_hartree,
            maximum_energy_hartree=maximum_energy_hartree,
            has_selected_energy=has_selected_energy,
            has_frequencies=has_frequencies,
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/calculation-frames/{frame_id}", response_model=CalculationFrameDetail)
async def get_calculation_frame(
    frame_id: UUID,
    project_id: UUID | None = None,
) -> CalculationFrameDetail:
    result = cast(
        CalculationFrameDetail | None,
        await CalculationQueryService.get_calculation_frame(
            frame_id=frame_id,
            project_id=project_id,
        ),
    )
    return _require_result(result, "calculation frame")


@router.get("/scientific-arrays/{array_id}.npy", response_class=Response)
async def download_scientific_array(
    array_id: UUID,
    _principal: OptionalPrincipal,
    max_bytes: ArrayPayloadBytes = 32 * 1024 * 1024,
) -> Response:
    try:
        download = await ScientificArrayContentService.load_npy(
            array_id,
            max_bytes=max_bytes,
        )
    except ScientificArrayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ScientificArrayPayloadTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    return Response(
        content=download.content,
        media_type="application/x-npy",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{download.filename}"',
            "X-Payload-SHA256": download.payload_sha256,
            "X-Array-Dtype": download.dtype,
            "X-Array-Shape": ",".join(str(size) for size in download.shape),
        },
    )


@router.get("/scientific-arrays/{array_id}/preview", response_model=ScientificArrayPreview)
async def preview_scientific_array(
    array_id: UUID,
    _principal: OptionalPrincipal,
    max_elements: ArrayPreviewElements = 512,
) -> ScientificArrayPreview:
    try:
        preview = await ScientificArrayContentService.preview(
            array_id,
            max_elements=max_elements,
        )
    except ScientificArrayNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return ScientificArrayPreview.model_validate(
        {
            "id": preview.array_id,
            "kind": preview.kind,
            "unit": preview.unit,
            "dtype": preview.dtype,
            "shape": list(preview.shape),
            "total_elements": preview.total_elements,
            "values": preview.values,
            "truncated": preview.truncated,
        }
    )


@router.get("/er-diagram")
async def get_er_diagram() -> dict[str, str]:
    diagram = ErDiagram.from_sqlmodel([MolecularFormula, MolecularTopology])
    return {"mermaid": diagram.to_mermaid()}


def _require_result[ResultT](result: ResultT | None, label: str) -> ResultT:
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} not found",
        )
    return result


def core_api_info() -> dict[str, Any]:
    return {
        "service": "NexusX Core API",
        "mode": "DefineSubset + ErManager + Resolver",
        "docs": "/docs",
        "endpoints": [
            "/api/topologies",
            "/api/formulas/search",
            "/api/topologies/search",
            "/api/artifacts",
            "/api/artifacts/{artifact_id}/preview",
            "/api/artifacts/{artifact_id}/download",
            "/api/logical-reactions",
            "/api/mapped-reactions",
            "/api/calculation-frames",
            "/api/scientific-arrays/{array_id}.npy",
            "/api/scientific-arrays/{array_id}/preview",
            "/api/er-diagram",
        ],
    }


__all__ = [
    "CoreResolver",
    "MolecularFormulaCoreDTO",
    "MolecularTopologyCoreDTO",
    "core_api_info",
    "er_manager",
    "router",
]
