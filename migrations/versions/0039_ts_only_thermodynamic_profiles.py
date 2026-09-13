"""Allow materialized transition-state-only thermodynamic profiles."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_ts_only_profile"
down_revision: str | None = "0038_geometry_match_index"
branch_labels: str | None = None
depends_on: str | None = None


_TABLE = "mapped_reaction_thermodynamic_profile"
_REACTANT_SCALARS = (
    "reactants_enthalpy_hartree",
    "reactants_gibbs_free_energy_hartree",
    "reactants_entropy_cal_mol_k",
)


def upgrade() -> None:
    op.alter_column(_TABLE, "reactants", existing_type=postgresql.JSONB(), nullable=True)
    for column_name in _REACTANT_SCALARS:
        op.alter_column(
            _TABLE,
            column_name,
            existing_type=sa.Float(),
            nullable=True,
        )
    op.create_check_constraint(
        "ck_mapped_rxn_profile_has_thermodynamic_state",
        _TABLE,
        "reactants IS NOT NULL OR transition_state IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_mapped_rxn_profile_has_thermodynamic_state",
        _TABLE,
        type_="check",
    )
    for column_name in reversed(_REACTANT_SCALARS):
        op.alter_column(
            _TABLE,
            column_name,
            existing_type=sa.Float(),
            nullable=False,
        )
    op.alter_column(_TABLE, "reactants", existing_type=postgresql.JSONB(), nullable=False)
