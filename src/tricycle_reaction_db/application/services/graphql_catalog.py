"""Non-paginated GraphQL browse facade over the canonical query services."""

from typing import Annotated, cast
from uuid import UUID

from nexusx import UseCaseService, query  # type: ignore[import-untyped]
from pydantic import Field

from tricycle_reaction_db.application.dtos import (
    ArtifactPage,
    ArtifactSummary,
    CalculationFrameDetail,
    CalculationFramePage,
    CalculationFrameSummary,
    LogicalReactionDetail,
    LogicalReactionPage,
    LogicalReactionSummary,
    MappedReactionDetail,
)
from tricycle_reaction_db.application.services.queries import (
    ArtifactQueryService,
    CalculationQueryService,
    LogicalReactionQueryService,
    MappedReactionQueryService,
)

BrowseLimit = Annotated[int, Field(ge=1, le=200, description="Maximum rows to return.")]


class GraphQLCatalogService(UseCaseService):  # type: ignore[misc]
    """Browse the database with direct list results for Direct-list GraphQL."""

    @query  # type: ignore[untyped-decorator]
    async def list_artifacts(cls, limit: BrowseLimit = 50) -> list[ArtifactSummary]:
        page = cast(
            ArtifactPage,
            await ArtifactQueryService.list_artifacts(limit=limit, offset=0),
        )
        return page.items

    @query  # type: ignore[untyped-decorator]
    async def list_logical_reactions(
        cls,
        limit: BrowseLimit = 50,
    ) -> list[LogicalReactionSummary]:
        page = cast(
            LogicalReactionPage,
            await LogicalReactionQueryService.list_logical_reactions(limit=limit, offset=0),
        )
        return page.items

    @query  # type: ignore[untyped-decorator]
    async def list_calculation_frames(
        cls,
        limit: BrowseLimit = 100,
    ) -> list[CalculationFrameSummary]:
        page = cast(
            CalculationFramePage,
            await CalculationQueryService.list_calculation_frames(limit=limit, offset=0),
        )
        return page.items

    @query  # type: ignore[untyped-decorator]
    async def get_logical_reaction(
        cls,
        logical_reaction_id: UUID,
    ) -> LogicalReactionDetail | None:
        return cast(
            LogicalReactionDetail | None,
            await LogicalReactionQueryService.get_logical_reaction(
                logical_reaction_id=logical_reaction_id
            ),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_mapped_reaction(
        cls,
        mapped_reaction_id: UUID,
    ) -> MappedReactionDetail | None:
        return cast(
            MappedReactionDetail | None,
            await MappedReactionQueryService.get_mapped_reaction(
                mapped_reaction_id=mapped_reaction_id
            ),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_calculation_frame(
        cls,
        frame_id: UUID,
    ) -> CalculationFrameDetail | None:
        return cast(
            CalculationFrameDetail | None,
            await CalculationQueryService.get_calculation_frame(frame_id=frame_id),
        )


__all__ = ["GraphQLCatalogService"]
