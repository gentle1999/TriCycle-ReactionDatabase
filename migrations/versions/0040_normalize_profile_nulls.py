"""Store optional thermodynamic JSON states as SQL NULL."""

import sqlalchemy as sa
from alembic import op

revision: str = "0040_normalize_profile_nulls"
down_revision: str | None = "0039_ts_only_profile"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE mapped_reaction_thermodynamic_profile
            SET reactants = NULL
            WHERE reactants = 'null'::jsonb
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE mapped_reaction_thermodynamic_profile
            SET transition_state = NULL
            WHERE transition_state = 'null'::jsonb
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE mapped_reaction_thermodynamic_profile
            SET products = NULL
            WHERE products = 'null'::jsonb
            """
        )
    )


def downgrade() -> None:
    # SQL NULL is the canonical representation for an omitted optional state;
    # retaining it is safe when rolling back this data-normalization migration.
    pass
