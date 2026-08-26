"""Allow fast MolOP ingestion without source span evidence."""

from alembic import op

revision: str = "0003_optional_source_evidence"
down_revision: str | None = "0002_upload_batches"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table in ("calculation_segment", "calculation_frame"):
        for column in (
            "source_start_byte",
            "source_end_byte",
            "source_start_line",
            "source_end_line",
            "source_block_sha256",
        ):
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")


def downgrade() -> None:
    # Existing rows without evidence cannot be converted losslessly. Refuse a
    # downgrade rather than inventing source locations or hashes.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM calculation_segment
                WHERE source_start_byte IS NULL OR source_end_byte IS NULL
                   OR source_start_line IS NULL OR source_end_line IS NULL
                   OR source_block_sha256 IS NULL
            ) OR EXISTS (
                SELECT 1 FROM calculation_frame
                WHERE source_start_byte IS NULL OR source_end_byte IS NULL
                   OR source_start_line IS NULL OR source_end_line IS NULL
                   OR source_block_sha256 IS NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while source evidence is absent';
            END IF;
        END $$;
        """
    )
    for table in ("calculation_segment", "calculation_frame"):
        for column in (
            "source_start_byte",
            "source_end_byte",
            "source_start_line",
            "source_end_line",
            "source_block_sha256",
        ):
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
