"""Materialize project-scoped Geometry list summaries.

The Geometry table deliberately keeps scientific coordinate payloads.  A project
catalogue therefore also owns the small, visibility-scoped facts needed to page
that table without re-scanning calculation frames for every request.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0012_catalog_summary"
down_revision: str | None = "0011_query_listing_indexes"
branch_labels: str | None = None
depends_on: str | None = None

_THERMODYNAMIC_PREDICATE = """
thermochemistry_result.zpe_correction_hartree IS NOT NULL
OR thermochemistry_result.thermal_energy_correction_hartree IS NOT NULL
OR thermochemistry_result.thermal_enthalpy_correction_hartree IS NOT NULL
OR thermochemistry_result.thermal_gibbs_correction_hartree IS NOT NULL
OR thermochemistry_result.zero_point_energy_hartree IS NOT NULL
OR thermochemistry_result.thermal_internal_energy_hartree IS NOT NULL
OR thermochemistry_result.enthalpy_hartree IS NOT NULL
OR thermochemistry_result.gibbs_free_energy_hartree IS NOT NULL
OR thermochemistry_result.entropy_cal_mol_k IS NOT NULL
OR thermochemistry_result.heat_capacity_cv_cal_mol_k IS NOT NULL
"""


def _create_summary_trigger_function(name: str, affected_rows: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION {name}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            WITH affected AS (
                {affected_rows}
            )
            UPDATE project_geometry_catalog AS catalog
            SET
                geometry_created_at = geometry.created_at,
                has_frequency_data = COALESCE(summary.has_frequency_data, false),
                has_imaginary_frequency = COALESCE(summary.has_imaginary_frequency, false),
                has_thermodynamic_property = COALESCE(
                    summary.has_thermodynamic_property,
                    false
                )
            FROM affected
            JOIN geometry ON geometry.id = affected.geometry_id
            LEFT JOIN LATERAL (
                SELECT
                    bool_or(calculation_frame.frequency_count IS NOT NULL)
                        AS has_frequency_data,
                    bool_or(calculation_frame.negative_frequency_count > 0)
                        AS has_imaginary_frequency,
                    bool_or(({_THERMODYNAMIC_PREDICATE}))
                        AS has_thermodynamic_property
                FROM calculation_frame
                JOIN parse_revision
                  ON parse_revision.id = calculation_frame.parse_revision_id
                JOIN artifact_file
                  ON artifact_file.id = parse_revision.artifact_file_id
                LEFT JOIN thermochemistry_result
                  ON thermochemistry_result.frame_id = calculation_frame.id
                WHERE calculation_frame.geometry_id = affected.geometry_id
                  AND artifact_file.project_id = affected.project_id
                  AND artifact_file.storage_status <> 'retired'
            ) AS summary ON true
            WHERE catalog.project_id = affected.project_id
              AND catalog.geometry_id = affected.geometry_id;
            RETURN NULL;
        END;
        $$
        """
    )


def upgrade() -> None:
    op.add_column(
        "project_geometry_catalog",
        sa.Column(
            "geometry_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    for column in (
        "has_frequency_data",
        "has_imaginary_frequency",
        "has_thermodynamic_property",
    ):
        op.add_column(
            "project_geometry_catalog",
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    for column in (
        "has_frequency_data",
        "has_imaginary_frequency",
        "has_thermodynamic_property",
    ):
        op.alter_column("project_geometry_catalog", column, server_default=None)

    op.create_table(
        "project_geometry_catalog_count",
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("geometry_count", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "geometry_count >= 0",
            name="ck_project_geometry_catalog_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.execute(
        """
        INSERT INTO project_geometry_catalog_count (project_id, geometry_count)
        SELECT project_id, count(*)::bigint
        FROM project_geometry_catalog
        GROUP BY project_id
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_count_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO project_geometry_catalog_count (project_id, geometry_count)
            SELECT project_id, count(*)::bigint
            FROM new_catalog_rows
            GROUP BY project_id
            ON CONFLICT (project_id)
            DO UPDATE
            SET geometry_count = project_geometry_catalog_count.geometry_count
                + EXCLUDED.geometry_count;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_project_geometry_catalog_count_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            WITH decrements AS (
                SELECT project_id, count(*)::bigint AS geometry_count
                FROM old_catalog_rows
                GROUP BY project_id
            )
            UPDATE project_geometry_catalog_count AS counts
            SET geometry_count = counts.geometry_count - decrements.geometry_count
            FROM decrements
            WHERE counts.project_id = decrements.project_id;

            DELETE FROM project_geometry_catalog_count
            WHERE geometry_count = 0;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_count_after_insert
        AFTER INSERT ON project_geometry_catalog
        REFERENCING NEW TABLE AS new_catalog_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION sync_project_geometry_catalog_count_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_count_after_delete
        AFTER DELETE ON project_geometry_catalog
        REFERENCING OLD TABLE AS old_catalog_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION sync_project_geometry_catalog_count_delete()
        """
    )

    frame_insert_rows = """
        SELECT DISTINCT artifact_file.project_id, new_frames.geometry_id
        FROM new_frames
        JOIN parse_revision ON parse_revision.id = new_frames.parse_revision_id
        JOIN artifact_file ON artifact_file.id = parse_revision.artifact_file_id
        WHERE artifact_file.storage_status <> 'retired'
    """
    frame_delete_rows = frame_insert_rows.replace("new_frames", "old_frames")
    frame_update_rows = f"{frame_delete_rows} UNION {frame_insert_rows}"
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_frame_insert",
        frame_insert_rows,
    )
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_frame_delete",
        frame_delete_rows,
    )
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_frame_update",
        frame_update_rows,
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_frame_after_insert
        AFTER INSERT ON calculation_frame
        REFERENCING NEW TABLE AS new_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_frame_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_frame_after_delete
        AFTER DELETE ON calculation_frame
        REFERENCING OLD TABLE AS old_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_frame_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_frame_after_update
        AFTER UPDATE ON calculation_frame
        REFERENCING OLD TABLE AS old_frames NEW TABLE AS new_frames
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_frame_update()
        """
    )

    result_insert_rows = """
        SELECT DISTINCT artifact_file.project_id, calculation_frame.geometry_id
        FROM new_results
        JOIN calculation_frame ON calculation_frame.id = new_results.frame_id
        JOIN parse_revision ON parse_revision.id = calculation_frame.parse_revision_id
        JOIN artifact_file ON artifact_file.id = parse_revision.artifact_file_id
        WHERE artifact_file.storage_status <> 'retired'
    """
    result_delete_rows = result_insert_rows.replace("new_results", "old_results")
    result_update_rows = f"{result_delete_rows} UNION {result_insert_rows}"
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_thermochemistry_insert",
        result_insert_rows,
    )
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_thermochemistry_delete",
        result_delete_rows,
    )
    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_thermochemistry_update",
        result_update_rows,
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_thermochemistry_after_insert
        AFTER INSERT ON thermochemistry_result
        REFERENCING NEW TABLE AS new_results
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_thermochemistry_insert()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_thermochemistry_after_delete
        AFTER DELETE ON thermochemistry_result
        REFERENCING OLD TABLE AS old_results
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_thermochemistry_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_thermochemistry_after_update
        AFTER UPDATE ON thermochemistry_result
        REFERENCING OLD TABLE AS old_results NEW TABLE AS new_results
        FOR EACH STATEMENT
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_thermochemistry_update()
        """
    )

    _create_summary_trigger_function(
        "refresh_project_geometry_catalog_summary_from_artifact",
        """
        SELECT DISTINCT NEW.project_id, calculation_frame.geometry_id
        FROM parse_revision
        JOIN calculation_frame ON calculation_frame.parse_revision_id = parse_revision.id
        WHERE parse_revision.artifact_file_id = NEW.id
          AND NEW.storage_status <> 'retired'
        """,
    )
    op.execute(
        """
        CREATE TRIGGER project_geometry_catalog_summary_artifact_after_update
        AFTER UPDATE OF project_id, storage_status ON artifact_file
        FOR EACH ROW
        WHEN (
            OLD.project_id IS DISTINCT FROM NEW.project_id
            OR OLD.storage_status IS DISTINCT FROM NEW.storage_status
        )
        EXECUTE FUNCTION refresh_project_geometry_catalog_summary_from_artifact()
        """
    )

    op.create_index(
        "ix_project_geometry_catalog_created_page",
        "project_geometry_catalog",
        ["project_id", "geometry_created_at", "geometry_id"],
    )
    op.create_index(
        "ix_project_geometry_catalog_thermodynamic_page",
        "project_geometry_catalog",
        ["project_id", "geometry_created_at", "geometry_id"],
        postgresql_where=sa.text("has_thermodynamic_property"),
    )
    op.create_index(
        "ix_project_geometry_catalog_nonthermodynamic_page",
        "project_geometry_catalog",
        ["project_id", "geometry_created_at", "geometry_id"],
        postgresql_where=sa.text("NOT has_thermodynamic_property"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_geometry_catalog_nonthermodynamic_page",
        table_name="project_geometry_catalog",
    )
    op.drop_index(
        "ix_project_geometry_catalog_thermodynamic_page",
        table_name="project_geometry_catalog",
    )
    op.drop_index("ix_project_geometry_catalog_created_page", table_name="project_geometry_catalog")
    op.execute(
        "DROP TRIGGER IF EXISTS project_geometry_catalog_summary_artifact_after_update "
        "ON artifact_file"
    )
    op.execute("DROP FUNCTION IF EXISTS refresh_project_geometry_catalog_summary_from_artifact()")
    for event in ("update", "delete", "insert"):
        op.execute(
            "DROP TRIGGER IF EXISTS "
            f"project_geometry_catalog_summary_thermochemistry_after_{event} "
            "ON thermochemistry_result"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            f"refresh_project_geometry_catalog_summary_from_thermochemistry_{event}()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS "
            f"project_geometry_catalog_summary_frame_after_{event} ON calculation_frame"
        )
        op.execute(
            f"DROP FUNCTION IF EXISTS refresh_project_geometry_catalog_summary_from_frame_{event}()"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS project_geometry_catalog_count_after_delete "
        "ON project_geometry_catalog"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS project_geometry_catalog_count_after_insert "
        "ON project_geometry_catalog"
    )
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_count_delete()")
    op.execute("DROP FUNCTION IF EXISTS sync_project_geometry_catalog_count_insert()")
    op.drop_table("project_geometry_catalog_count")
    for column in (
        "has_thermodynamic_property",
        "has_imaginary_frequency",
        "has_frequency_data",
        "geometry_created_at",
    ):
        op.drop_column("project_geometry_catalog", column)
