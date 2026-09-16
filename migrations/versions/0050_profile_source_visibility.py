"""Materialize project-scoped visibility for thermodynamic profile sources."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050_profile_source_visibility"
down_revision: str | None = "0049_geometry_project_hash"
branch_labels: str | None = None
depends_on: str | None = None


_PROFILE = "mapped_reaction_thermodynamic_profile"
_SOURCE = "mapped_reaction_thermodynamic_profile_source"
_UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def upgrade() -> None:
    op.add_column(
        _PROFILE,
        sa.Column(
            "source_visibility_status",
            sa.String(length=7),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        _PROFILE,
        sa.Column(
            "source_evidence_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "thermodynamic_profile_source_visibility",
        _PROFILE,
        "source_visibility_status IN ('unknown', 'visible', 'hidden')",
    )
    op.create_index(
        "ix_mapped_reaction_thermodynamic_source_visibility",
        _PROFILE,
        ["mapped_reaction_id", "source_visibility_status"],
    )

    op.create_table(
        _SOURCE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("calculation_frame_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "allow_partial_ingestion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.PrimaryKeyConstraint("id", name="mapped_reaction_thermodynamic_profile_source_pkey"),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            [f"{_PROFILE}.id"],
            name="fk_mapped_reaction_profile_source_profile",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["calculation_frame_id"],
            ["calculation_frame.id"],
            name="fk_mapped_reaction_profile_source_frame",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "calculation_frame_id",
            name="uq_mapped_reaction_profile_source_frame",
        ),
    )
    op.create_index(
        "ix_mapped_reaction_profile_source_frame_profile",
        _SOURCE,
        ["calculation_frame_id", "profile_id"],
    )

    # Keep the status calculation in the database so every write path has the
    # same fail-closed behavior.  The source table is deliberately normalized:
    # JSONB is the API/read representation, while this reverse index is the
    # dependency graph used for invalidation.
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION refresh_thermodynamic_profile_source_visibility(
                profile_ids uuid[]
            )
            RETURNS void
            LANGUAGE sql
            AS $$
                WITH recomputed AS (
                    SELECT
                        p.id,
                        CASE
                            WHEN p.source_evidence_complete IS NOT TRUE THEN
                                CASE
                                    WHEN EXISTS (
                                        SELECT 1
                                        FROM {_SOURCE} AS ps
                                        WHERE ps.profile_id = p.id
                                    ) THEN 'hidden'
                                    ELSE 'unknown'
                                END
                            WHEN NOT EXISTS (
                                SELECT 1
                                FROM {_SOURCE} AS ps
                                WHERE ps.profile_id = p.id
                            ) THEN 'hidden'
                            WHEN EXISTS (
                                SELECT 1
                                FROM {_SOURCE} AS ps
                                WHERE ps.profile_id = p.id
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM calculation_frame AS cf
                                      JOIN parse_revision AS pr
                                        ON pr.id = cf.parse_revision_id
                                      JOIN artifact_file AS af
                                        ON af.id = pr.artifact_file_id
                                      JOIN artifact_ingestion AS ai
                                        ON ai.artifact_file_id = af.id
                                      JOIN geometry AS g
                                        ON g.id = cf.geometry_id
                                      JOIN molecular_topology_derivation AS derivation
                                        ON derivation.id = cf.topology_derivation_id
                                      JOIN mapped_reaction AS mr
                                        ON mr.id = p.mapped_reaction_id
                                      WHERE cf.id = ps.calculation_frame_id
                                        AND af.project_id = mr.project_id
                                        AND af.storage_status <> 'retired'
                                        AND pr.status = 'succeeded'
                                        AND g.project_id = mr.project_id
                                        AND derivation.project_id = mr.project_id
                                        AND (
                                            ai.status = 'succeeded'
                                            OR (
                                                ps.allow_partial_ingestion
                                                AND ai.status = 'partial'
                                                AND pr.parse_completeness = 'complete'
                                                AND pr.source_complete IS TRUE
                                            )
                                        )
                                  )
                            ) THEN 'hidden'
                            ELSE 'visible'
                        END AS status
                    FROM {_PROFILE} AS p
                    WHERE p.id = ANY(profile_ids)
                )
                UPDATE {_PROFILE} AS p
                SET source_visibility_status = recomputed.status
                FROM recomputed
                WHERE p.id = recomputed.id
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION refresh_thermodynamic_profile_source_visibility_for_frames(
                frame_ids uuid[]
            )
            RETURNS void
            LANGUAGE sql
            AS $$
                SELECT refresh_thermodynamic_profile_source_visibility(
                    COALESCE(
                        array_agg(DISTINCT profile_id),
                        ARRAY[]::uuid[]
                    )
                )
                FROM {_SOURCE}
                WHERE calculation_frame_id = ANY(frame_ids)
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION
                refresh_thermodynamic_profile_source_visibility_on_source_change()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                affected_profile_ids uuid[] := ARRAY[]::uuid[];
            BEGIN
                IF TG_OP IN ('INSERT', 'UPDATE') THEN
                    affected_profile_ids := array_append(affected_profile_ids, NEW.profile_id);
                END IF;
                IF TG_OP IN ('UPDATE', 'DELETE') THEN
                    affected_profile_ids := array_append(affected_profile_ids, OLD.profile_id);
                END IF;
                PERFORM refresh_thermodynamic_profile_source_visibility(
                    affected_profile_ids
                );
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
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
            CREATE OR REPLACE FUNCTION
                refresh_thermodynamic_profile_source_visibility_on_profile_change()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                PERFORM refresh_thermodynamic_profile_source_visibility(ARRAY[NEW.id]);
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION
                refresh_thermodynamic_profile_source_visibility_on_frame_change()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                affected_frame_ids uuid[] := ARRAY[]::uuid[];
            BEGIN
                IF TG_TABLE_NAME = 'calculation_frame' THEN
                    affected_frame_ids := ARRAY[NEW.id];
                ELSIF TG_TABLE_NAME = 'parse_revision' THEN
                    SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                    INTO affected_frame_ids
                    FROM calculation_frame AS cf
                    WHERE cf.parse_revision_id IN (NEW.id, OLD.id);
                ELSIF TG_TABLE_NAME = 'artifact_file' THEN
                    SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                    INTO affected_frame_ids
                    FROM calculation_frame AS cf
                    JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                    WHERE pr.artifact_file_id = NEW.id;
                ELSIF TG_TABLE_NAME = 'artifact_ingestion' THEN
                    SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                    INTO affected_frame_ids
                    FROM calculation_frame AS cf
                    JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                    WHERE pr.artifact_file_id = NEW.artifact_file_id;
                END IF;

                PERFORM refresh_thermodynamic_profile_source_visibility_for_frames(
                    affected_frame_ids
                );
                RETURN NEW;
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER trg_thermodynamic_profile_source_change
            AFTER UPDATE OR DELETE ON {_SOURCE}
            FOR EACH ROW
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_source_change()
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER trg_thermodynamic_profile_visibility_profile_change
            AFTER INSERT OR UPDATE OF mapped_reaction_id
            ON {_PROFILE}
            FOR EACH ROW
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_profile_change()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_thermodynamic_profile_visibility_artifact_storage
            AFTER UPDATE OF storage_status ON artifact_file
            FOR EACH ROW
            WHEN (OLD.storage_status IS DISTINCT FROM NEW.storage_status)
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_frame_change()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_thermodynamic_profile_visibility_ingestion_status
            AFTER UPDATE OF status ON artifact_ingestion
            FOR EACH ROW
            WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_frame_change()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_thermodynamic_profile_visibility_parse_state
            AFTER UPDATE OF artifact_file_id, status, parse_completeness, source_complete
            ON parse_revision
            FOR EACH ROW
            WHEN (
                OLD.artifact_file_id IS DISTINCT FROM NEW.artifact_file_id
                OR OLD.status IS DISTINCT FROM NEW.status
                OR OLD.parse_completeness IS DISTINCT FROM NEW.parse_completeness
                OR OLD.source_complete IS DISTINCT FROM NEW.source_complete
            )
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_frame_change()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_thermodynamic_profile_visibility_frame_link
            AFTER UPDATE OF parse_revision_id, geometry_id, topology_derivation_id
            ON calculation_frame
            FOR EACH ROW
            WHEN (
                OLD.parse_revision_id IS DISTINCT FROM NEW.parse_revision_id
                OR OLD.geometry_id IS DISTINCT FROM NEW.geometry_id
                OR OLD.topology_derivation_id IS DISTINCT FROM NEW.topology_derivation_id
            )
            EXECUTE FUNCTION refresh_thermodynamic_profile_source_visibility_on_frame_change()
            """
        )
    )

    # Backfill the reverse index from the persisted JSON representation.  The
    # regex avoids making a deployment fail on a malformed legacy UUID; such a
    # profile remains incomplete and therefore cannot become materialized
    # visible.
    op.execute(
        sa.text(
            f"""
            WITH state_rows AS (
                SELECT
                    p.id AS profile_id,
                    p.reactants,
                    state_name,
                    state_json
                FROM {_PROFILE} AS p
                CROSS JOIN LATERAL (
                    VALUES
                        ('reactants', p.reactants),
                        ('transition_state', p.transition_state),
                        ('products', p.products)
                ) AS states(state_name, state_json)
                WHERE state_json IS NOT NULL
            ), selections AS (
                SELECT
                    profile_id,
                    reactants,
                    state_name,
                    topology_selection
                FROM state_rows
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(state_json->'topologies') = 'array'
                            THEN state_json->'topologies'
                        ELSE '[]'::jsonb
                    END
                ) AS topology_rows(topology_selection)
            ), raw_refs AS (
                SELECT
                    profile_id,
                    topology_selection->>'electronic_source_frame_id' AS frame_text,
                    state_name = 'transition_state' AND reactants IS NULL
                        AS allow_partial_ingestion
                FROM selections
                UNION ALL
                SELECT
                    profile_id,
                    topology_selection->>'thermochemistry_source_frame_id' AS frame_text,
                    state_name = 'transition_state' AND reactants IS NULL
                        AS allow_partial_ingestion
                FROM selections
            )
            INSERT INTO {_SOURCE} (
                profile_id,
                calculation_frame_id,
                allow_partial_ingestion
            )
            SELECT
                profile_id,
                frame_text::uuid,
                bool_and(allow_partial_ingestion)
            FROM raw_refs
            WHERE frame_text ~ '{_UUID_PATTERN}'
            GROUP BY profile_id, frame_text::uuid
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            WITH state_rows AS (
                SELECT
                    p.id AS profile_id,
                    state_json
                FROM {_PROFILE} AS p
                CROSS JOIN LATERAL (
                    VALUES
                        (p.reactants),
                        (p.transition_state),
                        (p.products)
                ) AS states(state_json)
                WHERE state_json IS NOT NULL
            ), selections AS (
                SELECT profile_id, topology_selection
                FROM state_rows
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(state_json->'topologies') = 'array'
                            THEN state_json->'topologies'
                        ELSE '[]'::jsonb
                    END
                ) AS topology_rows(topology_selection)
            ), summary AS (
                SELECT
                    profile_id,
                    count(*) AS selection_count,
                    bool_and(
                        topology_selection ? 'geometry_id'
                        AND nullif(topology_selection->>'geometry_id', '') IS NOT NULL
                        AND topology_selection ? 'electronic_source_frame_id'
                        AND topology_selection->>'electronic_source_frame_id'
                            ~ '{_UUID_PATTERN}'
                        AND topology_selection ? 'thermochemistry_source_frame_id'
                        AND topology_selection->>'thermochemistry_source_frame_id'
                            ~ '{_UUID_PATTERN}'
                        AND EXISTS (
                            SELECT 1
                            FROM calculation_frame AS cf
                            WHERE cf.id =
                                (topology_selection->>'electronic_source_frame_id')::uuid
                        )
                        AND EXISTS (
                            SELECT 1
                            FROM calculation_frame AS cf
                            WHERE cf.id =
                                (topology_selection->>'thermochemistry_source_frame_id')::uuid
                        )
                    ) AS complete
                FROM selections
                GROUP BY profile_id
            )
            UPDATE {_PROFILE} AS p
            SET source_evidence_complete = summary.selection_count > 0 AND summary.complete
            FROM summary
            WHERE p.id = summary.profile_id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            SELECT refresh_thermodynamic_profile_source_visibility(
                COALESCE(array_agg(id), ARRAY[]::uuid[])
            )
            FROM {_PROFILE}
            """
        )
    )


def downgrade() -> None:
    for trigger_name, table_name in (
        ("trg_thermodynamic_profile_source_change", _SOURCE),
        ("trg_thermodynamic_profile_visibility_profile_change", _PROFILE),
        ("trg_thermodynamic_profile_visibility_artifact_storage", "artifact_file"),
        ("trg_thermodynamic_profile_visibility_ingestion_status", "artifact_ingestion"),
        ("trg_thermodynamic_profile_visibility_parse_state", "parse_revision"),
        ("trg_thermodynamic_profile_visibility_frame_link", "calculation_frame"),
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "refresh_thermodynamic_profile_source_visibility_on_frame_change()"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "refresh_thermodynamic_profile_source_visibility_on_profile_change()"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "refresh_thermodynamic_profile_source_visibility_on_source_change()"
        )
    )
    op.execute(
        sa.text(
            "DROP FUNCTION IF EXISTS "
            "refresh_thermodynamic_profile_source_visibility_for_frames(uuid[])"
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS refresh_thermodynamic_profile_source_visibility(uuid[])")
    )
    op.drop_index("ix_mapped_reaction_profile_source_frame_profile", table_name=_SOURCE)
    op.drop_table(_SOURCE)
    op.drop_index(
        "ix_mapped_reaction_thermodynamic_source_visibility",
        table_name=_PROFILE,
    )
    op.drop_constraint(
        "thermodynamic_profile_source_visibility",
        _PROFILE,
        type_="check",
    )
    op.drop_column(_PROFILE, "source_evidence_complete")
    op.drop_column(_PROFILE, "source_visibility_status")
