"""NexusX Core API routes built from explicit DTO subsets and resolvers."""

import asyncio
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from nexusx import DefineSubset, ErDiagram, ErManager  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

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
    ArtifactDownload,
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
    write_artifact_archive,
)
from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.mapped_reaction_geometry_export import (
    iter_mapped_reaction_geometry_export,
)
from tricycle_reaction_db.application.services.units_ts_dataset_export import (
    UnitsDatasetExportExpiredError,
    UnitsDatasetExportNotFoundError,
    UnitsDatasetExportPendingError,
    UnitsDatasetExportUnavailableError,
    UnitsTsDatasetExportService,
    iter_units_ts_dataset_jsonl,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MolecularFormula, MolecularTopology
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    FrameRole,
    MappedReactionKind,
    OptimizationStatus,
    ReactionClass,
    SCFStatus,
    SimilarityMetric,
    StorageStatus,
)

CoreLimit = Annotated[int, Query(ge=1, le=500)]
CoreOffset = Annotated[int, Query(ge=0)]
ProjectQueryId = Annotated[
    UUID,
    Query(description="The project scope for this project-owned query."),
]
PreviewBytes = Annotated[int, Query(ge=1024, le=512 * 1024)]
ArrayPayloadBytes = Annotated[int, Query(ge=1, le=256 * 1024 * 1024)]
ArrayPreviewElements = Annotated[int, Query(ge=1, le=4096)]
OptionalPrincipal = Annotated[
    AuthenticatedPrincipal | None,
    Depends(get_optional_principal),
]


class ReactionThermodynamicAnalyticsQuery(BaseModel):
    """Project-scoped logical-reaction filters for thermodynamic analytics.

    The CSV export always restricts rows to mapped reactions with complete,
    visible calculation-frame source evidence. That database-side cleaning
    rule is inherent to the export and has no separate ``cleaned_only`` field.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: UUID = Field(description="Internal database project ID used as the data scope.")
    filter_expression: str | None = Field(
        default=None,
        description=(
            "Database-supported JSON logical filter. Leaf nodes use field/value; for example "
            '{"field":"reaction_smarts","value":"<reaction SMARTS>"}.'
        ),
    )
    has_activation_gibbs_free_energy: bool | None = Field(
        default=None,
        description="If true, include only profiles with a database activation Gibbs value.",
    )
    has_reaction_gibbs_free_energy: bool | None = Field(
        default=None,
        description="If true, include only profiles with a database reaction Gibbs value.",
    )


class ArtifactBatchDownloadRequest(BaseModel):
    """The artifact IDs selected for one authorized ZIP download."""

    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[UUID] = Field(min_length=1, max_length=500)


class UnitsTsDatasetExportRequest(BaseModel):
    """Project whose persisted transition-state geometries should be exported."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID


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
            "is_stereo_abstraction_upstream",
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
    project_id: ProjectQueryId,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> list[MolecularTopologyCoreDTO]:
    """List only topologies rooted in artifacts visible to this request."""

    page = await MolecularTopologyQueryService.list_visible_topologies(
        project_id=project_id,
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
            is_stereo_abstraction_upstream=item.is_stereo_abstraction_upstream,
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
    project_id: ProjectQueryId,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
) -> MolecularFormulaPage:
    """Search formula identities by a 118-dimensional inclusive count range."""

    return cast(
        MolecularFormulaPage,
        await MolecularFormulaQueryService.search_formulas(
            minimum_counts=ranges.minimum_counts,
            maximum_counts=ranges.maximum_counts,
            project_id=project_id,
            limit=limit,
            offset=offset,
        ),
    )


@router.post("/topologies/search", response_model=MolecularTopologySearchPage)
async def search_molecular_topologies(
    search: MolecularTopologySearchQuery,
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
    artifact_id: UUID | None = None,
    artifact_kind: ArtifactKind | None = None,
    content_sha256: str | None = None,
    storage_status: StorageStatus | None = None,
    ingestion_status: ArtifactIngestionStatus | None = None,
    original_filename_contains: str | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
    cursor: str | None = None,
    sort_by: str = "created_at",
    sort_direction: str = "desc",
) -> ArtifactPage:
    try:
        return cast(
            ArtifactPage,
            await ArtifactQueryService.list_artifacts(
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                project_id=project_id,
                content_sha256=content_sha256,
                storage_status=storage_status,
                ingestion_status=ingestion_status,
                original_filename_contains=original_filename_contains,
                limit=limit,
                offset=offset,
                cursor=cursor,
                sort_by=sort_by,
                sort_direction=sort_direction,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _remove_temporary_archive(path: str) -> None:
    with suppress(FileNotFoundError):
        Path(path).unlink()


async def _create_artifact_batch_download(
    artifact_ids: list[UUID],
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
) -> FileResponse:
    """Return one ZIP containing selected artifacts authorized for the project."""

    if not 1 <= len(artifact_ids) <= 500:
        raise HTTPException(
            status_code=422,
            detail="artifact_ids must contain between 1 and 500 items",
        )
    if len(set(artifact_ids)) != len(artifact_ids):
        raise HTTPException(status_code=422, detail="artifact_ids must be unique")

    user_id = principal.user_id if principal is not None else None
    download_slots = asyncio.Semaphore(16)

    async def resolve(artifact_id: UUID) -> ArtifactDownload:
        async with download_slots:
            return await ArtifactContentService.download(
                artifact_id,
                user_id=user_id,
                project_id=project_id,
            )

    try:
        downloads = list(
            await asyncio.gather(*(resolve(artifact_id) for artifact_id in artifact_ids))
        )
    except (ArtifactNotFoundError, ArtifactForbiddenError) as error:
        raise HTTPException(status_code=404, detail="artifact not found") from error
    except ArtifactUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ArtifactObjectIntegrityError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    total_bytes = sum(download.size_bytes for download in downloads)
    maximum_bytes = get_settings().max_batch_bytes
    if total_bytes > maximum_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"artifact archive exceeds the {maximum_bytes}-byte limit",
        )

    with tempfile.NamedTemporaryFile(
        prefix="artifact-download-",
        suffix=".zip",
        delete=False,
    ) as temporary:
        archive_path = Path(temporary.name)
    try:
        await asyncio.to_thread(write_artifact_archive, downloads, archive_path)
    except Exception as error:
        _remove_temporary_archive(str(archive_path))
        raise HTTPException(
            status_code=502,
            detail="artifact archive could not be created",
        ) from error

    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename="artifacts.zip",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(_remove_temporary_archive, str(archive_path)),
    )


@router.post("/artifacts/batch-download", response_class=FileResponse)
async def download_artifact_batch(
    payload: ArtifactBatchDownloadRequest,
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
) -> FileResponse:
    """Return one ZIP containing selected artifacts authorized for the project."""

    return await _create_artifact_batch_download(
        payload.artifact_ids,
        principal,
        project_id,
    )


@router.post("/artifacts/batch-download/form", response_class=FileResponse)
async def download_artifact_batch_form(
    artifact_ids: Annotated[list[UUID], Form()],
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
) -> FileResponse:
    """Stream a native-browser ZIP download without materializing it in JS."""

    return await _create_artifact_batch_download(artifact_ids, principal, project_id)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactSummary)
async def get_artifact(
    artifact_id: UUID,
    project_id: ProjectQueryId,
) -> ArtifactSummary:
    result = cast(
        ArtifactSummary | None,
        await ArtifactQueryService.get_artifact(
            artifact_id=artifact_id,
            project_id=project_id,
        ),
    )
    return _require_result(result, "artifact")


@router.get("/artifacts/{artifact_id}/preview", response_model=ArtifactPreview)
async def preview_artifact(
    artifact_id: UUID,
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
    max_bytes: PreviewBytes = 128 * 1024,
) -> ArtifactPreview:
    try:
        return await ArtifactContentService.preview(
            artifact_id,
            max_bytes=max_bytes,
            user_id=principal.user_id if principal is not None else None,
            project_id=project_id,
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
    project_id: ProjectQueryId,
) -> StreamingResponse:
    try:
        download = await ArtifactContentService.download(
            artifact_id,
            user_id=principal.user_id if principal is not None else None,
            project_id=project_id,
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


@router.post("/units-ts-datasets", status_code=status.HTTP_202_ACCEPTED)
async def create_units_ts_dataset(
    request: Request,
    payload: UnitsTsDatasetExportRequest,
    principal: OptionalPrincipal,
) -> dict[str, Any]:
    """Queue a UniTS dataset export and return its status and unique download links."""

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        result = await UnitsTsDatasetExportService.create(payload.project_id, principal.user_id)
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    job_id = result["job_id"]
    token = result["download_url_path"].rsplit("/", maxsplit=1)[-1]
    result["status_url"] = str(request.url_for("get_units_ts_dataset_status", job_id=job_id))
    result["download_url"] = str(request.url_for("download_units_ts_dataset", download_token=token))
    result.pop("status_url_path", None)
    result.pop("download_url_path", None)
    return result


@router.get(
    "/units-ts-datasets/export.jsonl",
    response_class=StreamingResponse,
    summary="Stream UniTS-compatible TS feature samples as JSONL",
    description=(
        "Streams one feature record per verified transition-state geometry mapping in the "
        "specified project. Atom and edge feature orders match the UniTS export contract."
    ),
)
async def export_units_ts_dataset_jsonl(
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
) -> StreamingResponse:
    """Stream UniTS feature records incrementally instead of building a complete NPY first."""

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        await AuthorizationService.require_project_permission(
            principal.user_id,
            project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    return StreamingResponse(
        iter_units_ts_dataset_jsonl(project_id),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="units-ts-dataset.jsonl"',
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/mapped-reactions/transition-state-geometries/export.jsonl",
    response_class=StreamingResponse,
    summary="Stream mapped-reaction TS geometry and RDKit Mol records",
    description=(
        "Streams one JSON Lines record per verified transition-state geometry mapping in the "
        "specified project. Each record uses mapped reaction SMILES as its key and contains "
        "angstrom coordinates plus an RDKit-readable Mol block."
    ),
)
async def export_mapped_reaction_transition_state_geometries(
    principal: OptionalPrincipal,
    project_id: ProjectQueryId,
) -> StreamingResponse:
    """Stream project-scoped mapped reaction, TS geometry, and RDKit Mol records."""

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        await AuthorizationService.require_project_permission(
            principal.user_id,
            project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
    except ProjectAccessDeniedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

    return StreamingResponse(
        iter_mapped_reaction_geometry_export(project_id),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": ('attachment; filename="mapped-reaction-ts-geometries.jsonl"'),
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/units-ts-datasets/{job_id}")
async def get_units_ts_dataset_status(
    job_id: UUID,
    principal: OptionalPrincipal,
) -> dict[str, Any]:
    """Return the generation state for a dataset export job."""

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        return await UnitsTsDatasetExportService.status(job_id, principal.user_id)
    except UnitsDatasetExportNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get(
    "/units-ts-datasets/download/{download_token}",
    response_class=StreamingResponse,
)
async def download_units_ts_dataset(download_token: str) -> StreamingResponse:
    """Stream a completed NPY dataset through its unguessable capability link."""

    try:
        download = await UnitsTsDatasetExportService.download(download_token)
    except UnitsDatasetExportNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except UnitsDatasetExportPendingError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
            headers={"Retry-After": "2"},
        ) from error
    except UnitsDatasetExportExpiredError as error:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(error)) from error
    except UnitsDatasetExportUnavailableError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", download.filename) or "dataset.npy"
    return StreamingResponse(
        UnitsTsDatasetExportService.iter_download(download),
        media_type="application/x-npy",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(download.size_bytes),
            "X-Content-SHA256": download.content_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/logical-reactions", response_model=LogicalReactionPage)
async def list_logical_reactions(
    project_id: ProjectQueryId,
    topology_id: UUID | None = None,
    reaction_key: str | None = None,
    reaction_hash: str | None = None,
    reaction_class: ReactionClass | None = None,
    reaction_smarts: str | None = None,
    similarity_reaction_smiles: str | None = None,
    similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
    reactant_mol_block: str | None = None,
    product_mol_block: str | None = None,
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_mapped_reaction_count: int | None = Query(default=None, ge=0),
    maximum_mapped_reaction_count: int | None = Query(default=None, ge=0),
    has_activation_gibbs_free_energy: bool | None = None,
    has_reaction_gibbs_free_energy: bool | None = None,
    reactant_product_changed: bool | None = None,
    limit: CoreLimit = 50,
    offset: CoreOffset = 0,
    sort_by: str = "default",
    sort_direction: str = "asc",
) -> LogicalReactionPage:
    """Return logical paths whose reaction-structure filters match MappedReaction rows."""

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
                similarity_reaction_smiles=similarity_reaction_smiles,
                similarity_metric=similarity_metric,
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
                minimum_mapped_reaction_count=minimum_mapped_reaction_count,
                maximum_mapped_reaction_count=maximum_mapped_reaction_count,
                has_activation_gibbs_free_energy=has_activation_gibbs_free_energy,
                has_reaction_gibbs_free_energy=has_reaction_gibbs_free_energy,
                reactant_product_changed=reactant_product_changed,
                limit=limit,
                offset=offset,
                sort_by=sort_by,
                sort_direction=sort_direction,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/logical-reactions/{reaction_id}", response_model=LogicalReactionDetail)
async def get_logical_reaction(
    reaction_id: UUID,
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
) -> MappedReactionThermodynamicStatistics:
    return await ReactionThermodynamicAnalyticsService.statistics(project_id=project_id)


@router.post(
    "/mapped-reactions/thermodynamics/statistics",
    response_model=MappedReactionThermodynamicStatistics,
)
async def query_mapped_reaction_thermodynamic_statistics(
    query: ReactionThermodynamicAnalyticsQuery,
) -> MappedReactionThermodynamicStatistics:
    try:
        return await ReactionThermodynamicAnalyticsService.statistics(
            project_id=query.project_id,
            filter_expression=query.filter_expression,
            has_activation_gibbs_free_energy=query.has_activation_gibbs_free_energy,
            has_reaction_gibbs_free_energy=query.has_reaction_gibbs_free_energy,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/mapped-reactions/thermodynamics/export.csv",
    response_class=StreamingResponse,
)
async def export_mapped_reaction_thermodynamics(
    project_id: ProjectQueryId,
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


@router.post(
    "/mapped-reactions/thermodynamics/export.csv",
    response_class=StreamingResponse,
    summary="Stream project-scoped mapped-reaction thermodynamics as CSV",
    description=(
        "Filters mapped reactions and thermodynamic profiles in the database and streams CSV. "
        "Every export row must have complete, visible source evidence: source calculation frames "
        "must come from successful ingestion, or from a partial ingestion whose parse succeeded "
        "and whose individual frame is complete. Incomplete or hidden source profiles are "
        "excluded. This endpoint always applies that cleaned-source rule; it does not accept a "
        "cleaned_only request field. Energy values are in kcal/mol and phase runtimes are "
        "in seconds."
    ),
)
async def export_filtered_mapped_reaction_thermodynamics(
    query: ReactionThermodynamicAnalyticsQuery,
) -> StreamingResponse:
    try:
        stream = await ReactionThermodynamicAnalyticsService.export_csv(
            project_id=query.project_id,
            filter_expression=query.filter_expression,
            has_activation_gibbs_free_energy=query.has_activation_gibbs_free_energy,
            has_reaction_gibbs_free_energy=query.has_reaction_gibbs_free_energy,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
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
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
    logical_reaction_id: UUID | None = None,
    topology_id: UUID | None = None,
    geometry_id: UUID | None = None,
    mapping_hash: str | None = None,
    mapped_reaction_kind: MappedReactionKind | None = None,
    node_role: str | None = None,
    reaction_smarts: str | None = None,
    similarity_reaction_smiles: str | None = None,
    similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
    minimum_similarity: float | None = None,
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    reactant_product_changed: bool | None = None,
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
            node_role=node_role,
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
            reactant_product_changed=reactant_product_changed,
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
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
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
    project_id: ProjectQueryId,
    max_bytes: ArrayPayloadBytes = 32 * 1024 * 1024,
) -> Response:
    try:
        download = await ScientificArrayContentService.load_npy(
            array_id,
            max_bytes=max_bytes,
            project_id=project_id,
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
            "X-Array-Unit": download.unit,
            "X-Array-Dtype": download.dtype,
            "X-Array-Shape": ",".join(str(size) for size in download.shape),
        },
    )


@router.get("/scientific-arrays/{array_id}/preview", response_model=ScientificArrayPreview)
async def preview_scientific_array(
    array_id: UUID,
    _principal: OptionalPrincipal,
    project_id: ProjectQueryId,
    max_elements: ArrayPreviewElements = 512,
) -> ScientificArrayPreview:
    try:
        preview = await ScientificArrayContentService.preview(
            array_id,
            max_elements=max_elements,
            project_id=project_id,
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
