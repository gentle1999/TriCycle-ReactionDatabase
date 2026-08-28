"""Persist the TS electronic state on both displaced endpoints."""

import sqlalchemy as sa
from alembic import op

revision: str = "0008_ts_endpoint_state"
down_revision: str | None = "0007_geometry_electronic_state"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "transition_state_endpoint",
        sa.Column("charge", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "transition_state_endpoint",
        sa.Column("multiplicity", sa.SmallInteger(), nullable=True),
    )
    op.execute(
        """
        UPDATE transition_state_endpoint AS endpoint
        SET charge = frame.charge,
            multiplicity = frame.multiplicity
        FROM calculation_frame AS frame
        WHERE frame.id = endpoint.calculation_frame_id
        """
    )
    op.alter_column("transition_state_endpoint", "charge", nullable=False)
    op.alter_column("transition_state_endpoint", "multiplicity", nullable=False)
    op.create_check_constraint(
        "ck_transition_state_endpoint_multiplicity_positive",
        "transition_state_endpoint",
        "multiplicity > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_transition_state_endpoint_multiplicity_positive",
        "transition_state_endpoint",
        type_="check",
    )
    op.drop_column("transition_state_endpoint", "multiplicity")
    op.drop_column("transition_state_endpoint", "charge")
