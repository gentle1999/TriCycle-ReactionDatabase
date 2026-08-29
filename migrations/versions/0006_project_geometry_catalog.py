"""Maintain a project-owned geometry directory for catalogue counts."""

import sqlalchemy as sa
from alembic import op

revision: str = "0006_project_geometry_catalog"
down_revision: str | None = "0005_frame_visibility_idx"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "project_geometry_catalog",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("geometry_id", sa.UUID(), nullable=False),
        sa.Column("frame_count", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("frame_count > 0", name="ck_project_geometry_catalog_frame_count"),
        sa.PrimaryKeyConstraint("project_id", "geometry_id"),
    )
    op.execute(
        """
        INSERT INTO project_geometry_catalog (project_id, geometry_id, frame_count)
        SELECT artifact_file.project_id, calculation_frame.geometry_id, count(*)::bigint
        FROM artifact_file
        JOIN parse_revision
          ON parse_revision.artifact_file_id = artifact_file.id
        JOIN calculation_frame
          ON calculation_frame.parse_revision_id = parse_revision.id
        WHERE artifact_file.storage_status <> 'retired'
        GROUP BY artifact_file.project_id, calculation_frame.geometry_id
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO project_geometry_catalog (project_id, geometry_id, frame_count)
            SELECT artifact_file.project_id, new_frames.geometry_id, count(*)::bigint
            FROM new_frames
            JOIN parse_revision
              ON parse_revision.id = new_frames.parse_revision_id
            JOIN artifact_file
              ON artifact_file.id = parse_revision.artifact_file_id
            WHERE artifact_file.storage_status <> 'retired'
            GROUP BY artifact_file.project_id, new_frames.geometry_id
            ON CONFLICT (project_id, geometry_id)
            DO UPDATE SET frame_count = project_geometry_catalog.frame_count + EXCLUDED.frame_count;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_delete()
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
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_update()
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

            INSERT INTO project_geometry_catalog (project_id, geometry_id, frame_count)
            SELECT artifact_file.project_id, new_frames.geometry_id, count(*)::bigint
            FROM new_frames
            JOIN parse_revision
              ON parse_revision.id = new_frames.parse_revision_id
            JOIN artifact_file
              ON artifact_file.id = parse_revision.artifact_file_id
            WHERE artifact_file.storage_status <> 'retired'
            GROUP BY artifact_file.project_id, new_frames.geometry_id
            ON CONFLICT (project_id, geometry_id)
            DO UPDATE SET frame_count = project_geometry_catalog.frame_count + EXCLUDED.frame_count;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_artifact()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.storage_status <> 'retired' THEN
                WITH decrements AS (
                    SELECT calculation_frame.geometry_id, count(*)::bigint AS frame_count
                    FROM parse_revision
                    JOIN calculation_frame
                      ON calculation_frame.parse_revision_id = parse_revision.id
                    WHERE parse_revision.artifact_file_id = OLD.id
                    GROUP BY calculation_frame.geometry_id
                )
                DELETE FROM project_geometry_catalog AS catalog
                USING decrements
                WHERE catalog.project_id = OLD.project_id
                  AND catalog.geometry_id = decrements.geometry_id
                  AND catalog.frame_count <= decrements.frame_count;

                WITH decrements AS (
                    SELECT calculation_frame.geometry_id, count(*)::bigint AS frame_count
                    FROM parse_revision
                    JOIN calculation_frame
                      ON calculation_frame.parse_revision_id = parse_revision.id
                    WHERE parse_revision.artifact_file_id = OLD.id
                    GROUP BY calculation_frame.geometry_id
                )
                UPDATE project_geometry_catalog AS catalog
                SET frame_count = catalog.frame_count - decrements.frame_count
                FROM decrements
                WHERE catalog.project_id = OLD.project_id
                  AND catalog.geometry_id = decrements.geometry_id;
            END IF;

            IF NEW.storage_status <> 'retired' THEN
                INSERT INTO project_geometry_catalog (project_id, geometry_id, frame_count)
                SELECT NEW.project_id, calculation_frame.geometry_id, count(*)::bigint
                FROM parse_revision
                JOIN calculation_frame
                  ON calculation_frame.parse_revision_id = parse_revision.id
                WHERE parse_revision.artifact_file_id = NEW.id
                GROUP BY calculation_frame.geometry_id
                ON CONFLICT (project_id, geometry_id)
                DO UPDATE
                SET frame_count = project_geometry_catalog.frame_count + EXCLUDED.frame_count;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_after_insert
        AFTER INSERT ON calculation_frame
        REFERENCING NEW TABLE AS new_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION sync_project_geometry_catalog_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_after_delete
        AFTER DELETE ON calculation_frame
        REFERENCING OLD TABLE AS old_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION sync_project_geometry_catalog_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_after_update
        AFTER UPDATE ON calculation_frame
        REFERENCING OLD TABLE AS old_frames NEW TABLE AS new_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION sync_project_geometry_catalog_update()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_after_artifact_update
        AFTER UPDATE OF project_id, storage_status ON artifact_file
        FOR EACH ROW
        WHEN (
            OLD.project_id IS DISTINCT FROM NEW.project_id
            OR OLD.storage_status IS DISTINCT FROM NEW.storage_status
        )
        EXECUTE FUNCTION sync_project_geometry_catalog_artifact()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS project_geometry_catalog_after_artifact_update ON artifact_file"
    )
    op.execute("DROP TRIGGER IF EXISTS project_geometry_catalog_after_update ON calculation_frame")
    op.execute("DROP TRIGGER IF EXISTS project_geometry_catalog_after_delete ON calculation_frame")
    op.execute("DROP TRIGGER IF EXISTS project_geometry_catalog_after_insert ON calculation_frame")
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_artifact()")
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_update()")
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_delete()")
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_insert()")
    op.drop_table("project_geometry_catalog")
