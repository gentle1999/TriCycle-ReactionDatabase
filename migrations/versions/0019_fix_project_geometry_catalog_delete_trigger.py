"""Fix the calculation-frame delete catalogue trigger.

The delete trigger has only an ``old_frames`` transition table.  A guard copied
from update handling also referenced ``new_frames``, making every frame delete
(including cascades from parse-revision cleanup) fail at runtime.
"""

from alembic import op

revision: str = "0019_catalog_delete_trigger"
down_revision: str | None = "0018_catalog_trigger_defaults"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION sync_project_geometry_catalog_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            WITH decrements AS (
                SELECT
                    artifact_file.project_id,
                    old_frames.geometry_id,
                    count(*)::bigint AS frame_count
                FROM old_frames
                JOIN parse_revision
                  ON parse_revision.id = old_frames.parse_revision_id
                JOIN artifact_file
                  ON artifact_file.id = parse_revision.artifact_file_id
                WHERE artifact_file.storage_status <> 'retired'
                GROUP BY artifact_file.project_id, old_frames.geometry_id
            )
            DELETE FROM project_geometry_catalog AS catalog
            USING decrements
            WHERE catalog.project_id = decrements.project_id
              AND catalog.geometry_id = decrements.geometry_id
              AND catalog.frame_count <= decrements.frame_count;

            WITH decrements AS (
                SELECT
                    artifact_file.project_id,
                    old_frames.geometry_id,
                    count(*)::bigint AS frame_count
                FROM old_frames
                JOIN parse_revision
                  ON parse_revision.id = old_frames.parse_revision_id
                JOIN artifact_file
                  ON artifact_file.id = parse_revision.artifact_file_id
                WHERE artifact_file.storage_status <> 'retired'
                GROUP BY artifact_file.project_id, old_frames.geometry_id
            )
            UPDATE project_geometry_catalog AS catalog
            SET frame_count = catalog.frame_count - decrements.frame_count
            FROM decrements
            WHERE catalog.project_id = decrements.project_id
              AND catalog.geometry_id = decrements.geometry_id;
            RETURN NULL;
        END;
        $$
        """
    )


def downgrade() -> None:
    # Keep the corrected function when downgrading schema-only catalogue changes.
    pass
