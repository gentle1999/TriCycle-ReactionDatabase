"""Make parsed calculation protocols project-local.

``ArtifactFile`` is the only cross-project cache boundary.  A calculation
protocol is parsed metadata, so the old global ``protocol_hash`` identity could
leak one project's calculation catalogue into another project's queries.  The
historical backfill keeps only protocols whose source segments resolve to one
project; ambiguous and orphaned rows are quarantined and cannot be reused by
new writes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_project_owned_protocol"
down_revision: str | None = "0036_project_isolation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "calculation_protocol",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_calculation_protocol_project",
        "calculation_protocol",
        "project",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_calculation_protocol_project_id",
        "calculation_protocol",
        ["project_id"],
    )
    op.drop_constraint(
        "calculation_protocol_protocol_hash_key",
        "calculation_protocol",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_calculation_protocol_project_hash",
        "calculation_protocol",
        ["project_id", "protocol_hash"],
    )

    # Resolve ownership from the immutable source chain.  A protocol used by
    # segments from more than one project is contaminated and intentionally
    # remains NULL until the source is re-imported into project-local rows.
    op.execute(
        sa.text(
            """
            WITH source_projects AS (
                SELECT DISTINCT cs.protocol_id, af.project_id
                FROM calculation_segment AS cs
                JOIN parse_revision AS pr ON pr.id = cs.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE cs.protocol_id IS NOT NULL
            ), summary AS (
                SELECT protocol_id,
                       min(project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT project_id) AS project_count
                FROM source_projects
                GROUP BY protocol_id
            )
            UPDATE calculation_protocol AS cp
            SET project_id = CASE
                WHEN summary.project_count = 1 THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.protocol_id = cp.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'calculation_protocol', id, '[]'::jsonb,
                   'no_unambiguous_project_owner_after_backfill'
            FROM calculation_protocol
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )

    # The model is nullable only so old quarantined rows remain readable by
    # maintenance tooling.  All new protocol rows and all new segment links
    # must be project-owned at the database boundary.  Keep each DDL command
    # separate: this migration is also run through drivers that do not accept
    # multiple PostgreSQL statements in one prepared execute call.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_project_owned_protocol_write()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.project_id IS NULL THEN
                    RAISE EXCEPTION
                        'CalculationProtocol.project_id is required for new or updated rows'
                        USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND NEW.project_id IS DISTINCT FROM OLD.project_id THEN
                    RAISE EXCEPTION
                        'CalculationProtocol project ownership is immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_cross_project_protocol_link()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                source_project uuid;
                protocol_project uuid;
            BEGIN
                SELECT af.project_id
                INTO source_project
                FROM parse_revision AS pr
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE pr.id = NEW.parse_revision_id;

                IF NEW.protocol_id IS NOT NULL THEN
                    SELECT project_id
                    INTO protocol_project
                    FROM calculation_protocol
                    WHERE id = NEW.protocol_id;
                    IF source_project IS DISTINCT FROM protocol_project THEN
                        RAISE EXCEPTION
                            'CalculationSegment source ArtifactFile and protocol must '
                            'share project_id'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_calculation_protocol_project_owner
            BEFORE INSERT OR UPDATE ON calculation_protocol
            FOR EACH ROW EXECUTE FUNCTION reject_project_owned_protocol_write()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_calculation_segment_protocol_boundary
            BEFORE INSERT OR UPDATE ON calculation_segment
            FOR EACH ROW EXECUTE FUNCTION reject_cross_project_protocol_link()
            """
        )
    )

    # Workflow manifests and their bindings do not duplicate project_id in
    # the ORM because their ownership is rooted in the manifest ArtifactFile.
    # Enforce that source chain at the database boundary nevertheless: a raw
    # object may be reused, but a manifest declaration or resolved binding may
    # never attach another project's parsed metadata to it.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_manifest_project_boundary()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                manifest_project uuid;
                target_project uuid;
                predecessor_project uuid;
                predecessor_key text;
            BEGIN
                IF TG_TABLE_NAME = 'workflow_manifest' THEN
                    SELECT project_id
                    INTO manifest_project
                    FROM artifact_file
                    WHERE id = NEW.artifact_file_id;
                    IF manifest_project IS NULL THEN
                        RAISE EXCEPTION
                            'WorkflowManifest must reference a project-owned ArtifactFile'
                            USING ERRCODE = '23514';
                    END IF;
                    IF TG_OP = 'UPDATE'
                       AND NEW.artifact_file_id IS DISTINCT FROM OLD.artifact_file_id THEN
                        RAISE EXCEPTION
                            'WorkflowManifest source ArtifactFile is immutable'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.supersedes_id IS NOT NULL THEN
                        SELECT af.project_id, wm.manifest_key
                        INTO predecessor_project, predecessor_key
                        FROM workflow_manifest AS wm
                        JOIN artifact_file AS af ON af.id = wm.artifact_file_id
                        WHERE wm.id = NEW.supersedes_id;
                        IF predecessor_project IS DISTINCT FROM manifest_project
                           OR predecessor_key IS DISTINCT FROM NEW.manifest_key THEN
                            RAISE EXCEPTION
                                'WorkflowManifest predecessor crosses project or manifest series'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;

                ELSIF TG_TABLE_NAME = 'manifest_artifact_binding' THEN
                    SELECT af.project_id
                    INTO manifest_project
                    FROM workflow_manifest AS wm
                    JOIN artifact_file AS af ON af.id = wm.artifact_file_id
                    WHERE wm.id = NEW.workflow_manifest_id;
                    IF manifest_project IS NULL THEN
                        RAISE EXCEPTION
                            'ManifestArtifactBinding must belong to a project-owned manifest'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.artifact_file_id IS NOT NULL THEN
                        SELECT project_id
                        INTO target_project
                        FROM artifact_file
                        WHERE id = NEW.artifact_file_id;
                        IF target_project IS DISTINCT FROM manifest_project THEN
                            RAISE EXCEPTION
                                'ManifestArtifactBinding target crosses project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_workflow_manifest_project_boundary
            BEFORE INSERT OR UPDATE ON workflow_manifest
            FOR EACH ROW EXECUTE FUNCTION reject_manifest_project_boundary()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_manifest_artifact_binding_project_boundary
            BEFORE INSERT OR UPDATE ON manifest_artifact_binding
            FOR EACH ROW EXECUTE FUNCTION reject_manifest_project_boundary()
            """
        )
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_orphan_thermodynamic_profile_write()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                reaction_project uuid;
            BEGIN
                SELECT project_id
                INTO reaction_project
                FROM mapped_reaction
                WHERE id = NEW.mapped_reaction_id;
                IF reaction_project IS NULL THEN
                    RAISE EXCEPTION
                        'MappedReactionThermodynamicProfile requires a project-owned MappedReaction'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_mapped_reaction_thermodynamic_profile_project_boundary
                ON mapped_reaction_thermodynamic_profile
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_mapped_reaction_thermodynamic_profile_project_boundary
            BEFORE INSERT OR UPDATE ON mapped_reaction_thermodynamic_profile
            FOR EACH ROW EXECUTE FUNCTION reject_orphan_thermodynamic_profile_write()
            """
        )
    )

    # All project-owned roots are nullable in the transitional ORM model only
    # because 0035 preserved ambiguous historical rows for quarantine.  Once
    # this migration is installed, no application or direct SQL write may
    # create another row in that legacy global namespace.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_null_project_owned_root_write()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.project_id IS NULL THEN
                    RAISE EXCEPTION
                        '% project_id is required for new or updated derived rows',
                        TG_TABLE_NAME
                        USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND NEW.project_id IS DISTINCT FROM OLD.project_id THEN
                    RAISE EXCEPTION
                        'project ownership is immutable for %', TG_TABLE_NAME
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    for table_name in (
        "molecular_formula",
        "molecular_topology",
        "molecular_topology_abstraction",
        "molecular_topology_derivation",
        "geometry",
        "logical_reaction",
        "mapped_reaction",
    ):
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_project_owner "
                f"BEFORE INSERT OR UPDATE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION reject_null_project_owned_root_write()"
            )
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_mapped_reaction_thermodynamic_profile_project_boundary
            ON mapped_reaction_thermodynamic_profile
            """
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS reject_orphan_thermodynamic_profile_write()")
    )
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_manifest_artifact_binding_project_boundary
            ON manifest_artifact_binding
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS trg_workflow_manifest_project_boundary
            ON workflow_manifest
            """
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_manifest_project_boundary()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_calculation_segment_protocol_boundary "
            "ON calculation_segment"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_calculation_protocol_project_owner "
            "ON calculation_protocol"
        )
    )
    for table_name in (
        "molecular_formula",
        "molecular_topology",
        "molecular_topology_abstraction",
        "molecular_topology_derivation",
        "geometry",
        "logical_reaction",
        "mapped_reaction",
    ):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_project_owner ON {table_name}")
        )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_null_project_owned_root_write()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_cross_project_protocol_link()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_project_owned_protocol_write()"))
    op.drop_constraint(
        "uq_calculation_protocol_project_hash",
        "calculation_protocol",
        type_="unique",
    )
    op.create_unique_constraint(
        "calculation_protocol_protocol_hash_key",
        "calculation_protocol",
        ["protocol_hash"],
    )
    op.drop_index(
        "ix_calculation_protocol_project_id",
        table_name="calculation_protocol",
    )
    op.drop_constraint(
        "fk_calculation_protocol_project",
        "calculation_protocol",
        type_="foreignkey",
    )
    op.drop_column("calculation_protocol", "project_id")
