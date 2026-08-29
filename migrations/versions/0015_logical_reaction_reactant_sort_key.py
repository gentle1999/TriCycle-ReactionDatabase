"""Materialize the default LogicalReaction catalogue sort key."""

import sqlalchemy as sa
from alembic import op

revision: str = "0015_logical_reaction_sort_key"
down_revision: str | None = "0014_frame_count_null_order"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "logical_reaction",
        sa.Column("reactant_sort_key", sa.ARRAY(sa.Text()), nullable=True),
    )
    op.execute(
        """
        CREATE FUNCTION refresh_logical_reaction_reactant_sort_key(target_reaction_id uuid)
        RETURNS void
        LANGUAGE sql
        AS $$
            UPDATE logical_reaction AS reaction
            SET reactant_sort_key = sort_key.values
            FROM (
                SELECT array_agg(
                    concat(
                        topology.canonical_isomeric_smiles,
                        ':',
                        participant.stoichiometric_coefficient::text
                    )
                    ORDER BY
                        topology.canonical_isomeric_smiles,
                        participant.stoichiometric_coefficient,
                        participant.participant_index
                ) AS values
                FROM logical_reaction_participant AS participant
                JOIN molecular_topology AS topology ON topology.id = participant.topology_id
                WHERE participant.logical_reaction_id = target_reaction_id
                  AND participant.side = 'reactant'
            ) AS sort_key
            WHERE reaction.id = target_reaction_id;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_logical_reaction_reactant_sort_key()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            PERFORM refresh_logical_reaction_reactant_sort_key(
                COALESCE(NEW.logical_reaction_id, OLD.logical_reaction_id)
            );
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION sync_topology_reactant_sort_keys()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_reaction_id uuid;
        BEGIN
            FOR affected_reaction_id IN
                SELECT DISTINCT logical_reaction_id
                FROM logical_reaction_participant
                WHERE topology_id = NEW.id
                  AND side = 'reactant'
            LOOP
                PERFORM refresh_logical_reaction_reactant_sort_key(affected_reaction_id);
            END LOOP;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER logical_reaction_reactant_sort_key_after_participant_change
        AFTER INSERT OR DELETE OR UPDATE OF topology_id, side, stoichiometric_coefficient,
            participant_index ON logical_reaction_participant
        FOR EACH ROW
        EXECUTE FUNCTION sync_logical_reaction_reactant_sort_key()
        """
    )
    op.execute(
        """
        CREATE TRIGGER logical_reaction_reactant_sort_key_after_topology_change
        AFTER UPDATE OF canonical_isomeric_smiles ON molecular_topology
        FOR EACH ROW
        WHEN (OLD.canonical_isomeric_smiles IS DISTINCT FROM NEW.canonical_isomeric_smiles)
        EXECUTE FUNCTION sync_topology_reactant_sort_keys()
        """
    )
    op.execute(
        """
        UPDATE logical_reaction AS reaction
        SET reactant_sort_key = sort_key.values
        FROM (
            SELECT
                participant.logical_reaction_id,
                array_agg(
                    concat(
                        topology.canonical_isomeric_smiles,
                        ':',
                        participant.stoichiometric_coefficient::text
                    )
                    ORDER BY
                        topology.canonical_isomeric_smiles,
                        participant.stoichiometric_coefficient,
                        participant.participant_index
                ) AS values
            FROM logical_reaction_participant AS participant
            JOIN molecular_topology AS topology ON topology.id = participant.topology_id
            WHERE participant.side = 'reactant'
            GROUP BY participant.logical_reaction_id
        ) AS sort_key
        WHERE reaction.id = sort_key.logical_reaction_id
        """
    )
    op.create_index(
        "ix_logical_reaction_reactant_sort_created_id",
        "logical_reaction",
        ["reactant_sort_key", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_logical_reaction_reactant_sort_created_id",
        table_name="logical_reaction",
    )
    op.execute(
        "DROP TRIGGER IF EXISTS logical_reaction_reactant_sort_key_after_topology_change "
        "ON molecular_topology"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS logical_reaction_reactant_sort_key_after_participant_change "
        "ON logical_reaction_participant"
    )
    op.execute("DROP FUNCTION IF EXISTS sync_topology_reactant_sort_keys()")
    op.execute("DROP FUNCTION IF EXISTS sync_logical_reaction_reactant_sort_key()")
    op.execute("DROP FUNCTION IF EXISTS refresh_logical_reaction_reactant_sort_key(uuid)")
    op.drop_column("logical_reaction", "reactant_sort_key")
