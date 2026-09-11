"""Reject cross-project links between derived chemistry and reaction rows.

``0035`` added project ownership to the derived roots and quarantined
ambiguous historical rows.  This migration closes the remaining write-side
gap: relationship tables do not all carry a redundant ``project_id`` column,
so application-only checks were still bypassable by direct SQL or a missed
service path.  The trigger permits quarantined NULL ownership to remain
inspectable, but rejects any relationship whose two known owners differ.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0036_project_isolation"
down_revision: str | None = "0035_project_owned_derived_data"
branch_labels: str | None = None
depends_on: str | None = None


_TRIGGER_TABLES = (
    "molecular_topology",
    "molecular_topology_derivation",
    "molecular_topology_abstraction",
    "geometry",
    "logical_reaction_participant",
    "logical_participant_concrete_topology",
    "mapped_reaction",
    "mapped_reaction_participant",
    "mapped_reaction_node",
    "mapped_reaction_node_geometry",
    "mapped_reaction_thermodynamic_profile",
    "parse_revision",
    "calculation_frame",
    "transition_state_endpoint",
    "transition_state_inference",
    "project_geometry_catalog",
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_cross_project_derived_write()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                expected_project uuid;
                expected_project_2 uuid;
                expected_project_3 uuid;
                expected_project_4 uuid;
                expected_artifact_id uuid;
                revision_artifact_id uuid;
                frame_artifact_id uuid;
                expected_logical_id uuid;
            BEGIN
                -- Project ownership is immutable once a derived root has
                -- been written.  Repartitioning is a new import, not an
                -- UPDATE that can silently move history between projects.
                IF TG_TABLE_NAME IN (
                    'molecular_formula',
                    'molecular_topology',
                    'molecular_topology_abstraction',
                    'molecular_topology_derivation',
                    'geometry',
                    'logical_reaction',
                    'mapped_reaction'
                ) AND TG_OP = 'UPDATE' THEN
                    IF NEW.project_id IS DISTINCT FROM OLD.project_id THEN
                        RAISE EXCEPTION
                            'project ownership is immutable for %', TG_TABLE_NAME
                            USING ERRCODE = '23514';
                    END IF;
                END IF;

                IF TG_TABLE_NAME = 'molecular_topology' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM molecular_formula
                    WHERE id = NEW.formula_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project THEN
                        RAISE EXCEPTION
                            'MolecularTopology and MolecularFormula must share project_id'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'molecular_topology_derivation' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM molecular_topology
                    WHERE id = NEW.topology_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project THEN
                        RAISE EXCEPTION
                            'MolecularTopologyDerivation and MolecularTopology '
                            'must share project_id'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'molecular_topology_abstraction' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM molecular_topology
                    WHERE id = NEW.specific_topology_id;
                    SELECT project_id
                    INTO expected_project_2
                    FROM molecular_topology
                    WHERE id = NEW.general_topology_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project
                       OR expected_project IS DISTINCT FROM expected_project_2 THEN
                        RAISE EXCEPTION
                            'topology abstraction endpoints must share project_id'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'geometry' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM molecular_topology
                    WHERE id = NEW.topology_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project THEN
                        RAISE EXCEPTION
                            'Geometry and MolecularTopology must share project_id'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'logical_reaction_participant' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM logical_reaction
                    WHERE id = NEW.logical_reaction_id;
                    SELECT project_id
                    INTO expected_project_2
                    FROM molecular_topology
                    WHERE id = NEW.topology_id;
                    IF expected_project IS DISTINCT FROM expected_project_2 THEN
                        RAISE EXCEPTION
                            'logical reaction participant crosses project boundary'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'logical_participant_concrete_topology' THEN
                    SELECT lr.project_id, ct.project_id
                    INTO expected_project, expected_project_2
                    FROM logical_reaction_participant AS lp
                    JOIN logical_reaction AS lr ON lr.id = lp.logical_reaction_id
                    JOIN molecular_topology AS ct ON ct.id = NEW.concrete_topology_id
                    WHERE lp.id = NEW.logical_reaction_participant_id;
                    IF expected_project IS DISTINCT FROM expected_project_2 THEN
                        RAISE EXCEPTION
                            'concrete topology membership crosses project boundary'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'mapped_reaction' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM logical_reaction
                    WHERE id = NEW.logical_reaction_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project THEN
                        RAISE EXCEPTION
                            'MappedReaction and LogicalReaction must share project_id'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'mapped_reaction_participant' THEN
                    SELECT mr.project_id, mr.logical_reaction_id
                    INTO expected_project, expected_logical_id
                    FROM mapped_reaction AS mr
                    WHERE mr.id = NEW.mapped_reaction_id;
                    SELECT lr.project_id
                    INTO expected_project_2
                    FROM logical_reaction_participant AS lp
                    JOIN logical_reaction AS lr ON lr.id = lp.logical_reaction_id
                    WHERE lp.id = NEW.logical_reaction_participant_id;
                    IF expected_logical_id IS DISTINCT FROM (
                        SELECT logical_reaction_id
                        FROM logical_reaction_participant
                        WHERE id = NEW.logical_reaction_participant_id
                    ) THEN
                        RAISE EXCEPTION
                            'mapped participant must belong to its mapped reaction'
                            USING ERRCODE = '23514';
                    END IF;
                    IF expected_project IS DISTINCT FROM expected_project_2 THEN
                        RAISE EXCEPTION
                            'mapped participant crosses logical reaction project boundary'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.concrete_topology_id IS NOT NULL THEN
                        SELECT project_id
                        INTO expected_project_3
                        FROM molecular_topology
                        WHERE id = NEW.concrete_topology_id;
                        IF expected_project IS DISTINCT FROM expected_project_3 THEN
                            RAISE EXCEPTION
                                'mapped participant concrete topology crosses project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;

                ELSIF TG_TABLE_NAME = 'mapped_reaction_node' THEN
                    -- The parent foreign key already fixes the node's
                    -- project: a node has no independent project identity.
                    -- Keep this branch explicit as documentation of that
                    -- ownership boundary.
                    NULL;

                ELSIF TG_TABLE_NAME = 'mapped_reaction_node_geometry' THEN
                    SELECT mr.project_id, g.project_id
                    INTO expected_project, expected_project_2
                    FROM mapped_reaction_node AS node
                    JOIN mapped_reaction AS mr ON mr.id = node.mapped_reaction_id
                    JOIN geometry AS g ON g.id = NEW.geometry_id
                    WHERE node.id = NEW.mapped_reaction_node_id;
                    IF expected_project IS NOT NULL
                       AND expected_project_2 IS NOT NULL
                       AND expected_project IS DISTINCT FROM expected_project_2 THEN
                        RAISE EXCEPTION
                            'mapped node geometry crosses project boundary'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.mapped_reaction_participant_id IS NOT NULL THEN
                        SELECT mr.project_id
                        INTO expected_project_3
                        FROM mapped_reaction_participant AS mrp
                        JOIN mapped_reaction AS mr ON mr.id = mrp.mapped_reaction_id
                        WHERE mrp.id = NEW.mapped_reaction_participant_id;
                        IF expected_project IS NOT NULL
                           AND expected_project_3 IS NOT NULL
                           AND expected_project IS DISTINCT FROM expected_project_3 THEN
                            RAISE EXCEPTION
                                'mapped node geometry participant crosses project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;

                ELSIF TG_TABLE_NAME = 'mapped_reaction_thermodynamic_profile' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM mapped_reaction
                    WHERE id = NEW.mapped_reaction_id;
                    IF expected_project IS NULL THEN
                        RAISE EXCEPTION
                            'thermodynamic profile requires a project-owned mapped reaction'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'parse_revision' THEN
                    IF NEW.reparse_of_id IS NOT NULL THEN
                        SELECT artifact_file_id
                        INTO expected_artifact_id
                        FROM parse_revision
                        WHERE id = NEW.reparse_of_id;
                        IF expected_artifact_id IS DISTINCT FROM NEW.artifact_file_id THEN
                            RAISE EXCEPTION
                                'reparse_of must reference a revision of the same ArtifactFile'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;

                ELSIF TG_TABLE_NAME = 'calculation_frame' THEN
                    SELECT af.project_id, g.project_id, d.project_id
                    INTO expected_project, expected_project_2, expected_project_3
                    FROM parse_revision AS pr
                    JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                    JOIN geometry AS g ON g.id = NEW.geometry_id
                    JOIN molecular_topology_derivation AS d
                      ON d.id = NEW.topology_derivation_id
                    WHERE pr.id = NEW.parse_revision_id;
                    IF (expected_project IS DISTINCT FROM expected_project_2)
                       OR (expected_project IS DISTINCT FROM expected_project_3)
                       OR (expected_project_2 IS DISTINCT FROM expected_project_3) THEN
                        RAISE EXCEPTION
                            'CalculationFrame source, Geometry, and derivation '
                            'cross project boundary'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'transition_state_endpoint' THEN
                    SELECT af.project_id, g.project_id, d.project_id
                    INTO expected_project, expected_project_2, expected_project_3
                    FROM calculation_frame AS cf
                    JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                    JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                    JOIN geometry AS g ON g.id = cf.geometry_id
                    JOIN molecular_topology_derivation AS d
                      ON d.id = cf.topology_derivation_id
                    WHERE cf.id = NEW.calculation_frame_id;
                    SELECT project_id
                    INTO expected_project_4
                    FROM molecular_topology
                    WHERE id = NEW.topology_id;
                    IF (expected_project IS DISTINCT FROM expected_project_2)
                       OR (expected_project IS DISTINCT FROM expected_project_3)
                       OR (expected_project IS DISTINCT FROM expected_project_4) THEN
                        RAISE EXCEPTION
                            'TS endpoint crosses calculation project boundary'
                            USING ERRCODE = '23514';
                    END IF;

                ELSIF TG_TABLE_NAME = 'transition_state_inference' THEN
                    SELECT ai.artifact_file_id, af.project_id, pr.artifact_file_id
                    INTO expected_artifact_id, expected_project, revision_artifact_id
                    FROM artifact_ingestion AS ai
                    JOIN artifact_file AS af ON af.id = ai.artifact_file_id
                    JOIN parse_revision AS pr ON pr.id = NEW.parse_revision_id
                    WHERE ai.id = NEW.artifact_ingestion_id;
                    IF expected_artifact_id IS DISTINCT FROM revision_artifact_id THEN
                        RAISE EXCEPTION
                            'TS inference ingestion and revision must use the same ArtifactFile'
                            USING ERRCODE = '23514';
                    END IF;
                    IF NEW.logical_reaction_id IS NOT NULL THEN
                        SELECT project_id
                        INTO expected_project_2
                        FROM logical_reaction
                        WHERE id = NEW.logical_reaction_id;
                        IF expected_project IS DISTINCT FROM expected_project_2 THEN
                            RAISE EXCEPTION
                                'TS inference logical reaction crosses source project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    IF NEW.mapped_reaction_id IS NOT NULL THEN
                        SELECT project_id, logical_reaction_id
                        INTO expected_project_3, expected_logical_id
                        FROM mapped_reaction
                        WHERE id = NEW.mapped_reaction_id;
                        IF expected_project IS DISTINCT FROM expected_project_3 THEN
                            RAISE EXCEPTION
                                'TS inference mapped reaction crosses source project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                        IF NEW.logical_reaction_id IS NOT NULL
                           AND expected_logical_id IS DISTINCT FROM NEW.logical_reaction_id THEN
                            RAISE EXCEPTION
                                'TS inference logical and mapped reactions do not match'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    IF NEW.calculation_frame_id IS NOT NULL THEN
                        SELECT af.project_id, af.id
                        INTO expected_project_4, frame_artifact_id
                        FROM calculation_frame AS cf
                        JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                        JOIN artifact_file AS af ON af.id = pr.artifact_file_id
                        WHERE cf.id = NEW.calculation_frame_id;
                        IF expected_artifact_id IS DISTINCT FROM frame_artifact_id THEN
                            RAISE EXCEPTION
                                'TS inference frame must use the ingestion ArtifactFile'
                                USING ERRCODE = '23514';
                        END IF;
                        IF expected_project IS DISTINCT FROM expected_project_4 THEN
                            RAISE EXCEPTION
                                'TS inference frame crosses source project boundary'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;

                ELSIF TG_TABLE_NAME = 'project_geometry_catalog' THEN
                    SELECT project_id
                    INTO expected_project
                    FROM geometry
                    WHERE id = NEW.geometry_id;
                    IF NEW.project_id IS DISTINCT FROM expected_project THEN
                        RAISE EXCEPTION
                            'project geometry catalogue and Geometry must share project_id'
                            USING ERRCODE = '23514';
                    END IF;
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
                f"""
                CREATE TRIGGER trg_{table_name}_project_immutable
                BEFORE INSERT OR UPDATE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION reject_cross_project_derived_write()
                """
            )
        )
    for table_name in _TRIGGER_TABLES:
        if table_name in {
            "molecular_formula",
            "molecular_topology",
            "molecular_topology_abstraction",
            "molecular_topology_derivation",
            "geometry",
            "logical_reaction",
            "mapped_reaction",
        }:
            continue
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER trg_{table_name}_project_boundary
                BEFORE INSERT OR UPDATE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION reject_cross_project_derived_write()
                """
            )
        )


def downgrade() -> None:
    for table_name in (*_TRIGGER_TABLES, "molecular_formula"):
        trigger_name = (
            f"trg_{table_name}_project_immutable"
            if table_name
            in {
                "molecular_formula",
                "molecular_topology",
                "molecular_topology_abstraction",
                "molecular_topology_derivation",
                "geometry",
                "logical_reaction",
                "mapped_reaction",
            }
            else f"trg_{table_name}_project_boundary"
        )
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
    for table_name in (
        "molecular_topology",
        "molecular_topology_abstraction",
        "molecular_topology_derivation",
        "geometry",
        "logical_reaction",
        "mapped_reaction",
    ):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_project_immutable ON {table_name}")
        )
    op.execute(sa.text("DROP FUNCTION IF EXISTS reject_cross_project_derived_write()"))
