"""Preserve the legacy NULL-SMILES ordering in the materialized sort key."""

from alembic import op

revision: str = "0017_reactant_sort_key"
down_revision: str | None = "0016_geometry_frame_count"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION
            refresh_logical_reaction_reactant_sort_key(target_reaction_id uuid)
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


def downgrade() -> None:
    # The predecessor's function is semantically equivalent on populated data;
    # the old placeholder expression only affected NULL canonical SMILES.
    pass
