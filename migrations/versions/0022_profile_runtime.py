"""Persist distinct source-file runtimes for mapped-reaction profiles."""

import sqlalchemy as sa
from alembic import op

revision: str = "0022_profile_runtime"
down_revision: str | None = "0021_parse_revision_running_time"
branch_labels: str | None = None
depends_on: str | None = None


_COLUMNS = (
    "reactants_running_time_seconds",
    "transition_state_running_time_seconds",
    "products_running_time_seconds",
    "total_running_time_seconds",
)
_CONSTRAINTS = (
    "ck_mapped_rxn_profile_reactants_runtime_nonnegative",
    "ck_mapped_rxn_profile_ts_runtime_nonnegative",
    "ck_mapped_rxn_profile_products_runtime_nonnegative",
    "ck_mapped_rxn_profile_total_runtime_nonnegative",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("mapped_reaction_thermodynamic_profile")
    }
    for column_name in _COLUMNS:
        if column_name not in columns:
            op.add_column(
                "mapped_reaction_thermodynamic_profile",
                sa.Column(column_name, sa.Float(), nullable=True),
            )
    constraints = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints("mapped_reaction_thermodynamic_profile")
    }
    for column_name, constraint_name in zip(_COLUMNS, _CONSTRAINTS, strict=True):
        if constraint_name not in constraints:
            op.create_check_constraint(
                constraint_name,
                "mapped_reaction_thermodynamic_profile",
                f"{column_name} IS NULL OR {column_name} >= 0",
            )


def downgrade() -> None:
    for _column_name, constraint_name in zip(
        reversed(_COLUMNS), reversed(_CONSTRAINTS), strict=True
    ):
        op.drop_constraint(
            constraint_name,
            "mapped_reaction_thermodynamic_profile",
            type_="check",
        )
    for column_name in reversed(_COLUMNS):
        op.drop_column("mapped_reaction_thermodynamic_profile", column_name)
