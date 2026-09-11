"""Scope derived chemistry and reaction identities to their source project.

The original schema treated formulas, topologies, geometries, and reactions as
global identities.  That is safe only when the corresponding rows are truly
public.  In this application the only intentionally shareable object is the
immutable ``artifact_file``; every parsed or derived row must therefore belong
to one project.  Historical rows whose successful source set is ambiguous are
left intact but quarantined (``project_id IS NULL``) so they cannot be served
through a project-scoped query and can be re-imported later without deleting
the source files or calculation facts.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_project_owned_derived_data"
down_revision: str | None = "0034_ingestion_recovery_lease"
branch_labels: str | None = None
depends_on: str | None = None


_OWNED_TABLES = (
    "molecular_formula",
    "molecular_topology",
    "molecular_topology_abstraction",
    "molecular_topology_derivation",
    "geometry",
    "logical_reaction",
    "mapped_reaction",
)


def _add_project_column(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        f"fk_{table_name}_project",
        table_name,
        "project",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        f"ix_{table_name}_project_id",
        table_name,
        ["project_id"],
    )


def _backfill_project_ownership() -> None:
    # Geometry and TS endpoint rows are the authoritative source edges for a
    # topology.  Retired artifacts are deliberately included: retirement
    # changes visibility, not provenance, and must not turn a historical
    # cross-project identity into an apparently private one.
    op.execute(
        sa.text(
            """
            WITH source_projects AS (
                SELECT g.topology_id, af.project_id
                FROM geometry AS g
                JOIN calculation_frame AS cf ON cf.geometry_id = g.id
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE pr.status = 'succeeded'
                  AND EXISTS (
                      SELECT 1
                      FROM artifact_ingestion AS ai
                      WHERE ai.artifact_file_id = af.id
                        AND ai.status = 'succeeded'
                  )
                UNION
                SELECT tse.topology_id, af.project_id
                FROM transition_state_endpoint AS tse
                JOIN calculation_frame AS cf ON cf.id = tse.calculation_frame_id
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE pr.status = 'succeeded'
                  AND EXISTS (
                      SELECT 1
                      FROM artifact_ingestion AS ai
                      WHERE ai.artifact_file_id = af.id
                        AND ai.status = 'succeeded'
                  )
            ), summary AS (
                SELECT mt.id,
                       min(sp.project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT sp.project_id) AS project_count
                FROM molecular_topology AS mt
                LEFT JOIN source_projects AS sp ON sp.topology_id = mt.id
                GROUP BY mt.id
            )
            UPDATE molecular_topology AS mt
            SET project_id = CASE
                WHEN summary.project_count = 1 THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.id = mt.id
            """
        )
    )

    # A formula is private only if every topology that uses it resolves to the
    # same project.  A NULL topology owner is treated as contamination rather
    # than silently inheriting a project.
    op.execute(
        sa.text(
            """
            WITH summary AS (
                SELECT mf.id,
                       min(mt.project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT mt.project_id) AS project_count,
                       count(*) FILTER (WHERE mt.project_id IS NULL) AS null_count
                FROM molecular_formula AS mf
                LEFT JOIN molecular_topology AS mt ON mt.formula_id = mf.id
                GROUP BY mf.id
            )
            UPDATE molecular_formula AS mf
            SET project_id = CASE
                WHEN summary.project_count = 1 AND summary.null_count = 0
                    THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.id = mf.id
            """
        )
    )

    op.execute(
        sa.text(
            """
            WITH source_projects AS (
                SELECT g.id AS geometry_id, af.project_id
                FROM geometry AS g
                JOIN calculation_frame AS cf ON cf.geometry_id = g.id
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE pr.status = 'succeeded'
                  AND EXISTS (
                      SELECT 1
                      FROM artifact_ingestion AS ai
                      WHERE ai.artifact_file_id = af.id
                        AND ai.status = 'succeeded'
                  )
            ), summary AS (
                SELECT g.id,
                       min(sp.project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT sp.project_id) AS project_count,
                       mt.project_id AS topology_project_id
                FROM geometry AS g
                JOIN molecular_topology AS mt ON mt.id = g.topology_id
                LEFT JOIN source_projects AS sp ON sp.geometry_id = g.id
                GROUP BY g.id, mt.project_id
            )
            UPDATE geometry AS g
            SET project_id = CASE
                WHEN summary.project_count = 1
                 AND summary.owner_project_id IS NOT DISTINCT FROM summary.topology_project_id
                 AND summary.topology_project_id IS NOT NULL
                    THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.id = g.id
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE molecular_topology_derivation AS d
            SET project_id = mt.project_id
            FROM molecular_topology AS mt
            WHERE mt.id = d.topology_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE molecular_topology_abstraction AS edge
            SET project_id = specific.project_id
            FROM molecular_topology AS specific,
                 molecular_topology AS general
            WHERE specific.id = edge.specific_topology_id
              AND general.id = edge.general_topology_id
              AND specific.project_id IS NOT NULL
              AND specific.project_id = general.project_id
            """
        )
    )

    # Mapped reactions can be sourced by calculation geometries, TS inference
    # artifacts, or concrete topology participants.  Keep NULL participants in
    # the summary so a source-less/ambiguous member quarantines the whole root.
    op.execute(
        sa.text(
            """
            WITH source_projects AS (
                SELECT node.mapped_reaction_id, af.project_id
                FROM mapped_reaction_node AS node
                JOIN mapped_reaction_node_geometry AS ng
                  ON ng.mapped_reaction_node_id = node.id
                JOIN calculation_frame AS cf ON cf.geometry_id = ng.geometry_id
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE pr.status = 'succeeded'
                  AND EXISTS (
                      SELECT 1
                      FROM artifact_ingestion AS ai
                      WHERE ai.artifact_file_id = af.id
                        AND ai.status = 'succeeded'
                  )
                UNION
                SELECT inference.mapped_reaction_id, af.project_id
                FROM transition_state_inference AS inference
                JOIN parse_revision AS pr ON pr.id = inference.parse_revision_id
                JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                WHERE inference.status = 'succeeded'
                  AND pr.status = 'succeeded'
                  AND EXISTS (
                      SELECT 1
                      FROM artifact_ingestion AS ai
                      WHERE ai.artifact_file_id = af.id
                        AND ai.status = 'succeeded'
                  )
                UNION ALL
                SELECT mrp.mapped_reaction_id, topology.project_id
                FROM mapped_reaction_participant AS mrp
                JOIN molecular_topology AS topology
                  ON topology.id = mrp.concrete_topology_id
                WHERE mrp.concrete_topology_id IS NOT NULL
            ), summary AS (
                SELECT mr.id,
                       min(sp.project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT sp.project_id) AS project_count,
                       count(*) FILTER (WHERE sp.project_id IS NULL) AS null_count,
                       count(sp.project_id) AS source_count
                FROM mapped_reaction AS mr
                LEFT JOIN source_projects AS sp
                  ON sp.mapped_reaction_id = mr.id
                GROUP BY mr.id
            )
            UPDATE mapped_reaction AS mr
            SET project_id = CASE
                WHEN summary.source_count > 0
                 AND summary.project_count = 1
                 AND summary.null_count = 0
                    THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.id = mr.id
            """
        )
    )

    # Logical reactions inherit ownership from both their declared topology
    # participants and their mapped descendants.  A mismatch is quarantined.
    op.execute(
        sa.text(
            """
            WITH source_projects AS (
                SELECT participant.logical_reaction_id, topology.project_id
                FROM logical_reaction_participant AS participant
                JOIN molecular_topology AS topology
                  ON topology.id = participant.topology_id
                UNION ALL
                SELECT mr.logical_reaction_id, mr.project_id
                FROM mapped_reaction AS mr
            ), summary AS (
                SELECT lr.id,
                       min(sp.project_id::text)::uuid AS owner_project_id,
                       count(DISTINCT sp.project_id) AS project_count,
                       count(*) FILTER (WHERE sp.project_id IS NULL) AS null_count,
                       count(sp.project_id) AS source_count
                FROM logical_reaction AS lr
                LEFT JOIN source_projects AS sp
                  ON sp.logical_reaction_id = lr.id
                GROUP BY lr.id
            )
            UPDATE logical_reaction AS lr
            SET project_id = CASE
                WHEN summary.source_count > 0
                 AND summary.project_count = 1
                 AND summary.null_count = 0
                    THEN summary.owner_project_id
                ELSE NULL
            END
            FROM summary
            WHERE summary.id = lr.id
            """
        )
    )

    # Make the parent/child ownership invariant explicit after both passes.
    # This also catches a mapped row whose direct source happened to be clean
    # but whose logical parent was already quarantined.
    op.execute(
        sa.text(
            """
            UPDATE mapped_reaction AS mr
            SET project_id = NULL
            FROM logical_reaction AS lr
            WHERE lr.id = mr.logical_reaction_id
              AND mr.project_id IS DISTINCT FROM lr.project_id
            """
        )
    )

    # The catalogue predates project-owned derived identities and may contain
    # one directory row for each historical source project of a shared
    # Geometry.  Remove only catalogue projections for quarantined or
    # mismatched Geometry rows; the Geometry, frames, revisions, and files stay
    # intact.  Rebuild the count table because its old trigger-maintained value
    # reflected the pre-isolation directory.
    op.execute(
        sa.text(
            """
            DELETE FROM project_geometry_catalog AS catalog
            USING geometry AS g
            WHERE g.id = catalog.geometry_id
              AND (
                  g.project_id IS NULL
                  OR g.project_id IS DISTINCT FROM catalog.project_id
              )
            """
        )
    )
    op.execute(sa.text("DELETE FROM project_geometry_catalog_count"))
    op.execute(
        sa.text(
            """
            INSERT INTO project_geometry_catalog_count (project_id, geometry_count)
            SELECT project_id, count(*)::bigint
            FROM project_geometry_catalog
            GROUP BY project_id
            """
        )
    )

    # The quarantine table is an audit ledger, not a delete queue.  Rows remain
    # in their original tables for forensic comparison and future re-import.
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'molecular_formula', id, '[]'::jsonb,
                   'no_unambiguous_project_owner_after_backfill'
            FROM molecular_formula
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'molecular_topology', id, '[]'::jsonb,
                   'no_unambiguous_project_owner_after_backfill'
            FROM molecular_topology
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'molecular_topology_abstraction', id, '[]'::jsonb,
                   'endpoint_topologies_do_not_share_one_project'
            FROM molecular_topology_abstraction
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'molecular_topology_derivation', id, '[]'::jsonb,
                   'parent_topology_has_no_unambiguous_project_owner'
            FROM molecular_topology_derivation
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'geometry', id, '[]'::jsonb,
                   'geometry_sources_do_not_share_one_project_with_topology'
            FROM geometry
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'logical_reaction', id, '[]'::jsonb,
                   'reaction_sources_do_not_share_one_project'
            FROM logical_reaction
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO derived_data_isolation_quarantine
                (object_type, object_id, source_project_ids, reason)
            SELECT 'mapped_reaction', id, '[]'::jsonb,
                   'reaction_sources_do_not_share_one_project'
            FROM mapped_reaction
            WHERE project_id IS NULL
            ON CONFLICT (object_type, object_id) DO UPDATE
            SET reason = EXCLUDED.reason
            """
        )
    )


def upgrade() -> None:
    for table_name in _OWNED_TABLES:
        _add_project_column(table_name)

    op.create_table(
        "derived_data_isolation_quarantine",
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_project_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("object_type", "object_id"),
        sa.CheckConstraint(
            "jsonb_typeof(source_project_ids) = 'array'",
            name="ck_derived_isolation_quarantine_project_ids_array",
        ),
    )
    op.create_index(
        "ix_derived_isolation_quarantine_object_id",
        "derived_data_isolation_quarantine",
        ["object_id"],
    )

    # Remove the old global identity barriers before creating their
    # project-qualified replacements.  NULL is intentional for quarantined
    # rows: PostgreSQL permits multiple unresolved identities while they are
    # excluded from project-scoped reads.
    op.drop_constraint(
        "molecular_formula_composition_hash_key",
        "molecular_formula",
        type_="unique",
    )
    op.drop_constraint("uq_molecular_topology_identity_hash", "molecular_topology", type_="unique")
    op.drop_constraint("uq_logical_reaction_hash", "logical_reaction", type_="unique")
    op.create_unique_constraint(
        "uq_molecular_formula_project_composition",
        "molecular_formula",
        ["project_id", "composition_hash"],
    )
    op.create_unique_constraint(
        "uq_molecular_topology_project_identity_hash",
        "molecular_topology",
        ["project_id", "identity_schema_version", "graph_hash"],
    )
    op.create_unique_constraint(
        "uq_logical_reaction_project_hash",
        "logical_reaction",
        ["project_id", "reaction_hash"],
    )

    _backfill_project_ownership()


def downgrade() -> None:
    # A downgrade is intended for an empty/new database. Existing rows may
    # legitimately contain two project-qualified identities and therefore
    # cannot be collapsed back into the old global unique constraints safely.
    op.drop_index(
        "ix_derived_isolation_quarantine_object_id",
        table_name="derived_data_isolation_quarantine",
    )
    op.drop_table("derived_data_isolation_quarantine")
    op.drop_constraint(
        "uq_molecular_formula_project_composition",
        "molecular_formula",
        type_="unique",
    )
    op.drop_constraint(
        "uq_molecular_topology_project_identity_hash",
        "molecular_topology",
        type_="unique",
    )
    op.drop_constraint("uq_logical_reaction_project_hash", "logical_reaction", type_="unique")
    op.create_unique_constraint(
        "molecular_formula_composition_hash_key",
        "molecular_formula",
        ["composition_hash"],
    )
    op.create_unique_constraint(
        "uq_molecular_topology_identity_hash",
        "molecular_topology",
        ["identity_schema_version", "graph_hash"],
    )
    op.create_unique_constraint(
        "uq_logical_reaction_hash",
        "logical_reaction",
        ["reaction_hash"],
    )
    for table_name in reversed(_OWNED_TABLES):
        op.drop_index(f"ix_{table_name}_project_id", table_name=table_name)
        op.drop_constraint(f"fk_{table_name}_project", table_name, type_="foreignkey")
        op.drop_column(table_name, "project_id")
