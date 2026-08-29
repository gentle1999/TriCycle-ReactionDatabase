"""Distinguish calculation outputs that contain no calculation frames."""

from alembic import op

revision: str = "0010_ingestion_filtered"
down_revision: str | None = "0009_geom_match_idx"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        "status IN ('pending', 'succeeded', 'partial', 'filtered', 'failed')",
    )
    op.execute(
        """
        UPDATE artifact_ingestion
        SET status = 'filtered'
        WHERE error_code = 'no_calculation_frames'
          AND COALESCE(source_frame_count, 0) = 0
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE artifact_ingestion
        SET status = 'failed'
        WHERE status = 'filtered'
        """
    )
    op.drop_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        type_="check",
    )
    op.create_check_constraint(
        "artifact_ingestion_status",
        "artifact_ingestion",
        "status IN ('pending', 'succeeded', 'partial', 'failed')",
    )
